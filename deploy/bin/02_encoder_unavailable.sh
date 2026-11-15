#!/usr/bin/env bash
# Encoder-unavailable drill.
#
# A container whose FFmpeg cannot encode must exit non-zero at startup, and
# no HTTP port may ever accept traffic: health is gated on a real
# encode/decode preflight, not on the socket being open.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-reel-loom:local}"
BROKEN_IMAGE="${BROKEN_IMAGE:-reel-loom:broken-encoder}"
DOCKER="${DOCKER:-docker}"
NAME="loom-broken-$$"
DATA_VOLUME="loom-broken-data-$$"

cleanup() {
    "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true
    "$DOCKER" volume rm "$DATA_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> building broken-encoder variant"
"$DOCKER" build -t "$BROKEN_IMAGE" -f "$REPO_ROOT/deploy/Dockerfile.broken-encoder" \
    --build-arg BASE_IMAGE="$IMAGE" "$REPO_ROOT"

echo "==> starting container with a non-functional encoder"
set +e
"$DOCKER" run --name "$NAME" \
    --security-opt no-new-privileges \
    -v "$DATA_VOLUME":/data \
    "$BROKEN_IMAGE" > /tmp/${NAME}.log 2>&1
EXIT_CODE=$?
set -e

echo "---- container log ----"
cat /tmp/${NAME}.log
echo "------------------------"

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "FAIL: broken-encoder container exited zero" >&2
    exit 1
fi
grep -q "encoder_unavailable\|could not produce a test rendition" /tmp/${NAME}.log \
    || { echo "FAIL: preflight refusal not logged" >&2; exit 1; }

# The HTTP socket must never have accepted traffic.
if grep -q "服务就绪" /tmp/${NAME}.log; then
    echo "FAIL: service announced readiness without a working encoder" >&2
    exit 1
fi

"$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true
echo "PASS: startup refused work and exited ${EXIT_CODE} when encoding was unavailable"
