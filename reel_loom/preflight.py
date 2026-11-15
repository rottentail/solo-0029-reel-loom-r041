"""Startup capability probe.

The service is only allowed to accept work once a real encoder round trip has
succeeded.  A missing binary, a broken wrapper or a container image without
working codecs all fail here, before the HTTP socket is opened.
"""
import json
import logging
from pathlib import Path

from .errors import LoomError, require

logger = logging.getLogger(__name__)


class PreflightFailure(LoomError):
    def __init__(self, detail, context=None):
        super().__init__("encoder_unavailable", detail, 503, context or {})


def run_preflight(processes, settings):
    """Probe, decode and encode real media; return a serialisable report."""
    ffmpeg = str(settings.ffmpeg)
    scratch = settings.root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    probe_path = scratch / "preflight.mp4"
    try:
        try:
            processes.run([
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(probe_path),
            ])
        except LoomError as exc:
            raise PreflightFailure("FFmpeg could not produce a test rendition",
                                   {"reason": exc.detail, "ffmpeg": ffmpeg}) from exc
        require(probe_path.is_file() and probe_path.stat().st_size > 0,
                "encoder_unavailable", "Preflight encoder produced no output", 503,
                ffmpeg=ffmpeg)
        try:
            from .probe import inspect_media
            metadata = inspect_media(probe_path, settings)
        except LoomError as exc:
            raise PreflightFailure("Preflight rendition could not be decoded",
                                   {"reason": exc.detail, "ffmpeg": ffmpeg}) from exc
        require(metadata["has_video"] and metadata["has_audio"],
                "encoder_unavailable", "Preflight rendition is missing required streams", 503,
                ffmpeg=ffmpeg)
        report = {
            "ffmpeg": ffmpeg,
            "probe_bytes": probe_path.stat().st_size,
            "container": metadata["container"],
            "video_codecs": [s["codec"] for s in metadata["streams"] if s["kind"] == "video"],
            "audio_codecs": [s["codec"] for s in metadata["streams"] if s["kind"] == "audio"],
        }
        logger.info("启动预检通过: %s", json.dumps(report, ensure_ascii=False))
        return report
    finally:
        try:
            if probe_path.exists():
                probe_path.unlink()
        except OSError:
            logger.warning("预检临时文件未能移除: %s", probe_path)
