#!/usr/bin/env bash
# Reproducible local test runner.
#
# Uses only the Python standard library. When PyAV is unavailable (e.g. a
# stripped CI sandbox) a tiny stub is injected via PYTHONPATH so the
# lifecycle tests can import the application wiring; the stub cannot decode
# media and no test path asks it to. Inside the container image the real
# PyAV is installed and takes precedence.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT/deploy/tests/stubs${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m unittest discover -s deploy/tests -p 'test_*.py' -v
