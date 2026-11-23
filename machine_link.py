"""Talking to the winding machine: Klipper (Kalico) through Moonraker's HTTP API.

The PC is only ever an observer of a wind. Once a file is started, Klipper
reads it from the machine's own storage (its virtual SD card), so this app can
disconnect, crash or be run on several PCs at once without the wind noticing.
That only holds as long as the app never sends anything on its own, so:

- connecting, reconnecting and polling only ever *read* (GET requests);
- every command is sent because the user clicked something, exactly once,
  and never replayed after a lost connection;
- nothing here reacts to a lost connection by stopping, pausing or cancelling.

Everything in this module is free of Tk: the status is polled on a thread of
its own and commands run on another, and the GUI picks up their results with
Link.drain() and Link.snapshot() (see control_tab).
"""
import bisect
import json
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

DEFAULT_ADDRESS = "192.168.0.199"
MOONRAKER_PORT = 7125  # Moonraker's own port; port 80 reaches it through Mainsail's web server
POLL_SECONDS = 0.25    # status refresh while connected
RETRY_SECONDS = 1.0    # between attempts while the machine can't be reached
REQUEST_TIMEOUT = 3.0  # one status request (s)
# A G-code script request only returns once Klipper has run the script, so
# homing or a long travel move takes as long as the motion itself.
SCRIPT_TIMEOUT = 900.0
TRANSFER_TIMEOUT = 60.0  # per read/write of a file upload or download

# The calibration laser sits on the X carriage this far toward -X from the
# eye: with the carriage at X, its dot lands at X - LASER_OFFSET_X.
LASER_OFFSET_X = 295.0
# Feed rates (mm/min, deg/min for A) of the moves the Control tab makes.
JOG_FEED = {"X": 3000.0, "Y": 1200.0, "A": 3600.0}
TRAVEL_FEED_X = 18000.0  # calibration's travel along the tank (300 mm/s; the machine allows 1000)
RETRACT_FEED_Y = 1200.0

# Turning A on and on (the Control tab's Rotate) is a file the machine runs,
# like a wind: so PAUSE / RESUME / CANCEL work on it from anywhere (this app,
# Mainsail, the machine's own buttons), and it doesn't depend on the PC. It's
# made of short moves (ROTATE_MOVE_SECONDS each), so it pauses and stops as
# promptly as a wind does, and it runs for ROTATE_HOURS at most. The angle is
# declared 0 first, the way every wind file does it: G92 changes only A's
# G-code coordinate. (SET_KINEMATIC_POSITION is a diagnostic command for X/Y/Z,
# which would also redefine the other axes' positions.)
ROTATE_FEED = 5500.0       # deg/min
ROTATE_MOVE_SECONDS = 0.5  # one move of the file, like the winds' default Max Move Time
ROTATE_HOURS = 24
ROTATE_PREFIX = "cfrp_rotate_"  # the rotation files' names on the machine
ZERO_A = "G92 A0"
# The steppers "X/Y off" disables (SET_STEPPER_ENABLE), as named in printer.cfg.
XY_STEPPERS = ("stepper_x", "stepper_y")

# The print states (Klipper's print_stats.state) in which a file is running:
# printing, or paused part-way.
ACTIVE_STATES = ("printing", "paused")

# What the status poll asks for, as Moonraker's query string.
STATUS_QUERY = ("webhooks&print_stats&virtual_sdcard&toolhead=position,homed_axes"
                "&motion_report=live_position&display_status=message&idle_timeout=state")


