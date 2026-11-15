import threading
from .database import Database
from .blobs import BlobStore
from .assets import Assets
from .reels import Reels
from .looks import Looks
from .captions import Captions
from .requests import RequestKeys
from .planner import Planner
from .processes import Processes
from .tickets import Tickets
from .artifacts import Artifacts
from .rendering import Renderer
from .analysis import AudioAnalysis
from .frames import Frames
from .imprints import Imprints
from .resources import Resources
from .instance import InstanceLock
from .batches import RenderBatches
from .conformance import Conformance
from .lineage import Lineage
from .preflight import run_preflight
from .errors import LoomError, require

# Lifecycle phases: starting -> ready -> draining -> stopped.
PHASE_STARTING = "starting"
PHASE_READY = "ready"
PHASE_DRAINING = "draining"
PHASE_STOPPED = "stopped"


class Application:
    def __init__(self, settings, start_workers=True):
        self.settings = settings.prepare()
        self.lock = InstanceLock(settings.root).acquire()
        self.renderer = None
        self.phase = PHASE_STARTING
        self.preflight_report = None
        self._drain_report = None
        self._closed = False
        self._phase_lock = threading.Lock()
        try:
            self.db = Database(settings.root)
            self.blobs = BlobStore(settings.root, self.db)
            self.keys = RequestKeys()
            self.assets = Assets(self.db, self.blobs, settings)
            self.reels = Reels(self.db, self.assets, self.keys, settings)
            self.looks = Looks(self.db)
            self.captions = Captions(self.db, self.assets, self.reels)
            self.planner = Planner(self.assets, self.reels, self.looks)
            self.processes = Processes(settings)
            self.tickets = Tickets(self.db, self.planner, self.keys)
            self.artifacts = Artifacts(self.db, self.blobs)
            self.batches = RenderBatches(self.db, self.planner, self.tickets, self.keys)
            self.conformance = Conformance(settings, self.blobs, self.tickets, self.artifacts)
            self.lineage = Lineage(self.db, self.tickets)
            self.analysis = AudioAnalysis(self.db, self.assets, self.blobs, self.processes)
            self.frames = Frames(self.assets, self.blobs, self.processes, self.artifacts)
            self.imprints = Imprints(self.db, self.blobs, self.tickets, self.captions)
            self.resources = Resources(self.db, self.blobs)
            self.recovery = self.tickets.recover()
            self.renderer = Renderer(settings, self.blobs, self.tickets, self.planner, self.processes, self.artifacts)
            if start_workers:
                self.preflight_report = run_preflight(self.processes, settings)
                self.renderer.start()
                self.phase = PHASE_READY
        except BaseException:
            self.close()
            raise

    def mark_ready(self):
        """Allow a preflight-paused application (e.g. tests) to take work."""
        with self._phase_lock:
            self.phase = PHASE_READY

    def require_accepting(self):
        require(self.phase == PHASE_READY, "service_unavailable",
                "The service is not accepting new work", 503, phase=self.phase)

    def is_accepting(self):
        return self.phase == PHASE_READY

    def active_work(self):
        """Observation snapshot for shutdown and the health endpoint."""
        running = []
        with self.db.read() as db:
            rows = db.execute("SELECT id,state,progress FROM tickets WHERE state='running'").fetchall()
            running = [dict(row) for row in rows]
        return {"phase": self.phase,
                "active_tickets": self.renderer.active_tickets() if self.renderer else [],
                "running_tickets": running,
                "media_processes": self.processes.active_snapshot()}

    def health(self):
        body = {"service": "reel-loom", "version": "0.1.0",
                "status": "ready" if self.phase == PHASE_READY else self.phase,
                "phase": self.phase,
                **self.db.health(), "blobs": self.blobs.stats(), "recovery": self.recovery,
                "limits": self.settings.public(),
                "preflight": self.preflight_report,
                "active": self.active_work()}
        if self.phase != PHASE_READY:
            raise LoomError("service_unavailable",
                            "The service is not accepting new work", 503, body)
        return body

    def begin_drain(self):
        """Flip the readiness gate while the HTTP socket is still served."""
        with self._phase_lock:
            if self.phase == PHASE_READY:
                self.phase = PHASE_DRAINING

    def drain(self, timeout=None):
        """Orderly shutdown: reject new work, release media work and locks."""
        timeout = self.settings.shutdown_timeout if timeout is None else timeout
        self.begin_drain()
        with self._phase_lock:
            if self.phase == PHASE_STOPPED and self._drain_report is not None:
                return self._drain_report
        report = {"timeout_seconds": timeout, "before": self.active_work()}
        try:
            self.processes.request_drain()
            renderer_report = self.renderer.drain(timeout) if self.renderer else {"joined": True}
            media_report = self.processes.stop_all(timeout=min(5, timeout))
            report["renderer"] = renderer_report
            report["media_processes"] = media_report
            report["after"] = self.active_work()
            report["clean"] = (media_report["remaining"] == 0 and renderer_report["joined"]
                               and self.processes.active_count() == 0)
        finally:
            self._drain_report = report
            self.close()
            self.phase = PHASE_STOPPED
        return report

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.renderer is not None:
            self.renderer.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
