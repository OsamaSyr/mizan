#!/usr/bin/env python3
"""
End-to-end resource benchmark of app.py, exactly as a judge would hit it.

Starts `PYTHON app.py` on a private port with a throwaway review DB, samples
the server's RSS every 100 ms, and times POST /api/check for:
  * the short UI sample      (first call = what the first visitor waits for)
  * a ~1,000-word article    (x3)
  * the 49:13 paraphrase probe from tests/test_semantic.py: only the
    semantic tier can locate it, so it proves whether the AI tier ran.

Usage:
  python3 deploy/experiments/bench_server.py --python .venv/bin/python \
      --docs DIR --port 18791 --label ml-cpu [--env MIZAN_DEVICE=cpu ...]
Prints one JSON object. Never touches port 8777 or data/review.sqlite.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROBE = ("Many readers find comfort in one passage above all. "
         "O humanity, We formed you from one male and one female and arranged you "
         "into peoples and tribes so that you might come to know one another; the "
         "noblest of you before God is the one most conscious of Him. "
         "It is recited at many weddings.")


def rss_kb(pid: int) -> int:
    """Resident set of pid and its children (KB), via ps — works on macOS and Linux."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out or 0)
    except Exception:
        return 0


def post(base: str, payload: dict, timeout: float = 300) -> tuple[float, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + "/api/check", data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    return time.perf_counter() - t0, body


def summarize(body: dict) -> dict:
    f = body.get("findings") or []
    return {"verdict": body.get("document_verdict"), "pipeline": body.get("pipeline"),
            "n_findings": len(f), "tiers": sorted({x.get("tier") or "-" for x in f}),
            "ai_findings": [x.get("ref") for x in f if x.get("ai_tier")]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--port", type=int, default=18791)
    ap.add_argument("--label", default="run")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--long-runs", type=int, default=3)
    a = ap.parse_args()
    assert a.port != 8777, "8777 is reserved for the owner's dev server"

    long_doc = open(os.path.join(a.docs, "long.txt"), encoding="utf-8").read()
    short_doc = open(os.path.join(a.docs, "short.txt"), encoding="utf-8").read()
    tmp = tempfile.mkdtemp(prefix="mizan-bench-")
    env = dict(os.environ, MIZAN_PORT=str(a.port), MIZAN_HOST="127.0.0.1",
               MIZAN_REVIEW_DB=os.path.join(tmp, "review.sqlite"), PYTHONUNBUFFERED="1")
    for kv in a.env:
        k, v = kv.split("=", 1)
        env[k] = v

    t_start = time.perf_counter()
    proc = subprocess.Popen([a.python, "app.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    samples: list[tuple[float, int]] = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            samples.append((time.perf_counter() - t_start, rss_kb(proc.pid)))
            time.sleep(0.1)
    threading.Thread(target=sampler, daemon=True).start()
    log_lines: list[str] = []
    threading.Thread(target=lambda: [log_lines.append(l) for l in proc.stdout],
                     daemon=True).start()

    base = f"http://127.0.0.1:{a.port}"
    res: dict = {"label": a.label, "python": a.python,
                 "env": {k: v for k, v in (kv.split("=", 1) for kv in a.env)}}
    try:
        while True:
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    json.loads(r.read())
                break
            except Exception:
                if proc.poll() is not None:
                    raise SystemExit("server died:\n" + "".join(log_lines))
                if time.perf_counter() - t_start > 180:
                    raise SystemExit("server never became healthy")
                time.sleep(0.1)
        res["health_ready_s"] = round(time.perf_counter() - t_start, 2)
        time.sleep(3)  # let app._warm_up (detector build) finish, as a real deploy would
        res["rss_idle_after_warmup_mb"] = round(rss_kb(proc.pid) / 1024)

        dt, body = post(base, {"translation": short_doc, "lang": "en"})
        res["first_check_short_s"] = round(dt, 2)
        res["first_check_short"] = summarize(body)
        res["rss_after_first_check_mb"] = round(rss_kb(proc.pid) / 1024)
        times = []
        for _ in range(3):
            dt, body = post(base, {"translation": short_doc, "lang": "en"})
            times.append(round(dt, 2))
        res["short_warm_s"] = times
        times = []
        for _ in range(a.long_runs):
            dt, body = post(base, {"translation": long_doc, "lang": "en"})
            times.append(round(dt, 2))
        res["long_1000w_s"] = times
        res["long_1000w"] = summarize(body)
        dt, body = post(base, {"translation": PROBE, "lang": "en"})
        res["probe_49_13_s"] = round(dt, 2)
        res["probe_49_13"] = summarize(body)
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            t0 = time.perf_counter(); r.read()
        t0 = time.perf_counter()
        urllib.request.urlopen(base + "/api/health", timeout=5).read()
        res["health_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        res["rss_end_mb"] = round(rss_kb(proc.pid) / 1024)
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    res["rss_peak_mb"] = round(max(s for _, s in samples) / 1024) if samples else None
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