class MoonrakerError(Exception):
    """Moonraker answered, but refused or failed the request (`code` is the
    HTTP status)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MachineStatus:
    """One snapshot of the machine, as the status poll read it."""
    klippy_state: str = "disconnected"  # ready / startup / shutdown / error / disconnected
    state_message: str = ""
    print_state: str = "standby"        # standby / printing / paused / complete / cancelled / error
    filename: str = ""                  # the file printing (or last printed), relative to gcodes/
    print_duration: float = 0.0
    file_position: int = 0              # bytes of the file Klipper has read so far
    file_size: int = 0
    progress: float = 0.0
    position: tuple = ()                # the toolhead's commanded position (X, Y, ...)
    live_position: tuple = ()           # its estimated actual position
    homed_axes: str = ""
    message: str = ""                   # the M117 status line
    idle_state: str = ""

    @property
    def ready(self):
        return self.klippy_state == "ready"

    @property
    def active(self):
        """A file is running (printing or paused)."""
        return self.print_state in ACTIVE_STATES

    @property
    def can_jog(self):
        # Never while a file runs: Klipper would slip the jog in between the
        # file's own moves. Paused is fine -- RESUME returns to where it paused.
        return self.ready and self.print_state != "printing"

    @property
    def idle(self):
        """Ready, with no file running: free for homing, calibration, a new wind."""
        return self.ready and not self.active

    def homed(self, axes="xy"):
        return all(a in self.homed_axes.lower() for a in axes)


def parse_status(status):
    """A MachineStatus from Moonraker's objects-query `status` dict; any
    object or field missing from it keeps its default."""
    def obj(name):
        value = status.get(name)
        return value if isinstance(value, dict) else {}

    def num(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def vector(value):
        try:
            return tuple(float(v) for v in value)
        except (TypeError, ValueError):
            return ()

    webhooks, stats, sd = obj("webhooks"), obj("print_stats"), obj("virtual_sdcard")
    return MachineStatus(
        klippy_state=str(webhooks.get("state", "ready")),
        state_message=str(webhooks.get("state_message", "") or ""),
        print_state=str(stats.get("state", "standby") or "standby"),
        filename=str(stats.get("filename", "") or ""),
        print_duration=num(stats.get("print_duration")),
        file_position=int(num(sd.get("file_position"))),
        file_size=int(num(sd.get("file_size"))),
        progress=num(sd.get("progress")),
        position=vector(obj("toolhead").get("position", ())),
        live_position=vector(obj("motion_report").get("live_position", ())),
        homed_axes=str(obj("toolhead").get("homed_axes", "") or ""),
        message=str(obj("display_status").get("message", "") or ""),
        idle_state=str(obj("idle_timeout").get("state", "") or ""),
    )


def base_urls(address):
    """The base URLs to try for a machine address the user typed: as given
    with a scheme or port, otherwise through the web server (port 80, where
    Mainsail passes the API on to Moonraker) and then Moonraker's own port."""
    address = address.strip().rstrip("/")
    if not address:
        return []
    if "://" in address:
        return [address]
    host = address.split("/", 1)[0]
    if ":" in host:
        return [f"http://{address}"]
    return [f"http://{address}", f"http://{address}:{MOONRAKER_PORT}"]


def _error_message(error):
    # Moonraker's own explanation from an HTTP error's JSON body, if it has one.
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        message = body["error"]["message"]
    except Exception:
        message = error.reason if isinstance(error.reason, str) else str(error)
    if error.code in (401, 403):
        message = (f"{message} -- Moonraker doesn't trust this PC: add its network (e.g. 192.168.0.0/24) "
                   f"to trusted_clients in moonraker.conf's [authorization] section.")
    return str(message)


def describe(error):
    """A connection error as a short sentence for the user."""
    if isinstance(error, MoonrakerError):
        return str(error)
    if isinstance(error, urllib.error.URLError):
        error = error.reason if isinstance(error.reason, Exception) else OSError(str(error.reason))
    if isinstance(error, TimeoutError):
        return "The machine didn't answer in time."
    if isinstance(error, ValueError):
        return "Something answers at that address, but it isn't Moonraker."
    if isinstance(error, OSError):
        return f"Can't reach the machine: {error.strerror or error}"
    return str(error)


