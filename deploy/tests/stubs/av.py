"""Minimal PyAV stub for sandboxed lifecycle tests.

The real dependency is declared in pyproject.toml and installed inside the
container image.  The lifecycle tests only exercise process and shutdown
coordination, so when PyAV is absent on the test host this stub exposes just
the attributes imported by ``reel_loom.probe`` and ``reel_loom.conformance``.
Any actual media operation fails loudly instead of returning fake data.
"""


class FFmpegError(Exception):
    pass


def open(*args, **kwargs):  # pragma: no cover - never reached in lifecycle tests
    raise FFmpegError("PyAV is stubbed in this test environment; install the real 'av' package")
