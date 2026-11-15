#!/usr/bin/env python3
"""Container entrypoint.

Runs as root only long enough to make the single data volume writable by
the unprivileged service account, then drops to that UID/GID and replaces
itself with the server process.  When the container already runs as a
non-root user (e.g. ``docker run --user``) privilege dropping is skipped.
"""
import os
import shutil
import sys
from pathlib import Path

SERVICE_UID = 10001
SERVICE_GID = 10001


def fail(message):
    print(f"loom-entrypoint: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    data = Path(os.environ.get("LOOM_DATA", "/data")).resolve()
    try:
        data.mkdir(parents=True, exist_ok=True)
        for part in ("blobs", "scratch", "tmp"):
            (data / part).mkdir(exist_ok=True)
    except OSError as exc:
        fail(f"cannot prepare data directory {data}: {exc}")

    if os.geteuid() == 0:
        try:
            import grp
            import pwd
            try:
                gid = grp.getgrgid(SERVICE_GID).gr_gid
            except KeyError:
                gid = SERVICE_GID
            try:
                uid = pwd.getpwuid(SERVICE_UID).pw_uid
            except KeyError:
                uid = SERVICE_UID
            # Only fix ownership when the volume does not already belong to
            # the service account; walking a large media store on every
            # restart would be wasteful.
            if data.stat().st_uid != uid or data.stat().st_gid != gid:
                os.chown(data, uid, gid)
                for root, dirs, files in os.walk(data):
                    for name in dirs + files:
                        os.chown(os.path.join(root, name), uid, gid)
            os.setgid(gid)
            os.setuid(uid)
        except (OSError, PermissionError) as exc:
            fail(f"could not drop privileges: {exc}")

    if not shutil.which(os.environ.get("LOOM_FFMPEG", "ffmpeg")) and not Path(
            os.environ.get("LOOM_FFMPEG", "ffmpeg")).is_file():
        fail("ffmpeg is not on PATH or at LOOM_FFMPEG")

    print(f"loom-entrypoint: serving as uid={os.geteuid()} gid={os.getegid()} data={data}",
          flush=True)
    os.execvp(sys.executable, [sys.executable, "-m", "reel_loom.server"])


if __name__ == "__main__":
    main()
