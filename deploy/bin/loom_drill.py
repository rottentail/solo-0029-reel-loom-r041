#!/usr/bin/env python3
"""Stdlib-only Reel Loom drill client.

Drives the same production HTTP API an integration partner would use, with
no third-party packages.  Used by the container replacement drills so the
exercise does not depend on a host Python environment.

Commands:
  wait-ready URL [--timeout S]        poll /health until phase=ready
  expect-rejected URL                 assert /health is 503 (encoder broken)
  sample PATH [--seconds N]           create a colorbar+tone MP4 via ffmpeg
  ingest URL FILE LABEL
  reel URL ASSET_ID
  look URL ID SPEC_JSON
  render URL REEL_ID LOOK_ID
  watch URL TICKET_ID [--timeout S]   print state transitions until terminal
  retry URL TICKET_ID
  status URL TICKET_ID
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


def http(method, url, body=None, headers=None, raw=False, timeout=30):
    headers = dict(headers or {})
    data = None
    if isinstance(body, bytes):
        data = body
        headers.setdefault("Content-Type", "application/octet-stream")
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (payload if raw else json.loads(payload))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except ValueError:
            payload = {"error": "http_failure"}
        return exc.code, payload


def health(url):
    try:
        status, body = http("GET", url.rstrip("/") + "/health")
        return status, body
    except urllib.error.URLError:
        return None, None


def cmd_wait_ready(args):
    deadline = time.monotonic() + args.timeout
    last = None
    while time.monotonic() < deadline:
        status, body = health(args.url)
        phase = body.get("phase") if isinstance(body, dict) else None
        if (status, phase) != last:
            print(f"health: http={status} phase={phase}", flush=True)
            last = (status, phase)
        if status == 200 and phase == "ready":
            print("service is accepting work", flush=True)
            return 0
        time.sleep(0.5)
    print("timeout waiting for ready phase", file=sys.stderr)
    return 1


def cmd_expect_rejected(args):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, body = health(args.url)
        if status == 503 and isinstance(body, dict) and body.get("context", {}).get("phase"):
            print("startup correctly rejected work:", json.dumps(body, ensure_ascii=False))
            return 0
        if status is not None:
            time.sleep(0.5)
        else:
            time.sleep(0.5)
    print("service never exposed a 503 startup rejection", file=sys.stderr)
    return 1


def cmd_sample(args):
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={args.seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=659:sample_rate=48000:duration={args.seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        args.path,
    ], check=True)
    print(f"sample written: {args.path}")


def cmd_ingest(args):
    content = open(args.file, "rb").read()
    status, body = http("POST", args.url.rstrip("/") + "/assets", content,
                        {"X-Media-Label": args.label})
    print(json.dumps(body, ensure_ascii=False))
    return 0 if status == 200 else 1


def cmd_reel(args):
    body = {"title": "滚动替换演练",
            "slices": [{"asset": args.asset, "in": 0.1, "out": max(0.2, args.seconds - 0.1)}]}
    status, reply = http("POST", args.url.rstrip("/") + "/reels", body)
    print(json.dumps(reply, ensure_ascii=False))
    return 0 if status == 200 else 1


def cmd_look(args):
    status, reply = http("PUT", args.url.rstrip("/") + "/looks/" + args.id,
                         json.loads(args.spec))
    print(json.dumps(reply, ensure_ascii=False))
    return 0 if status == 200 else 1


def cmd_render(args):
    body = {"reel": args.reel, "look": args.look}
    status, reply = http("POST", args.url.rstrip("/") + "/tickets", body,
                         {"Idempotency-Key": args.key} if args.key else None)
    print(json.dumps(reply, ensure_ascii=False))
    return 0 if status == 200 else 1


def cmd_watch(args):
    seen = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status, body = http("GET", args.url.rstrip("/") + "/tickets/" + args.ticket)
        if status != 200:
            print(f"watch: http={status} body={body}")
            time.sleep(0.5)
            continue
        state = body["state"]
        if state != seen:
            print(f"ticket {args.ticket}: state={state} progress={body.get('progress')} "
                  f"attempt={body.get('attempt')} error={body.get('error')}", flush=True)
            seen = state
        if state in ("succeeded", "failed", "cancelled"):
            return 0
        time.sleep(0.5)
    print("watch timed out before terminal state", file=sys.stderr)
    return 1


def cmd_retry(args):
    status, reply = http("POST", args.url.rstrip("/") + f"/tickets/{args.ticket}/retry")
    print(json.dumps(reply, ensure_ascii=False))
    return 0 if status == 200 else 1


def cmd_status(args):
    status, reply = http("GET", args.url.rstrip("/") + f"/tickets/{args.ticket}")
    print(json.dumps(reply, ensure_ascii=False))
    return 0 if status == 200 else 1


def main():
    parser = argparse.ArgumentParser(description="Reel Loom container drill client")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("wait-ready"); p.add_argument("url"); p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(func=cmd_wait_ready)
    p = sub.add_parser("expect-rejected"); p.add_argument("url"); p.set_defaults(func=cmd_expect_rejected)
    p = sub.add_parser("sample"); p.add_argument("path"); p.add_argument("--seconds", type=int, default=30)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("ingest"); p.add_argument("url"); p.add_argument("file"); p.add_argument("label")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("reel"); p.add_argument("url"); p.add_argument("asset"); p.add_argument("--seconds", type=float, default=30)
    p.set_defaults(func=cmd_reel)
    p = sub.add_parser("look"); p.add_argument("url"); p.add_argument("id"); p.add_argument("spec")
    p.set_defaults(func=cmd_look)
    p = sub.add_parser("render"); p.add_argument("url"); p.add_argument("reel"); p.add_argument("look")
    p.add_argument("--key"); p.set_defaults(func=cmd_render)
    p = sub.add_parser("watch"); p.add_argument("url"); p.add_argument("ticket"); p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("retry"); p.add_argument("url"); p.add_argument("ticket"); p.set_defaults(func=cmd_retry)
    p = sub.add_parser("status"); p.add_argument("url"); p.add_argument("ticket"); p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
