#!/usr/bin/env bash
# Build the read-only service image and verify the read-only rootfs +
# writable single data volume boundary.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-reel-loom:local}"
DOCKER="${DOCKER:-docker}"

cd "$REPO_ROOT"

echo "==> building ${IMAGE}"
"$DOCKER" build --pull -t "$IMAGE" .

echo "==> image filesystem is read-only at runtime; /data is the only writable mount"
"$DOCKER" run --rm --read-only --tmpfs /tmp:size=64m \
    --security-opt no-new-privileges \
    --entrypoint python "$IMAGE" -c \
    'import os, tempfile, sys
try:
    open("/opt/reel-loom/probe","w")
except OSError:
    print("rootfs read-only: OK")
else:
    sys.exit("rootfs was writable")
assert os.access("/data", os.W_OK), "/data must be writable"
print("data volume writable: OK")'

echo "==> service runs as non-root"
"$DOCKER" run --rm --entrypoint id "$IMAGE" | tee /dev/stderr | grep -q '^uid=10001(loom)'

echo "build OK: ${IMAGE}"
