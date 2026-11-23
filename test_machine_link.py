import os
import tempfile
import time
import unittest

import fake_moonraker
import machine_link
import winding
from machine_link import Link, MachineStatus, MoonrakerClient, MoonrakerError
from view_tab import read_gcode


def _wait(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _small_job():
    return winding.WindingJob(straight_length=300.0, tank_diameter=120.0, end_cap_diameter=40.0,
                              layups=(winding.Layup(wind_angle=45.0, pattern_number=1, auto_cycles=False, passes=1),))


def _write_program(folder, job=None):
    # Written in text mode, as the app writes it (CRLF line ends on Windows).
    path = os.path.join(folder, "wind.gcode")
    with open(path, "w") as f:
        winding.write_gcode(f, job or _small_job())
    return path


class Addresses(unittest.TestCase):
    def test_plain_address_tries_web_server_then_moonraker(self):
        self.assertEqual(machine_link.base_urls("192.168.0.199"),
                         ["http://192.168.0.199", "http://192.168.0.199:7125"])

    def test_port_or_scheme_is_taken_as_given(self):
        self.assertEqual(machine_link.base_urls(" 192.168.0.199:7125/ "), ["http://192.168.0.199:7125"])
        self.assertEqual(machine_link.base_urls("https://winder.local"), ["https://winder.local"])
        self.assertEqual(machine_link.base_urls(""), [])


class Status(unittest.TestCase):
    def test_missing_objects_keep_defaults(self):
        status = machine_link.parse_status({"print_stats": {"state": "printing", "filename": "a.gcode"}})
        self.assertEqual(status.print_state, "printing")
        self.assertEqual(status.filename, "a.gcode")
        self.assertEqual(status.position, ())
        self.assertTrue(status.ready and status.active and not status.can_jog and not status.idle)

    def test_what_each_state_allows(self):
        paused = MachineStatus(klippy_state="ready", print_state="paused")
        self.assertTrue(paused.can_jog)
        self.assertFalse(paused.idle)
        done = MachineStatus(klippy_state="ready", print_state="complete")
        self.assertTrue(done.idle and done.can_jog)
        shutdown = MachineStatus(klippy_state="shutdown", print_state="standby")
        self.assertFalse(shutdown.can_jog or shutdown.idle)
        self.assertTrue(MachineStatus(homed_axes="xyz").homed("xy"))
        self.assertFalse(MachineStatus(homed_axes="x").homed("xy"))


class FilePosition(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = _write_program(self.folder)

    def test_offsets_line_up_with_the_viewer_moves(self):
        data = read_gcode(self.path)
        offsets = machine_link.move_offsets(self.path)
        self.assertEqual(len(offsets), len(data.commands))
        with open(self.path, "rb") as f:
            raw = f.read()
        for i in (0, 1, len(offsets) // 2, len(offsets) - 1):
            line = raw[offsets[i]:raw.index(b"\n", offsets[i])].decode()
            self.assertTrue(line.startswith(("G1", "G0")), line)
            x = float(line.split("X", 1)[1].split()[0]) if "X" in line else None
            if x is not None:
                self.assertAlmostEqual(x, data.commands[i][0], places=3)

    def test_queued_move_counts_the_lines_read(self):
        offsets = [10, 20, 30]
        self.assertEqual(machine_link.queued_move(offsets, 0), -1)
        self.assertEqual(machine_link.queued_move(offsets, 10), -1)  # that line is being read, not yet done
        self.assertEqual(machine_link.queued_move(offsets, 11), 0)
        self.assertEqual(machine_link.queued_move(offsets, 30), 1)
        self.assertEqual(machine_link.queued_move(offsets, 999), 2)

    def test_live_move_follows_the_carriage_behind_the_reading(self):
        # A straight pass along X, 1 s per 10 mm move.
        commands = [(10.0 * i, 50.0, 0.0, 0.0) for i in range(20)]
        times = [float(i) for i in range(20)]
        self.assertEqual(machine_link.live_move(commands, times, 15, (125.0, 50.0)), 13)
        # Without a position: the move last read.
        self.assertEqual(machine_link.live_move(commands, times, 15, ()), 15)
        # Nowhere near the moves just read (parked, say): the move last read.
        self.assertEqual(machine_link.live_move(commands, times, 15, (125.0, 150.0)), 15)
        self.assertEqual(machine_link.live_move(commands, times, -1, (0.0, 50.0)), -1)

    def test_live_move_in_a_dwell_takes_the_expected_lag(self):
        # The carriage stands still while the mandrel turns: every move fits.
        commands = [(100.0, 50.0, 10.0 * i, 0.0) for i in range(10)]
        times = [0.5 * i for i in range(10)]
        self.assertEqual(machine_link.live_move(commands, times, 9, (100.0, 50.0)), 7)  # 1 s behind


class Calibration(unittest.TestCase):
    def test_planned_section_round_trips(self):
        job = _small_job()
        start, end = machine_link.straight_section(job)
        self.assertAlmostEqual(start, job.chuck_offset + job.dome_length)
        values = machine_link.calibrated_settings(job, start, end)
        self.assertAlmostEqual(values["chuck_offset"], job.chuck_offset)
        self.assertAlmostEqual(values["straight_length"], job.straight_length)

    def test_measured_section_moves_the_tank(self):
        job = _small_job()
        values = machine_link.calibrated_settings(job, 200.0, 512.5)
        self.assertAlmostEqual(values["straight_length"], 312.5)
        self.assertAlmostEqual(values["chuck_offset"], 200.0 - job.dome_length)
        flat = winding.WindingJob(end_cap_type="Flat")
        self.assertEqual(machine_link.calibrated_settings(flat, 80.0, 900.0)["chuck_offset"], 80.0)

    def test_ends_the_wrong_way_round_are_refused(self):
        with self.assertRaises(ValueError):
            machine_link.calibrated_settings(_small_job(), 500.0, 400.0)


class Client(unittest.TestCase):
    def setUp(self):
        self.machine = fake_moonraker.FakeMachine(speed=200.0)
        self.server = fake_moonraker.serve(self.machine)
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        self.client = MoonrakerClient.find(self.address)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_jog_home_and_refusals(self):
        with self.assertRaises(MoonrakerError) as e:
            self.client.gcode("G1 X10")
        self.assertIn("home", str(e.exception))
        self.client.gcode("G28 X Y")
        self.client.gcode("G91\nG1 X12.5 F3000\nG90")
        self.assertAlmostEqual(self.client.status().position[0], 12.5)
        with self.assertRaises(MoonrakerError) as e:
            self.client.gcode("SOMETHING_UNKNOWN")
        self.assertEqual(e.exception.code, 400)
        self.client.gcode("LASER_ON")
        self.assertTrue(self.machine.laser)

    def test_upload_download_and_a_wind(self):
        folder = tempfile.mkdtemp()
        path = _write_program(folder)
        shares = []
        self.client.upload(path, "wind.gcode", shares.append)
        self.assertEqual(shares[-1], 1.0)
        copy = os.path.join(folder, "copy.gcode")
        self.client.download("wind.gcode", copy)
        with open(path, "rb") as a, open(copy, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.client.start("wind.gcode")
        status = self.client.status()
        self.assertEqual(status.filename, "wind.gcode")
        self.assertIn(status.print_state, ("printing", "paused"))
        # The program pauses for the fiber after the move to the start.
        self.assertTrue(_wait(lambda: self.client.status().print_state == "paused"))
        self.client.resume()
        self.assertTrue(_wait(lambda: self.client.status().print_state == "complete", 20))
        with self.assertRaises(MoonrakerError):
            self.client.download("missing.gcode", copy)

    def test_emergency_stop_shuts_klipper_down(self):
        self.client.emergency_stop()
        status = self.client.status()
        self.assertEqual(status.klippy_state, "shutdown")
        self.assertFalse(status.ready)
        self.client.firmware_restart()
        self.assertTrue(self.client.status().ready)

    def test_motors_off(self):
        self.client.gcode("G28")
        self.client.gcode("\n".join(f"SET_STEPPER_ENABLE STEPPER={n} ENABLE=0" for n in machine_link.XY_STEPPERS))
        self.assertEqual(self.machine.motors, {"stepper_a"})
        self.assertEqual(self.client.status().homed_axes, "a")
        self.client.gcode("M84")
        self.assertEqual(self.machine.motors, set())

    def test_rotation_file(self):
        path = os.path.join(tempfile.mkdtemp(), "r.gcode")
        machine_link.write_rotation_file(path, 6000.0, -1, hours=0.01, move_seconds=0.5)
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[1:3], ["G90", "G92 A0"])
        moves = [l for l in lines if l.startswith("G1")]
        self.assertEqual(len(moves), 72)  # 36 s of 0.5 s moves
        self.assertEqual(moves[0], "G1 A-50.000 F6000")  # 6000 deg/min for 0.5 s, A-
        self.assertEqual(moves[-1], "G1 A-3600.000")      # absolute: a cancel can't leave G91 behind
        name = machine_link.rotation_filename(6000.0, -1)
        self.assertEqual(machine_link.parse_rotation(name), (6000.0, -1))
        self.assertIsNone(machine_link.parse_rotation("wind.gcode"))

    def test_rotation_pauses_resumes_and_stops_like_a_wind(self):
        # It's a file the machine runs: the standard print controls work on it.
        path = os.path.join(tempfile.mkdtemp(), "r.gcode")
        name = machine_link.rotation_filename(5500.0, 1)
        machine_link.write_rotation_file(path, 5500.0, 1, hours=0.5)
        self.client.upload(path, name)
        self.client.start(name)
        self.assertTrue(_wait(lambda: self.machine.position[3] > 1000))
        self.client.pause()
        self.assertEqual(self.client.status().print_state, "paused")
        held = self.machine.position[3]
        time.sleep(0.3)
        self.assertEqual(self.machine.position[3], held)
        self.client.resume()
        self.assertTrue(_wait(lambda: self.machine.position[3] > held + 100))
        self.client.cancel()
        self.assertEqual(self.client.status().print_state, "cancelled")

    def test_nothing_there(self):
        with self.assertRaises(OSError):
            MoonrakerClient.find("127.0.0.1:1", timeout=0.5)


class Connection(unittest.TestCase):
    def setUp(self):
        self.machine = fake_moonraker.FakeMachine(speed=1.0)
        self.server = fake_moonraker.serve(self.machine)
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        self.link = Link()

    def tearDown(self):
        self.link.disconnect()
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass

    def test_watching_never_sends_a_command(self):
        self.link.connect(self.address)
        self.assertTrue(_wait(lambda: self.link.snapshot()[0] == Link.ONLINE))
        time.sleep(1.0)  # a few polls
        self.assertGreater(len(self.machine.requests), 3)
        self.assertEqual([r for r in self.machine.requests if r[0] != "GET"], [])

    def test_commands_come_back_through_drain(self):
        self.assertFalse(self.link.run(lambda c: c.gcode("G28")))  # not connected: nothing runs
        self.link.connect(self.address)
        self.assertTrue(_wait(lambda: self.link.client is not None))
        results = []
        self.assertTrue(self.link.run(lambda c: c.gcode("G28"), on_done=results.append))
        self.assertTrue(self.link.run(lambda c: c.gcode("BOGUS"), on_error=results.append))
        self.assertTrue(_wait(lambda: (self.link.drain(), len(results) == 2)[1]))
        self.assertEqual(results[0], "ok")
        self.assertIsInstance(results[1], MoonrakerError)
        self.assertEqual(self.link.pending, 0)

    def test_a_lost_connection_is_retried_without_sending_anything(self):
        self.link.connect(self.address)
        self.assertTrue(_wait(lambda: self.link.snapshot()[0] == Link.ONLINE))
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(_wait(lambda: self.link.snapshot()[0] == Link.LOST))
        state, status, error = self.link.snapshot()
        self.assertIsNotNone(status)  # the last known status stays
        self.assertIsNone(self.link.client)  # but nothing can be sent
        self.assertEqual([r for r in self.machine.requests if r[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