class MoonrakerClient:
    """Moonraker's HTTP API at one base URL. Stateless, so it's safe to use
    from several threads at once."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    @classmethod
    def find(cls, address, timeout=REQUEST_TIMEOUT):
        """The client for the first of base_urls(address) where Moonraker
        answers. Raises the last attempt's error if none does."""
        error = OSError("No address given")
        for base in base_urls(address):
            client = cls(base)
            try:
                client.request("GET", "/server/info", timeout=timeout)
                return client
            except (OSError, ValueError, MoonrakerError) as e:
                error = e
        raise error

    # --- Requests ---

    def request(self, method, path, params=None, body=None, headers=None, timeout=REQUEST_TIMEOUT, raw_query=None,
                wrapped=True):
        """Sends one request and returns its JSON "result" (the whole answer
        for one Moonraker sends unwrapped -- a file upload's). Raises
        MoonrakerError when Moonraker refuses it, OSError when it can't be
        reached, ValueError for an answer that isn't Moonraker's."""
        url = self.base + path
        query = raw_query or (urllib.parse.urlencode(params) if params else "")
        if query:
            url += "?" + query
        data, all_headers = None, dict(headers or {})
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        elif body is not None:
            data = body
        req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            raise MoonrakerError(_error_message(e), e.code) from None
        answer = json.loads(payload.decode("utf-8"))
        if not isinstance(answer, dict) or (wrapped and "result" not in answer):
            raise ValueError("Not a Moonraker answer")
        return answer["result"] if wrapped else answer

    def status(self):
        result = self.request("GET", "/printer/objects/query", raw_query=STATUS_QUERY)
        return parse_status(result.get("status", {}))

    def server_info(self):
        return self.request("GET", "/server/info")

    def gcode(self, script, timeout=SCRIPT_TIMEOUT):
        """Runs a G-code script; returns once Klipper has run all of it."""
        return self.request("POST", "/printer/gcode/script", body={"script": script}, timeout=timeout)

    def start(self, filename):
        return self.request("POST", "/printer/print/start", params={"filename": filename})

    def pause(self):
        return self.request("POST", "/printer/print/pause", timeout=30)

    def resume(self):
        return self.request("POST", "/printer/print/resume", timeout=30)

    def cancel(self):
        return self.request("POST", "/printer/print/cancel", timeout=30)

    def emergency_stop(self):
        # Moonraker's own endpoint, not an M112 in a script: a script waits its
        # turn in the G-code queue, this doesn't.
        return self.request("POST", "/printer/emergency_stop")

    def firmware_restart(self):
        return self.request("POST", "/printer/firmware_restart", timeout=30)

    # --- Files ---

    def upload(self, path, name, on_progress=None):
        """Uploads the local file at `path` to the machine's gcodes folder as
        `name`. `on_progress`, if given, gets the share sent so far (0-1);
        anything it raises stops the upload (the machine then discards it)."""
        boundary = uuid.uuid4().hex
        safe_name = name.replace('"', "'")
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\ngcodes\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        size = os.path.getsize(path)

        def body():
            yield head
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
                    sent += len(chunk)
                    if on_progress is not None:
                        on_progress(sent / max(1, size))
            yield tail

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(head) + size + len(tail))}
        # (Moonraker answers an upload with {"item": ..., "action": ...} as it
        # is, not wrapped in "result" like every other answer.)
        answer = self.request("POST", "/server/files/upload", body=body(), headers=headers, timeout=TRANSFER_TIMEOUT,
                              wrapped=False)
        if "item" not in answer:
            raise ValueError("Not a Moonraker answer")
        return answer

    def download(self, name, dest, on_progress=None):
        """Downloads `name` from the machine's gcodes folder to `dest`
        (written next to it first and only moved into place once complete)."""
        url = f"{self.base}/server/files/gcodes/{urllib.parse.quote(name)}"
        tmp = dest + ".part"
        try:
            with urllib.request.urlopen(url, timeout=TRANSFER_TIMEOUT) as response, open(tmp, "wb") as f:
                total, got = int(response.headers.get("Content-Length") or 0), 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if on_progress is not None and total:
                        on_progress(got / total)
            os.replace(tmp, dest)
        except urllib.error.HTTPError as e:
            raise MoonrakerError(_error_message(e), e.code) from None
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# --- Turning A on and on ---

def rotation_filename(feed, direction):
    return f"{ROTATE_PREFIX}A{'pos' if direction > 0 else 'neg'}_F{feed:g}.gcode"


