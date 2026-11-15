import logging
import threading
import time
from .errors import LoomError
from .probe import inspect_media
from .timebase import seconds, TICKS_PER_SECOND

logger = logging.getLogger(__name__)


class Renderer:
    def __init__(self, settings, blobs, tickets, planner, processes, artifacts):
        self.settings, self.blobs, self.tickets = settings, blobs, tickets
        self.planner, self.processes, self.artifacts = planner, processes, artifacts
        self.stopping = threading.Event()
        self.threads = []
        self._active_lock = threading.Lock()
        self._active = {}

    def execute(self, ticket):
        ticket_id = ticket["id"]
        snapshot = ticket["snapshot"]
        suffix = ".mp4" if snapshot["look"]["spec"]["kind"] == "video" else ".wav"
        with self._active_lock:
            self._active[ticket_id] = {"kind": "render", "started": threading.get_ident()}
        try:
            with self.blobs.temporary(suffix) as output:
                args = self.planner.compile(snapshot, lambda key: self.blobs.resolve(key, verify=True), output)
                duration = seconds(snapshot["document"]["duration_tick"])
                self.processes.run(args,
                    cancelled=lambda: self.stopping.is_set() or self.tickets.cancellation_requested(ticket_id),
                    progress=lambda value: self.tickets.progress(ticket_id, value), duration=duration)
                metadata = inspect_media(output, self.settings)
                actual = seconds(metadata["duration_tick"])
                tolerance = max(0.15, 2/float(__import__('fractions').Fraction(snapshot["look"]["spec"]["frame_rate"])))
                if abs(actual-duration) > tolerance:
                    raise LoomError("rendition_duration", "Encoded duration differs from the frozen reel", 422,
                                    {"expected": duration, "actual": actual})
                return self.artifacts.commit_render(ticket_id, output, {**metadata, "fingerprint": ticket["fingerprint"]})
        except LoomError as exc:
            self.tickets.fail(ticket_id, exc)
            return None
        except Exception:
            logger.exception("Render failed unexpectedly: %s", ticket_id)
            self.tickets.fail(ticket_id, LoomError("internal_render", "Unexpected rendering failure", 500))
            return None
        finally:
            with self._active_lock:
                self._active.pop(ticket_id, None)

    def active_tickets(self):
        with self._active_lock:
            return list(self._active)

    def run_once(self):
        ticket = self.tickets.claim()
        if ticket is None:
            return None
        self.execute(ticket)
        return self.tickets.get(ticket["id"])

    def loop(self):
        while not self.stopping.is_set():
            if self.run_once() is None:
                self.stopping.wait(.2)

    def start(self):
        self.stopping.clear()
        for index in range(self.settings.workers):
            thread = threading.Thread(target=self.loop, name=f"loom-render-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def drain(self, timeout):
        """Stop claiming new work and release worker threads.

        Running renders observe the draining flag, terminate their FFmpeg
        processes and record a terminal failure/cancellation state, so the
        next container can retry the original frozen ticket after recovery.
        """
        self.stopping.set()
        end = time.monotonic() + max(1.0, float(timeout))
        for thread in list(self.threads):
            remaining = max(0.0, end - time.monotonic())
            thread.join(timeout=remaining)
        joined = all(not thread.is_alive() for thread in self.threads)
        report = {"worker_threads": len(self.threads), "joined": joined,
                  "active_tickets": self.active_tickets()}
        self.threads.clear()
        return report

    def close(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.threads.clear()
