"""A stand-in for the winding machine's Moonraker, for trying the Control tab
(and testing machine_link) without the machine.

    python fake_moonraker.py [--port 7125] [--speed 20]

then connect the Control tab to 127.0.0.1:7125. It answers the requests the
app makes: status, G-code scripts (G28, G90/G91, G1, M400, LASER_ON/OFF, M117;
anything else is refused, as Klipper-for-CNC does), uploads, downloads, and
start/pause/resume/cancel/emergency stop. A started file is "wound" by moving
through its moves on the clock (`speed` times faster than real); a PAUSE in it
pauses, as on the machine. Only a model: no acceleration, no lookahead.
"""
import argparse
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMachine:
    def __init__(self, speed=1.0, folder=None):
        self.lock = threading.RLock()
        self.speed = speed
        self.folder = folder or tempfile.mkdtemp(prefix="fake_moonraker_")
        self.klippy_state = "ready"
        self.position = [0.0, 0.0, 0.0, 0.0]
        self.homed = ""
        self.relative = False
        self.laser = False
        self.motors = {"stepper_x", "stepper_y", "stepper_a"}  # enabled steppers
        self.message = ""
        self.scripts = []          # every script run, for tests
        self.requests = []         # (method, path) of every request, for tests
        self.print_state = "standby"
        self.filename = ""
        self.file_size = 0
        self.file_position = 0
        self.print_duration = 0.0
        self._moves = []           # (end offset, x, y, seconds, is_pause)
        self._index = 0
        self._left = 0.0           # seconds left of the current move
        self._last = time.monotonic()
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    # --- Status ---

    def status(self):
        with self.lock:
            self._advance()
            size = max(1, self.file_size)
            return {
                "webhooks": {"state": self.klippy_state,
                             "state_message": "Emergency stop" if self.klippy_state == "shutdown" else "Printer is ready"},
                "print_stats": {"state": self.print_state, "filename": self.filename,
                                "print_duration": self.print_duration},
                "virtual_sdcard": {"file_position": self.file_position, "file_size": self.file_size,
                                   "progress": self.file_position / size, "is_active": self.print_state == "printing"},
                "toolhead": {"position": list(self.position), "homed_axes": self.homed},
                "motion_report": {"live_position": list(self.position)},
                "display_status": {"message": self.message},
                "idle_timeout": {"state": "Printing" if self.print_state == "printing" else "Idle"},
            }

    # --- G-code ---

    def run_script(self, script):
        with self.lock:
            if self.klippy_state != "ready":
                raise ValueError("Klipper is not ready")
            self.scripts.append(script)
            for line in script.splitlines():
                self._command(line.split(";", 1)[0].strip())

    def _command(self, line):
        if not line:
            return
        word = line.split()[0].upper()
        if word == "G28":
            axes = [a for a in "XYA" if a in line.upper()[3:]] or ["X", "Y", "A"]
            for a in axes:
                self.position[{"X": 0, "Y": 1, "A": 3}[a]] = 0.0
                if a.lower() not in self.homed:
                    self.homed += a.lower()
        elif word == "G90":
            self.relative = False
        elif word == "G91":
            self.relative = True
        elif word in ("G1", "G0"):
            target = list(self.position)
            for part in line.split()[1:]:
                axis, value = part[0].upper(), part[1:]
                if axis in "XYA":
                    i = 3 if axis == "A" else "XY".index(axis)
                    v = float(value)
                    target[i] = target[i] + v if self.relative else v
                    if axis in "XY" and axis.lower() not in self.homed:
                        raise ValueError("Must home axis first")
                    if axis == "Y" and not 0 <= target[i] <= 180:
                        raise ValueError(f"Move out of range: Y{target[i]:.3f}")
                    if axis == "X" and not 0 <= target[i] <= 5250:
                        raise ValueError(f"Move out of range: X{target[i]:.3f}")
            self.position = target
        elif word == "G92":
            pass
        elif word in ("M400", "SET_PRINT_STATS_INFO", "RESPOND"):
            pass
        elif word == "M117":
            self.message = line[4:].strip()
        elif word in ("M84", "M18"):
            self.homed = ""
            self.motors = set()
        elif word == "SET_STEPPER_ENABLE":
            args = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
            name = args.get("STEPPER", "")
            if args.get("ENABLE", "1") == "0":
                self.motors.discard(name)
                axis = name.replace("stepper_", "")[:1]
                self.homed = self.homed.replace(axis, "")
            else:
                self.motors.add(name)
        elif word == "LASER_ON":
            self.laser = True
        elif word == "LASER_OFF":
            self.laser = False
        elif word == "PAUSE":
            if self.print_state == "printing":
                self.print_state = "paused"
        else:
            raise ValueError(f"Unknown command:\"{word}\"")

    # --- Printing ---

    def start(self, filename):
        with self.lock:
            if self.print_state in ("printing", "paused"):
                raise ValueError("Printer is busy")
            path = os.path.join(self.folder, filename)
            if not os.path.exists(path):
                raise FileNotFoundError(filename)
            self._moves = self._parse(path)
            self.filename, self.file_size = filename, os.path.getsize(path)
            self.file_position, self._index, self._left, self.print_duration = 0, 0, 0.0, 0.0
            self.print_state = "printing"
            self._last = time.monotonic()

    def _parse(self, path):
        moves, pos, x, y, a, feed = [], 0, 0.0, 0.0, 0.0, 6000.0
        with open(path, "rb") as f:
            for raw in f:
                pos += len(raw)
                line = raw.decode("utf-8", "replace").split(";", 1)[0].strip()
                if not line:
                    continue
                if line.startswith(("G1", "G0")):
                    nx, ny, na = x, y, a
                    for p in line.split()[1:]:
                        try:
                            v = float(p[1:])
                        except ValueError:
                            continue
                        if p[0] == "X": nx = v
                        elif p[0] == "Y": ny = v
                        elif p[0] == "A": na = v
                        elif p[0] == "F": feed = v
                    dist = math.sqrt((nx - x) ** 2 + (ny - y) ** 2 + (na - a) ** 2)
                    moves.append((pos, nx, ny, na, dist / feed * 60.0 if feed > 0 else 0.0, None))
                    x, y, a = nx, ny, na
                else:
                    moves.append((pos, None, None, None, 0.0, line))
        return moves

    def _advance(self):
        now = time.monotonic()
        dt, self._last = (now - self._last) * self.speed, now
        if self.print_state != "printing":
            return
        self.print_duration += dt
        while self.print_state == "printing" and self._index < len(self._moves):
            end, x, y, a, seconds, text = self._moves[self._index]
            if text is not None:
                self.file_position = end
                self._index += 1
                try:
                    self._command(text)
                except ValueError:
                    pass
                continue
            if self._left <= 0:
                self._left = seconds
            step = min(dt, self._left)
            f = step / self._left if self._left > 0 else 1.0
            for i, v in ((0, x), (1, y), (3, a)):
                self.position[i] += (v - self.position[i]) * f
            self._left -= step
            dt -= step
            self.file_position = end
            if self._left > 1e-9:
                return
            self._index += 1
        if self._index >= len(self._moves) and self.print_state == "printing":
            self.print_state = "complete"

    def _run(self):
        while not self._stop:
            with self.lock:
                self._advance()
            time.sleep(0.05)

    def pause(self):
        with self.lock:
            if self.print_state == "printing":
                self.print_state = "paused"

    def resume(self):
        with self.lock:
            if self.print_state == "paused":
                self.print_state = "printing"
                self._last = time.monotonic()

    def cancel(self):
        with self.lock:
            if self.print_state in ("printing", "paused"):
                self.print_state = "cancelled"

    def emergency_stop(self):
        with self.lock:
            self.klippy_state = "shutdown"
            if self.print_state in ("printing", "paused"):
                self.print_state = "error"
            self.homed = ""

    def firmware_restart(self):
        with self.lock:
            self.klippy_state = "ready"
            self.print_state, self.filename, self.file_position = "standby", "", 0


