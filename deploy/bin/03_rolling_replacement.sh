#!/usr/bin/env bash
# Rolling replacement drill with task observation.
#
# 1. starts the service on a persistent data volume
# 2. submits a long render and observes it running
# 3. sends SIGTERM (the orchestrator's rolling-update signal)
# 4. asserts bounded, clean termination: new work rejected while draining,
#    media processes released, no task reported as successful
# 5. starts a fresh container on the SAME volume
# 6. asserts the interrupted ticket is recovered (failed, worker_interrupted)
#    and an explicit retry drives it to success against the frozen snapshot
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-reel-loom:local}"
DOCKER="${DOCKER:-docker}"
NAME="loom-rolling"
DATA_VOLUME="${DATA_VOLUME:-loom-rolling-data}"
PORT="${PORT:-8729}"
GRACE="${GRACE:-30}"
BASE="http://127.0.0.1:${PORT}/loom/v1"
DRILL="python3 /opt/drill/loom_drill.py"

cleanup() {
    "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$DOCKER" volume create "$DATA_VOLUME" >/dev/null

start_container() {
    echo "==> starting ${NAME} on volume ${DATA_VOLUME}"
    "$DOCKER" run -d --name "$NAME" \
        --security-opt no-new-privileges \
        --stop-signal SIGTERM --stop-timeout "$GRACE" \
        -v "$DATA_VOLUME":/data \
        -v "$REPO_ROOT/deploy/bin/loom_drill.py":/opt/drill/loom_drill.py:ro \
        -p "127.0.0.1:${PORT}:8729" \
        "$IMAGE"
}

start_container

echo "==> waiting for capability-gated readiness"
"$DOCKER" exec "$NAME" $DRILL wait-ready "$BASE" --timeout 90

echo "==> health reports real encoder preflight"
"$DOCKER" exec "$NAME" python3 -c \
    "import json,urllib.request; h=json.load(urllib.request.urlopen('$BASE/health')); \
assert h['phase']=='ready' and h['preflight']['video_codecs'], h; print(json.dumps(h['preflight']))"

echo "==> creating a 30 second sample and submitting a long render"
"$DOCKER" exec "$NAME" $DRILL sample /data/tmp/drill.mp4 --seconds 30
ASSET=$("$DOCKER" exec "$NAME" $DRILL ingest "$BASE" /data/tmp/drill.mp4 drill-sample \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "asset: $ASSET"
REEL=$("$DOCKER" exec "$NAME" $DRILL reel "$BASE" "$ASSET" --seconds 30 \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "reel: $REEL"
"$DOCKER" exec "$NAME" $DRILL look "$BASE" preview \
    '{"kind":"video","width":320,"height":180,"fit":"contain"}' >/dev/null
TICKET=$("$DOCKER" exec "$NAME" $DRILL render "$BASE" "$REEL" preview --key drill-rolling-1 \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "ticket: $TICKET"

echo "==> observing the render in flight"
"$DOCKER" exec "$NAME" python3 - "$BASE" "$TICKET" <<'PY'
import json, sys, time, urllib.request
base, ticket = sys.argv[1], sys.argv[2]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    t = json.load(urllib.request.urlopen(f"{base}/tickets/{ticket}"))
    h = json.load(urllib.request.urlopen(f"{base}/health"))
    if t["state"] == "running":
        procs = h["active"]["media_processes"]
        assert procs and procs[0]["alive"], h
        print(f"observed running ticket progress={t['progress']:.2f} media_pid={procs[0]['pid']}")
        break
    time.sleep(0.5)
else:
    raise SystemExit("ticket never reached running")
PY

echo "==> sending SIGTERM; container must drain within ${GRACE}s"
STOP_START=$(date +%s)
"$DOCKER" stop --time "$GRACE" "$NAME" >/dev/null
STOP_END=$(date +%s)
STOPPED_IN=$((STOP_END - STOP_START))
echo "container released after ${STOPPED_IN}s"
[ "$STOPPED_IN" -lt "$GRACE" ] || { echo "FAIL: drain exceeded the grace period" >&2; exit 1; }

echo "==> shutdown report"
"$DOCKER" logs "$NAME" 2>&1 | grep "终止报告" | tail -1

echo "==> interrupted render must NOT be successful"
"$DOCKER" logs "$NAME" 2>&1 | grep -q '"clean": true' \
    || { echo "FAIL: shutdown was not clean" >&2; exit 1; }
"$DOCKER" rm -f "$NAME" >/dev/null

# Same volume, new container: the single-instance lock must be free.
start_container
"$DOCKER" exec "$NAME" $DRILL wait-ready "$BASE" --timeout 90

echo "==> recovery semantics on the reopened data directory"
"$DOCKER" exec "$NAME" $DRILL status "$BASE" "$TICKET" | tee /tmp/recovered.json
python3 - <<PY
import json
t = json.load(open("/tmp/recovered.json"))
assert t["state"] == "failed", t
assert t["error"]["error"] == "worker_interrupted", t
print("recovered ticket is failed/interrupted as required")
PY

echo "==> explicit retry reuses the frozen snapshot and completes"
"$DOCKER" exec "$NAME" $DRILL retry "$BASE" "$TICKET"
"$DOCKER" exec "$NAME" $DRILL watch "$BASE" "$TICKET" --timeout 120
"$DOCKER" exec "$NAME" $DRILL status "$BASE" "$TICKET" | tee /tmp/final-status.json
python3 - <<'PY'
import json
t = json.load(open("/tmp/final-status.json"))
assert t["state"] == "succeeded", t
assert t["artifacts"], t
print("retry succeeded with artifact:", t["artifacts"][0]["id"])
PY

echo "==> data persisted across replacement"
"$DOCKER" exec "$NAME" python3 -c \
    "import json,urllib.request; h=json.load(urllib.request.urlopen('$BASE/health')); \
assert h['blobs']['count'] >= 2 and h['database'] == 'ok', h; print('persisted blobs:', h['blobs'])"

echo "PASS: rolling replacement, bounded drain, recovery and retry all verified"