def parse_rotation(filename):
    """(feed, direction) of a rotation file's name, or None for any other file."""
    match = re.fullmatch(re.escape(ROTATE_PREFIX) + r"A(pos|neg)_F([0-9.]+)\.gcode", os.path.basename(filename or ""))
    if not match:
        return None
    return float(match.group(2)), 1 if match.group(1) == "pos" else -1


def write_rotation_file(path, feed, direction, hours=ROTATE_HOURS, move_seconds=ROTATE_MOVE_SECONDS):
    """Writes the program that turns A (`direction` +1 or -1) at `feed`
    deg/min for `hours`: A declared 0, then short absolute moves (absolute,
    so a cancel can't leave the machine in relative mode)."""
    step = direction * feed / 60.0 * move_seconds
    moves = max(1, round(hours * 3600 / move_seconds))
    sign = "A+" if direction > 0 else "A-"
    with open(path, "w", newline="\n") as f:
        f.write(f"; CFRP Winder: endless rotation, {sign} at {feed:g} deg/min, up to {hours:g} h\n")
        f.write(f"G90\n{ZERO_A}\nM117 Rotating {sign} at {feed:g} deg/min\n")
        f.write(f"G1 A{step:.3f} F{feed:g}\n")
        f.writelines(f"G1 A{k * step:.3f}\n" for k in range(2, moves + 1))
        f.write("M117 Rotation finished\n")


# --- Where in the file the machine is ---

def move_offsets(path):
    """The byte offset where each move line of a G-code file starts, in the
    order (and by the same rule) view_tab.read_gcode() counts moves -- so
    Klipper's file position, a byte count, can be told as a move index."""
    offsets, pos = [], 0
    with open(path, "rb") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith(b";") and not line.startswith(b"G92") and (
                    line.startswith(b"G1") or line.startswith(b"G0")):
                offsets.append(pos)
            pos += len(raw)
    return offsets


def queued_move(offsets, file_position):
    """Index of the last move Klipper has read from the file (-1 before the
    first): every move whose line starts before its file position."""
    return bisect.bisect_left(offsets, file_position) - 1


# Klipper reads a little ahead of the motion (up to ~2 s). The move being
# executed is looked for among the moves read in the last LOOKBACK_SECONDS of
# program time; where several fit the carriage's actual position (a dwell,
# where only the mandrel turns), the one EXPECTED_LAG behind the reading wins.
LOOKBACK_SECONDS = 4.0
EXPECTED_LAG = 1.0
MATCH_TOLERANCE = 1.0  # mm
MAX_MATCH_DISTANCE = 25.0  # mm: further from every recent move than this, the match is meaningless


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def live_move(commands, times, queued, live_xy):
    """The move the machine is most likely executing: among the moves just
    read (up to `queued`), the one whose X/Y path passes through the
    carriage's actual position `live_xy`. `commands` and `times` are
    read_gcode()'s. Without a usable position it's the move last read."""
    if queued < 0 or not commands:
        return queued
    queued = min(queued, len(commands) - 1)
    if len(live_xy) < 2:
        return queued
    x, y = live_xy[0], live_xy[1]
    t_read = times[queued]
    candidates = []
    k = queued
    while k >= 0 and t_read - times[k] <= LOOKBACK_SECONDS:
        a = commands[k - 1] if k > 0 else commands[k]
        b = commands[k]
        candidates.append((_segment_distance(x, y, a[0], a[1], b[0], b[1]), k))
        k -= 1
    best = min(d for d, _ in candidates)
    if best > MAX_MATCH_DISTANCE:
        return queued
    near = [k for d, k in candidates if d <= best + MATCH_TOLERANCE]
    target = t_read - EXPECTED_LAG
    return min(near, key=lambda k: (abs(times[k] - target), -k))


# --- Tank calibration ---

def straight_section(job):
    """(start, end): machine X of the planned straight section's two ends."""
    start = job.chuck_offset + job.dome_length
    return start, start + job.straight_length


