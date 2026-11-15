#!/usr/bin/env python3
"""Manual end-to-end lifecycle simulator (no real FFmpeg/PyAV required).

Starts the real HTTP server with a stub encoder that can satisfy the
preflight, submits work, then sends SIGTERM and asserts the ordered drain:
503 for new work during draining, media processes released, clean exit.
Used only for local verification; the container drills exercise the real
media toolchain.
"""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

AV_STUB = (
    "from fractions import Fraction\n"
    "class FFmpegError(Exception):\n"
    "    pass\n"
    "class _Codec:\n"
    "    def __init__(self, kind):\n"
    "        self.name='stub'\n"
    "        if kind == 'video':\n"
    "            self.width=160; self.height=90\n"
    "            self.format=type('F',(),{'name':'yuv420p'})()\n"
    "        else:\n"
    "            self.sample_rate=8000; self.channels=1\n"
    "            self.layout=type('L',(),{'name':'mono'})()\n"
    "class _Stream:\n"
    "    def __init__(self, kind):\n"
    "        self.type=kind; self.index=0; self.time_base=Fraction(1, 1_000_000)\n"
    "        self.start_time=0; self.duration=1_000_000; self.frames=10\n"
    "        self.codec_context=_Codec(kind)\n"
    "        self.average_rate=Fraction(10,1) if kind=='video' else None\n"
    "        self.base_rate=None\n"
    "class _Container:\n"
    "    def __init__(self, *a, **k):\n"
    "        self.format=type('Fmt',(),{'name':'mov'})()\n"
    "        self.duration=1000000\n"
    "        self.streams=[_Stream('video'), _Stream('audio')]\n"
    "    def __enter__(self): return self\n"
    "    def __exit__(self, *a): return False\n"
    "def open(*a, **k): return _Container()\n"
)


def build_stub_tree(workdir):
    ffmpeg = workdir / "ffmpeg"
    ffmpeg.write_text(
        "#!" + sys.executable + "\n"
        "import sys, time\n"
        "out = next((a for a in sys.argv[1:] if not a.startswith('-') and '.' in a), None)\n"
        "if out:\n"
        "    open(out, 'wb').write(b'RIFF' + b'\\x00' * 64)\n"
        "time.sleep(0.2)\n"
    )
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    stubs = workdir / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "av.py").write_text(AV_STUB)
    return ffmpeg, stubs


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    except (urllib.error.URLError, ConnectionResetError, OSError):
        return None, None


def main():
    workdir = Path(tempfile.mkdtemp(prefix="loom-e2e-"))
    ffmpeg, stubs = build_stub_tree(workdir)
    data = workdir / "data"
    env = dict(os.environ, LOOM_DATA=str(data), LOOM_FFMPEG=str(ffmpeg),
               LOOM_PORT="18730", LOOM_HOST="127.0.0.1", LOOM_WORKERS="1",
               LOOM_SHUTDOWN_TIMEOUT="8",
               PYTHONPATH=str(REPO) + os.pathsep + str(stubs))
    proc = subprocess.Popen([sys.executable, "-m", "reel_loom.server"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:18730/loom/v1"
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            status, body = http("GET", base + "/health")
            if status == 200 and body.get("phase") == "ready":
                ready = True
                break
            time.sleep(0.5)
        assert ready, "service never became ready with a working preflight"
        print("PASS: readiness only after preflight:", json.dumps(body["preflight"]))

        # New work is accepted while ready (expect a domain 404, not a 503).
        status, body = http("POST", base + "/tickets", {"reel": "nope", "look": "nope"})
        assert status == 404 and body["error"] == "not_found", (status, body)
        print("PASS: work accepted while ready (domain routing reached)")

        proc.send_signal(signal.SIGTERM)
        # During draining the API must reject new work.
        saw_503 = False
        drain_deadline = time.monotonic() + 6
        while time.monotonic() < drain_deadline:
            status, body = http("POST", base + "/tickets", {"reel": "x", "look": "y"})
            if status == 503 and body["context"]["phase"] == "draining":
                saw_503 = True
                break
            time.sleep(0.1)
        assert saw_503, "draining never rejected new work with 503"
        print("PASS: draining rejects new work")

        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0, proc.returncode
        assert '"clean": true' in out, out
        print("PASS: clean ordered shutdown, exit 0")
        print("---- shutdown report ----")
        line = [ln for ln in out.splitlines() if "终止报告" in ln][-1]
        print(line)
    finally:
        if proc.poll() is None:
            proc.kill()
    print("E2E LIFECYCLE OK")


if __name__ == "__main__":
    main()
