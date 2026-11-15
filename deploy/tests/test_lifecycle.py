"""Stdlib-only lifecycle tests.

These tests deliberately avoid PyAV/FFmpeg: tiny generated scripts act as
stub encoders (they ignore the FFmpeg-flavoured argv) so the process
registry, drain ordering and termination contract can be verified on any
Python 3.11+ host, including CI without a media toolchain.
"""
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reel_loom.config import Settings
from reel_loom.processes import Processes, DrainInterrupted


def write_stub(root, name, ignore_term=False, sleep=30):
    stub = Path(root) / name
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"time.sleep({sleep})\n"
        "sys.exit(0)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def make_settings(root, ffmpeg=None, shutdown_timeout=10):
    binary = ffmpeg or write_stub(root, "stub-ffmpeg")
    settings = Settings(root=Path(root), ffmpeg=str(binary),
                        render_timeout=20, shutdown_timeout=shutdown_timeout)
    return settings.prepare()


class ProcessRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="loom-test-")
        self.settings = make_settings(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _start_worker(self, processes):
        """Start a stub encoder and wait until it is registered as active."""
        started = threading.Event()

        def worker():
            started.set()
            try:
                processes.run(["-i", "ignored", "out.mp4"])
            except Exception:
                pass

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        deadline = time.monotonic() + 5
        while not processes.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(processes.active_count(), 1, "stub encoder never registered")
        return thread

    def test_registry_tracks_live_processes(self):
        processes = Processes(self.settings)
        thread = self._start_worker(processes)
        snapshot = processes.active_snapshot()
        self.assertTrue(snapshot[0]["alive"])
        # Draining releases the media process and its capacity slot.
        processes.request_drain()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(processes.active_count(), 0)

    def test_drain_interrupts_new_and_running_work(self):
        processes = Processes(self.settings)
        thread = self._start_worker(processes)
        processes.request_drain()
        # New work must be refused immediately, without spawning a process.
        with self.assertRaises(DrainInterrupted):
            processes.run(["-version"])
        report = processes.stop_all(timeout=5)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(report["remaining"], 0)
        self.assertGreaterEqual(report["terminated"] + report["killed"], 1)
        self.assertEqual(processes.active_count(), 0)

    def test_stop_all_kills_processes_that_ignore_sigterm(self):
        rude = write_stub(self.tmp.name, "rude-ffmpeg", ignore_term=True)
        settings = make_settings(self.tmp.name, ffmpeg=rude, shutdown_timeout=6)
        processes = Processes(settings)
        thread = self._start_worker(processes)
        report = processes.stop_all(timeout=6)
        thread.join(timeout=6)
        self.assertEqual(report["remaining"], 0)
        self.assertGreaterEqual(report["killed"], 1)

    def test_slot_is_released_after_drain(self):
        processes = Processes(self.settings)
        processes.request_drain()
        # The capacity semaphore must not leak when work is refused.
        for _ in range(3):
            with self.assertRaises(DrainInterrupted):
                processes.run(["-version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
