import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from .errors import LoomError, RenderCancelled, require


class DrainInterrupted(LoomError):
    """Raised when in-flight media work is cut short by service shutdown."""

    def __init__(self):
        super().__init__("worker_draining",
                         "The service is shutting down and stopped this media operation", 503)


class Processes:
    """Bounded FFmpeg execution with a registry of live media processes."""

    def __init__(self, settings):
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.workers + 1)
        self.draining = threading.Event()
        self._active_lock = threading.Lock()
        self._active = {}
        # Keep every ephemeral file (encoder status/stderr pipes) inside the
        # single data boundary instead of the image's (read-only) /tmp.
        os.makedirs(str(settings.root / "tmp"), exist_ok=True)
        self._tmp_dir = str(settings.root / "tmp")

    def command(self, args):
        return [self.settings.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1"] + list(args)

    def _stop(self, process):
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def _register(self, process):
        with self._active_lock:
            self._active[process.pid] = process

    def _unregister(self, process):
        with self._active_lock:
            self._active.pop(process.pid, None)

    def active_snapshot(self):
        with self._active_lock:
            return [{"pid": pid, "alive": process.poll() is None}
                    for pid, process in self._active.items()]

    def active_count(self):
        with self._active_lock:
            return sum(1 for process in self._active.values() if process.poll() is None)

    def request_drain(self):
        self.draining.set()

    def stop_all(self, timeout):
        """Terminate every live media process within ``timeout`` seconds.

        Returns a report describing what was still running and how it was
        released.  Never raises: shutdown must always make progress.
        """
        deadline = time.monotonic() + max(1.0, float(timeout))
        with self._active_lock:
            processes = list(self._active.values())
        report = {"started": len(processes), "terminated": 0, "killed": 0,
                  "remaining": 0, "pids": [p.pid for p in processes]}
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                continue
        grace = max(0.5, min(3.0, (deadline - time.monotonic()) / 2))
        for process in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                process.wait(timeout=min(grace, remaining))
                report["terminated"] += 1
            except subprocess.TimeoutExpired:
                continue
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=max(0.5, deadline - time.monotonic()))
                    report["killed"] += 1
                except (ProcessLookupError, subprocess.TimeoutExpired, ValueError):
                    pass
        report["remaining"] = sum(1 for p in processes if p.poll() is None)
        return report

    def run(self, args, cancelled=lambda: False, progress=lambda value: None, duration=None):
        deadline = time.monotonic() + self.settings.render_timeout
        acquired = False
        while not acquired:
            if self.draining.is_set():
                raise DrainInterrupted()
            if cancelled():
                raise RenderCancelled()
            require(time.monotonic() < deadline, "encoder_capacity", "Encoder capacity wait expired", 503)
            acquired = self.slots.acquire(timeout=0.1)
        process = None
        try:
            with tempfile.TemporaryFile(dir=self._tmp_dir) as errors, \
                    tempfile.TemporaryFile(dir=self._tmp_dir) as status:
                command = self.command(["-progress", "pipe:1", "-nostats"] + list(args))
                process = subprocess.Popen(command, stdout=status, stderr=errors,
                                           start_new_session=os.name == "posix")
                self._register(process)
                last = 0
                while process.poll() is None:
                    if self.draining.is_set():
                        self._stop(process)
                        raise DrainInterrupted()
                    if cancelled():
                        self._stop(process)
                        raise RenderCancelled()
                    if time.monotonic() >= deadline:
                        self._stop(process)
                        raise LoomError("encoder_timeout", "FFmpeg exceeded the processing deadline", 504)
                    end = status.tell()
                    if end != last and duration:
                        status.seek(0)
                        lines = status.read().decode(errors="replace").splitlines()
                        values = [line.split("=",1)[1] for line in lines if line.startswith("out_time_us=")]
                        if values and values[-1].isdigit():
                            progress(min(0.98, int(values[-1])/1_000_000/duration))
                        status.seek(0, 2)
                        last = end
                    time.sleep(0.05)
                errors.seek(0, 2)
                size = errors.tell()
                errors.seek(max(0, size-6000))
                detail = errors.read().decode(errors="replace")
                require(process.returncode == 0, "encoder_failed", "FFmpeg could not produce this rendition",
                        422, stderr=detail)
                if self.draining.is_set():
                    raise DrainInterrupted()
                if cancelled():
                    raise RenderCancelled()
                progress(1)
                return {"exit_code": process.returncode, "stderr": detail}
        finally:
            if process is not None:
                self._unregister(process)
                self._stop(process)
            self.slots.release()

    def decode_audio(self, path, output, sample_rate=16000):
        self.run(["-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-map", "0:a:0",
                  "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "s16le", str(output)])
        require(Path(output).is_file(), "decode_output", "Audio decoder produced no output")
        return Path(output)
