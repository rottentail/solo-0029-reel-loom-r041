"""Application lifecycle tests with stubbed media execution.

Exercises the phase gate (starting/ready/draining/stopped), rejection of
new work during shutdown, the ordered drain and the recovery contract for
a ticket interrupted mid-render.  SQLite and the real application wiring
are used; only the FFmpeg binary is stubbed, so no media toolchain is
required.
"""
import json
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reel_loom.config import Settings
from reel_loom.application import Application, PHASE_READY, PHASE_DRAINING, PHASE_STOPPED
from reel_loom.api import Api
from reel_loom.http import MediaHttpServer
from reel_loom.errors import LoomError


def write_stub(root, name, sleep=30):
    stub = Path(root) / name
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import time\n"
        f"time.sleep({sleep})\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def make_app(root, **overrides):
    stub = write_stub(root, "stub-ffmpeg")
    settings = Settings(root=Path(root), ffmpeg=str(stub), workers=1,
                        render_timeout=20, shutdown_timeout=8, **overrides)
    return Application(settings, start_workers=False)


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="loom-app-")
        self.app = make_app(self.tmp.name)

    def tearDown(self):
        try:
            self.app.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_starting_phase_rejects_work_until_marked_ready(self):
        # The walkthrough/test entry point starts without preflight and must
        # not accept work before the readiness transition.
        with self.assertRaises(LoomError) as caught:
            self.app.require_accepting()
        self.assertEqual(caught.exception.status, 503)
        self.app.mark_ready()
        self.app.require_accepting()  # no raise
        self.assertEqual(self.app.health()["status"], "ready")

    def test_draining_phase_rejects_new_work_but_keeps_reads(self):
        self.app.mark_ready()
        self.app.phase = PHASE_DRAINING
        api = Api(self.app)
        with self.assertRaises(LoomError) as caught:
            api.handle("POST", "/loom/v1/tickets", {}, {}, b'{"reel":"x","look":"y"}')
        self.assertEqual(caught.exception.status, 503)
        # Read-only observation stays available.
        body = api.handle("GET", "/loom/v1/tickets", {}, {}, b"")
        self.assertEqual(body["items"], [])
        # Cancellation requests stay available so clients can release work.
        with self.assertRaises(LoomError) as caught:
            api.handle("POST", "/loom/v1/tickets/render_nope/cancel", {}, {}, b"{}")
        self.assertEqual(caught.exception.code, "not_found")

    def test_drain_releases_processes_and_marks_stopped(self):
        self.app.mark_ready()
        from reel_loom.processes import DrainInterrupted

        def slow_work(cancelled=lambda: False):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.app.processes.draining.is_set() or cancelled():
                    raise DrainInterrupted()
                time.sleep(0.02)

        ticket_id = "render_drain_probe"
        self.app.renderer._active[ticket_id] = {"kind": "render"}

        def execute(_ticket):
            try:
                slow_work()
            except DrainInterrupted:
                pass
            finally:
                self.app.renderer._active.pop(ticket_id, None)

        self.app.renderer.execute = execute
        thread = threading.Thread(target=execute, args=({"id": ticket_id},), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while ticket_id not in self.app.renderer.active_tickets() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(ticket_id, self.app.renderer.active_tickets())

        report = self.app.drain(timeout=6)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.app.phase, PHASE_STOPPED)
        self.assertTrue(report["clean"], report)
        self.assertEqual(self.app.processes.active_count(), 0)
        self.assertEqual(report["media_processes"]["remaining"], 0)
        self.assertEqual(report["before"]["phase"], PHASE_DRAINING)

    def test_interrupted_running_ticket_is_recovered_on_next_start(self):
        self.app.mark_ready()
        from reel_loom.values import now
        # Fabricate the minimum rows for a queued ticket and claim it, then
        # stop without completing: the next application must mark it failed.
        with self.app.db.write_unit() as db:
            db.execute("INSERT INTO reels(id,title,head,state,created) VALUES(?,?,?,?,?)",
                       ("reel_rec", "恢复卷", 1, "sealed", now()))
            db.execute("INSERT INTO revisions(reel,number,document,hash,created) VALUES(?,?,?,?,?)",
                       ("reel_rec", 1, "{}", "rh", now()))
            db.execute("INSERT INTO looks(id,version,spec,hash,created) VALUES(?,?,?,?,?)",
                       ("lk", 1, "{}", "lh", now()))
            db.execute(
                "INSERT INTO tickets(id,reel,revision,look_id,look_version,snapshot,fingerprint,state,attempt,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("render_rec", "reel_rec", 1, "lk", 1, "{}", "fp", "running", 1, now(), now()))
        self.app.close()

        restarted = make_app(self.tmp.name)
        try:
            ticket = restarted.tickets.get("render_rec")
            self.assertEqual(ticket["state"], "failed")
            self.assertEqual(ticket["error"]["error"], "worker_interrupted")
            self.assertEqual(restarted.recovery["interrupted"], 1)
            retried = restarted.tickets.retry("render_rec")
            self.assertEqual(retried["state"], "queued")
        finally:
            restarted.close()


class HttpDrainObservabilityTests(unittest.TestCase):
    def test_health_reports_503_with_phase_until_ready(self):
        tmp = tempfile.TemporaryDirectory(prefix="loom-http-")
        self.addCleanup(tmp.cleanup)
        app = make_app(tmp.name)
        self.addCleanup(app.close)
        server = MediaHttpServer(("127.0.0.1", 0), Api(app))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02},
                                  daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/loom/v1"
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/health", timeout=3)
            self.assertEqual(caught.exception.code, 503)
            payload = json.loads(caught.exception.read())
            self.assertEqual(payload["context"]["phase"], "starting")

            app.mark_ready()
            with urllib.request.urlopen(base + "/health", timeout=3) as response:
                ready = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(ready["phase"], PHASE_READY)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
