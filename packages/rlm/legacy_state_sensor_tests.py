#!/usr/bin/env python3
"""Offline controls for the private legacy-state inotify sensor."""
import importlib.util
import shutil
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("legacy_state_sensor", ROOT / "legacy_state_sensor.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("legacy-state sensor module is unavailable")
sensor_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sensor_module)


class LegacyStateSensorTests(unittest.TestCase):
    roots: ClassVar[set[str]] = set()

    def fixture(self):
        directory = tempfile.mkdtemp(prefix="pi-legacy-state-sensor-")
        self.addCleanup(self._cleanup, directory)
        self.assertNotIn(directory, self.roots)
        self.roots.add(directory)
        root = Path(directory)
        calibration = root / "calibration"
        legacy = root / "kernel.bin"
        calibration.write_bytes(b"calibrate")
        legacy.write_bytes(b"legacy-state")
        return root, calibration, legacy

    def _cleanup(self, directory):
        shutil.rmtree(directory, ignore_errors=True)
        self.assertFalse(Path(directory).exists(), "private sensor fixture survived cleanup")

    def calibrated(self):
        root, calibration, legacy = self.fixture()
        sensor = sensor_module.LegacyStateSensor([legacy], calibration)
        self.addCleanup(sensor.close)
        counts = sensor.calibrate()
        self.assertGreater(counts[calibration]["access"], 0)
        return root, calibration, legacy, sensor

    def test_calibration_success(self):
        _root, calibration, legacy, sensor = self.calibrated()
        counts = sensor.final_drain()
        self.assertGreater(counts[calibration]["access"], 0)
        self.assertEqual(counts[legacy], {"access": 0, "write": 0, "delete": 0})

    def test_unavailable_setup(self):
        _root, calibration, legacy = self.fixture()

        class Unavailable:
            def init(self):
                raise OSError("inotify unavailable")

        with self.assertRaisesRegex(OSError, "inotify unavailable"):
            sensor_module.LegacyStateSensor([legacy], calibration, api=Unavailable())

        class SetupApi:
            def __init__(self):
                self.closed: list[int] = []

            def init(self):
                return 123

            def close(self, fd: int):
                self.closed.append(fd)

        def failing_paths():
            yield legacy
            raise RuntimeError("path iteration failed")

        for label, paths, error in (
            ("invalid-path", [legacy, object()], TypeError),
            ("failing-iterable", failing_paths(), RuntimeError),
        ):
            with self.subTest(control=label):
                api = SetupApi()
                with self.assertRaises(error):
                    sensor_module.LegacyStateSensor(paths, calibration, api=api)
                self.assertEqual(api.closed, [123])

    def test_calibration_missing_event(self):
        _root, calibration, legacy = self.fixture()
        sensor = sensor_module.LegacyStateSensor([legacy], calibration)
        self.addCleanup(sensor.close)
        with self.assertRaisesRegex(RuntimeError, "calibration access missing"):
            sensor.calibrate(read_calibration=False)

    def test_cumulative_access(self):
        _root, calibration, legacy, sensor = self.calibrated()
        legacy.read_bytes()
        first = sensor.drain()
        legacy.read_bytes()
        final = sensor.final_drain()
        self.assertGreater(first[legacy]["access"], 0)
        self.assertGreater(final[legacy]["access"], first[legacy]["access"])
        self.assertGreaterEqual(final[calibration]["access"], 1)

    def test_read_only_mutant_access_despite_equal_bytes_and_mode(self):
        _root, calibration, legacy = self.fixture()
        before = (legacy.read_bytes(), stat.S_IMODE(legacy.stat().st_mode))
        sensor = sensor_module.LegacyStateSensor([legacy], calibration)
        self.addCleanup(sensor.close)
        sensor.calibrate()
        baseline = sensor.drain()
        observed = legacy.read_bytes()
        after = (observed, stat.S_IMODE(legacy.stat().st_mode))
        counts = sensor.final_drain()
        self.assertEqual(after, before)
        self.assertGreater(counts[legacy]["access"], baseline[legacy]["access"])

    def test_write_mutant_category_and_integrity(self):
        _root, _calibration, legacy, sensor = self.calibrated()
        before = (legacy.read_bytes(), stat.S_IMODE(legacy.stat().st_mode))
        with legacy.open("r+b") as stream:
            stream.write(b"mutant")
        counts = sensor.final_drain()
        after = (legacy.read_bytes(), stat.S_IMODE(legacy.stat().st_mode))
        self.assertNotEqual(after, before)
        self.assertGreater(counts[legacy]["write"], 0)

    def test_delete_and_move_mutant_category_and_integrity(self):
        for operation in ("delete", "move"):
            with self.subTest(operation=operation):
                _root, _calibration, legacy, sensor = self.calibrated()
                before = (legacy.read_bytes(), stat.S_IMODE(legacy.stat().st_mode))
                if operation == "delete":
                    legacy.unlink()
                else:
                    legacy.rename(legacy.with_suffix(".moved"))
                counts = sensor.final_drain()
                self.assertFalse(legacy.exists())
                self.assertGreater(counts[legacy]["delete"], 0)
                self.assertEqual(before[0], b"legacy-state")

    def test_final_drain_and_no_drain_control(self):
        _root, _calibration, legacy, sensor = self.calibrated()
        legacy.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "final drain required"):
            sensor.final_counts()
        final = sensor.final_drain()
        self.assertGreater(final[legacy]["access"], 0)
        self.assertEqual(sensor.final_counts(), final)

        class ScriptedApi:
            def __init__(self, event):
                self.event = event
                self.next_watch = 1

            def init(self):
                return 123

            def add_watch(self, _fd, _path):
                watch = self.next_watch
                self.next_watch += 1
                return watch

            def read(self, _fd):
                if isinstance(self.event, BaseException):
                    raise self.event
                event, self.event = self.event, BlockingIOError()
                return event

            def close(self, _fd):
                pass

        event = lambda wd, mask: struct.pack("iIII", wd, mask, 0, 0)
        for label, scripted, message in (
            ("final-drain", b"", "inotify final drain failed"),
            ("overflow", event(-1, sensor_module.IN_Q_OVERFLOW), "inotify queue overflow"),
            ("unknown-watch", event(3, sensor_module.IN_ACCESS), "unknown inotify watch"),
        ):
            with self.subTest(control=label):
                _root, calibration, legacy = self.fixture()
                control = sensor_module.LegacyStateSensor([legacy], calibration, api=ScriptedApi(scripted))
                self.addCleanup(control.close)
                with self.assertRaisesRegex(RuntimeError, message):
                    control.final_drain()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LegacyStateSensorTests)
    if suite.countTestCases() != 8:
        raise SystemExit(f"expected exactly 8 legacy sensor controls, found {suite.countTestCases()}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit(1)
    print("legacy sensor controls: 8/8; fresh fixtures; calibration and access/write/delete/no-drain mutants rejected.")