def calibrated_settings(job, start, end):
    """{chuck_offset, straight_length} for a straight section measured from
    X `start` to X `end`, on `job`'s tank (its domes set where the tank
    itself begins). Raises ValueError for ends that make no tank."""
    if not end > start:
        raise ValueError("The far end must lie beyond the chuck-side end.")
    return {"chuck_offset": start - job.dome_length, "straight_length": end - start}


# --- The connection ---

class Link:
    """The app's connection to the machine. Polls the status on a thread of
    its own -- reconnecting by itself, read-only -- and runs commands one at
    a time on another (or, `background`, each on a thread of its own: the
    emergency stop must never wait behind a slow jog). Results come back to
    the GUI thread through drain()."""

    OFFLINE, CONNECTING, ONLINE, LOST = "offline", "connecting", "online", "lost"

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._state, self._status, self._error = self.OFFLINE, None, ""
        self._client = None
        self.address = ""
        self._commands = queue.Queue()
        self._results = queue.Queue()
        self._pending = 0
        threading.Thread(target=self._command_loop, daemon=True).start()

    # --- Connection (GUI thread) ---

    def connect(self, address):
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.address = address
            self._state, self._status, self._error, self._client = self.CONNECTING, None, "", None
        threading.Thread(target=self._poll_loop, args=(generation, address), daemon=True).start()

    def disconnect(self):
        with self._lock:
            self._generation += 1
            self._state, self._status, self._error, self._client = self.OFFLINE, None, "", None

    def snapshot(self):
        """(connection state, latest MachineStatus or None, error text)."""
        with self._lock:
            return self._state, self._status, self._error

    @property
    def client(self):
        with self._lock:
            return self._client if self._state == self.ONLINE else None

    @property
    def pending(self):
        """Commands waiting or running on the command thread."""
        with self._lock:
            return self._pending

    # --- Commands (GUI thread) ---

    def run(self, work, on_done=None, on_error=None, background=False, max_pending=None):
        """Runs work(client) off the GUI thread; on_done(result) or
        on_error(exception) are then called by drain(). Returns False (and
        runs nothing) while not connected, or with `max_pending` commands
        already queued."""
        client = self.client
        if client is None:
            return False
        if background:
            threading.Thread(target=self._execute, args=(work, client, on_done, on_error, False), daemon=True).start()
            return True
        with self._lock:
            if max_pending is not None and self._pending >= max_pending:
                return False
            self._pending += 1
        self._commands.put((work, client, on_done, on_error))
        return True

    def drain(self):
        """Calls the callbacks of every command finished since the last call
        (on the GUI thread)."""
        while True:
            try:
                callback, value = self._results.get_nowait()
            except queue.Empty:
                return
            if callback is not None:
                callback(value)

    # --- Threads ---

    def _execute(self, work, client, on_done, on_error, counted):
        try:
            result = work(client)
            self._results.put((on_done, result))
        except Exception as e:
            self._results.put((on_error, e))
        finally:
            if counted:
                with self._lock:
                    self._pending -= 1

    def _command_loop(self):
        while True:
            work, client, on_done, on_error = self._commands.get()
            self._execute(work, client, on_done, on_error, True)

    def _set(self, generation, **values):
        with self._lock:
            if generation != self._generation:
                return False
            for key, value in values.items():
                setattr(self, "_" + key, value)
            return True

    def _poll_loop(self, generation, address):
        client, seen = None, False
        while True:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                if client is None:
                    client = MoonrakerClient.find(address)
                try:
                    status = client.status()
                except MoonrakerError as e:
                    # Moonraker is there, Klipper isn't (yet): starting up,
                    # restarting, or not connected to it.
                    info = client.server_info()
                    status = MachineStatus(klippy_state=str(info.get("klippy_state", "disconnected")),
                                           state_message=str(e))
                seen = True
                if not self._set(generation, state=self.ONLINE, status=status, error="", client=client):
                    return
                time.sleep(POLL_SECONDS)
            except (OSError, ValueError, MoonrakerError) as e:
                if not self._set(generation, state=self.LOST if seen else self.CONNECTING, error=describe(e)):
                    return
                time.sleep(RETRY_SECONDS)