def _multipart_file(body, content_type):
    # (filename, bytes) of the "file" part of a multipart/form-data body.
    boundary = re.search(r"boundary=([^;]+)", content_type).group(1).encode()
    for part in body.split(b"--" + boundary):
        head, _, data = part.partition(b"\r\n\r\n")
        match = re.search(rb'name="file"; filename="([^"]*)"', head)
        if match:
            return match.group(1).decode(), data[:-2] if data.endswith(b"\r\n") else data
    raise ValueError("no file part")


def make_handler(machine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, result):
            self._send(200, {"result": result})

        def _error(self, code, message):
            self._send(code, {"error": {"code": code, "message": message}})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            machine.requests.append(("GET", url.path))
            if url.path == "/server/info":
                return self._ok({"klippy_state": machine.klippy_state, "klippy_connected": True})
            if url.path == "/printer/objects/query":
                return self._ok({"eventtime": time.monotonic(), "status": machine.status()})
            if url.path.startswith("/server/files/gcodes/"):
                name = urllib.parse.unquote(url.path[len("/server/files/gcodes/"):])
                path = os.path.join(machine.folder, name)
                if not os.path.exists(path):
                    return self._error(404, "File not found")
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._error(404, "Not found")

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            machine.requests.append(("POST", url.path))
            params = dict(urllib.parse.parse_qsl(url.query))
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                if url.path == "/printer/gcode/script":
                    machine.run_script(json.loads(body)["script"])
                    return self._ok("ok")
                if url.path == "/server/files/upload":
                    name, data = _multipart_file(body, self.headers["Content-Type"])
                    with open(os.path.join(machine.folder, name), "wb") as f:
                        f.write(data)
                    # Unwrapped, as the real Moonraker answers an upload.
                    return self._send(201, {"item": {"path": name, "root": "gcodes"}, "action": "create_file",
                                            "print_started": False, "print_queued": False})
                if url.path == "/printer/print/start":
                    machine.start(params["filename"])
                    return self._ok("ok")
                actions = {"/printer/print/pause": machine.pause, "/printer/print/resume": machine.resume,
                           "/printer/print/cancel": machine.cancel, "/printer/emergency_stop": machine.emergency_stop,
                           "/printer/firmware_restart": machine.firmware_restart}
                if url.path in actions:
                    actions[url.path]()
                    return self._ok("ok")
            except (ValueError, KeyError, FileNotFoundError) as e:
                return self._error(400, str(e))
            self._error(404, "Not found")

    return Handler


def serve(machine, host="127.0.0.1", port=0):
    """Starts serving `machine` on a thread; returns the server (its
    server_address tells the port)."""
    server = ThreadingHTTPServer((host, port), make_handler(machine))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=7125)
    parser.add_argument("--speed", type=float, default=20.0, help="how much faster than real a file is run")
    args = parser.parse_args()
    machine = FakeMachine(speed=args.speed)
    server = serve(machine, port=args.port)
    print(f"Fake Moonraker on 127.0.0.1:{server.server_address[1]} (files in {machine.folder}); Ctrl+C stops it.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
