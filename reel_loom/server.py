import json
import logging
import signal
import threading

from .config import Settings
from .application import Application
from .api import Api
from .http import MediaHttpServer

logger = logging.getLogger(__name__)


class Service:
    """Owns the application lifecycle and the ordered termination sequence."""

    def __init__(self, settings):
        self.settings = settings
        self.app = None
        self.server = None
        self._shutdown_started = threading.Event()
        self._shutdown_finished = threading.Event()
        self._drain_report = None

    def start(self):
        # Preflight runs inside Application construction: a broken encoder
        # aborts here, before any HTTP socket is opened.
        self.app = Application(self.settings)
        self.server = MediaHttpServer((self.settings.host, self.settings.port), Api(self.app))
        logger.info("声画织场服务就绪: http://%s:%s/loom/v1/", self.settings.host, self.settings.port)

    def serve(self):
        self.server.serve_forever(poll_interval=.2)
        # serve_forever returns once shutdown() runs the termination sequence.
        self._shutdown_finished.wait(timeout=self.settings.shutdown_timeout + 5)
        clean = bool(self._drain_report and self._drain_report.get("clean"))
        if not clean:
            raise SystemExit(1)

    def request_shutdown(self, signum):
        """Signal-safe entry point: never blocks inside the handler."""
        if self._shutdown_started.is_set():
            logger.warning("收到重复终止信号，等待关闭完成")
            return
        self._shutdown_started.set()
        logger.info("收到信号 %s，开始终止：停止接收新工作", signum)
        thread = threading.Thread(target=self._shutdown_sequence, name="loom-shutdown", daemon=True)
        thread.start()

    def _shutdown_sequence(self):
        report = {"http": {}, "application": {}}
        try:
            if self.app is not None:
                # Flip the readiness gate first: in-flight requests finish and
                # the health endpoint starts answering 503 while the socket is
                # still being served.
                self.app.begin_drain()
                # Observability window: probes and clients polling during a
                # rolling replacement must see the draining 503, not a reset.
                threading.Event().wait(1.0)
                if self.server is not None:
                    self.server.shutdown()
                    report["http"] = self.server.request_snapshot()
                    self.server.server_close()
                report["application"] = self.app.drain(self.settings.shutdown_timeout)
        except Exception:
            logger.exception("终止过程发生异常")
        clean = report.get("application", {}).get("clean", False)
        report["clean"] = clean
        self._drain_report = report
        logger.info("终止报告: %s", json.dumps(report, ensure_ascii=False, default=str))
        if not clean:
            # The container runtime will SIGKILL us after its grace period;
            # surface the unclean shutdown explicitly in the meantime.
            logger.error("终止期限内仍有媒体工作未释放，退出码标记为失败")
        self._shutdown_finished.set()


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    settings = Settings.from_env()
    service = Service(settings)
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, lambda sig, frame: service.request_shutdown(sig))
    try:
        service.start()
        service.serve()
    except BaseException:
        logger.exception("服务运行失败")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
