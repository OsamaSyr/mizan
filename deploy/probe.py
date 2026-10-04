#!/usr/bin/env python3
"""
MIZAN deployment probe. Standard library only, Python 3.9+.

  prewarm  Run after the service starts (systemd ExecStartPost). Waits for
           /api/health, then sends one check per indexed language, which loads
           bge-m3, every language's vectors and every detector before a judge
           arrives. Verifies that the AI tier really ran and writes the result
           to a small JSON status file. Always exits 0: a cold AI tier must
           never stop the deterministic service from running.

  check    Used by the watchdog every minute. Exit 0 = healthy, 1 = /api/health
           failed, 2 = the engine did not answer a check (hung or erroring).
           With --ai it also sends the 49:13 paraphrase probe and records
           whether the semantic tier located it ("up") or not ("degraded").
           A degraded AI tier is reported, not treated as a failure: the
           deterministic path is still serving correct answers.

  smoke    Run from your laptop against the public URL before and during
           judging. Times the home page, the UI's first sample, the AI probe
           and a fixed ~1,000-word document, shows whether the AI tier is
           active, and shows how many days the TLS certificate has left.

Examples
  python3 deploy/probe.py smoke --base https://mizan.example.com
  python3 deploy/probe.py check --base http://127.0.0.1:8000 --ai \
      --status-file /var/lib/mizan/status/ai.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Only the semantic tier can locate this passage (tests/test_semantic.py,
# test_paraphrase_is_located_and_referred_never_matched): a fresh rendering of
# 49:13 with no printed reference. With the AI tier on, a finding for 49:13
# with ai_tier=true comes back; on the deterministic path, NO_QUOTES.
AI_PROBE = ("Many readers find comfort in one passage above all. "
            "O humanity, We formed you from one male and one female and arranged you "
            "into peoples and tribes so that you might come to know one another; the "
            "noblest of you before God is the one most conscious of Him. "
            "It is recited at many weddings.")
AI_PROBE_REF = "49:13"

# The UI's first reviewed sample (web/app.js, s1): what every page load checks.
SAMPLE = ('Then He clarified that creation was not left purposeless, saying: '
          '"And I did not create the jinn and mankind except to serve Me." (Quran 51:56). '
          'This verse is a foundation for understanding the purpose of human existence.')

# Fixed benchmark document: these paragraphs (written for this probe) plus one
# approved English rendering per verse below, fetched from the server's own
# /api/verse. ~1,000 words, ~10 quotations. Reference timing for the same
# document is in docs/DEPLOY.md.
BENCH_REFS = ["2:255", "112:1", "3:59", "51:56", "13:35", "16:96", "4:77", "2:286", "39:53", "94:5"]
BENCH_PROSE = [
    "A printed booklet passes through many hands before it reaches a reader. An author drafts it, "
    "a translator renders it, an editor shortens it, a designer lays it out, and a printer sets "
    "the final pages. At every step a sentence can change. Most of those changes are harmless and "
    "some are improvements, but a quotation of scripture is different: the reader trusts that the "
    "words on the page are the words of an approved translation, and has no way to check.",
    "Reviewers in a translation office know this problem well. They are asked to sign off on "
    "material in languages they may read only slowly, under deadlines set by printing schedules. "
    "They can compare a passage with one translation they know, but they cannot hold fifteen "
    "translations in mind at once, and they cannot tell from memory whether a verse was quoted "
    "faithfully, lightly edited, or written from scratch by a well-meaning volunteer.",
    "The aim of a review gate is therefore modest. It does not decide what a verse means and it "
    "does not offer a better wording. It answers one practical question for each quotation: "
    "which approved published translation does this text come from, and if it differs, exactly "
    "which words differ. Anything it cannot attribute is sent to a person, with the closest "
    "approved renderings placed beside it so the specialist can decide quickly.",
    "Consider a short leaflet on patience written for new readers. Its author opens with a "
    "familiar passage, moves to a story, and closes with a reminder of hope. Each of those "
    "quotations may have been copied from a different source over the years, and some may have "
    "been retyped from a poster or a slide. The leaflet is short, yet it can carry three or four "
    "different translations, and nobody in the chain may know which ones.",
    "A careful office keeps a record of these decisions. When a reviewer accepts a modified "
    "rendering, the reason is written down; when a quotation is replaced with an approved one, "
    "the replacement and its source are kept with the document. Over time the record shows which "
    "translations the office relies on, which errors recur, and where training would help most. "
    "None of this requires the reviewer to read every language fluently.",
    "Speed matters as much as care. If a check takes too long, people stop running it and the "
    "gate becomes a formality. A useful gate answers in seconds for a page of text, explains "
    "itself in plain language, and never pretends to certainty it does not have. It should also "
    "keep working when an optional component is unavailable, saying clearly which path produced "
    "the answer rather than failing without explanation.",
    "The paragraphs above are ordinary prose and should produce no findings. The quotations "
    "between them are taken verbatim from approved translations held in the index, so a working "
    "gate should attribute each one to its edition. The document as a whole is about the length "
    "of a typical article, which is why it is used to measure how long a real check takes on the "
    "server that will host the public demonstration.",
]

UA = "mizan-deploy-probe/1"


def _req(base: str, path: str, payload: dict | None = None, timeout: float = 10.0):
    url = base.rstrip("/") + path
    data = None
    headers = {"User-Agent": UA}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        status = r.status
    return status, body, time.perf_counter() - t0


def get_json(base: str, path: str, timeout: float = 10.0) -> tuple[dict, float]:
    _, body, secs = _req(base, path, timeout=timeout)
    return json.loads(body), secs


def check_doc(base: str, text: str, lang: str = "en", timeout: float = 180.0) -> tuple[dict, float]:
    _, body, secs = _req(base, "/api/check", {"translation": text, "lang": lang}, timeout=timeout)
    return json.loads(body), secs


def ai_found(result: dict) -> bool:
    return any(f.get("ref") == AI_PROBE_REF and f.get("ai_tier")
               for f in result.get("findings") or [])


def write_status(path: str | None, state: str, detail: str) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"ai_tier": state, "detail": detail,
           "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- prewarm

def cmd_prewarm(a: argparse.Namespace) -> int:
    t_start = time.perf_counter()
    while True:
        try:
            get_json(a.base, "/api/health", timeout=3)
            break
        except Exception:                                   # noqa: BLE001
            if time.perf_counter() - t_start > a.wait:
                log(f"prewarm: /api/health not up after {a.wait:.0f}s; giving up (service keeps running)")
                write_status(a.status_file, "unknown", "health never came up during prewarm")
                return 0
            time.sleep(0.5)
    log(f"prewarm: health up after {time.perf_counter() - t_start:.1f}s")
    try:
        langs = [row["code"] for row in get_json(a.base, "/api/languages")[0].get("languages", [])]
    except Exception as exc:                                # noqa: BLE001
        log(f"prewarm: /api/languages failed ({exc}); warming en only")
        langs = ["en"]
    if "en" in langs:
        langs.remove("en")
    langs.insert(0, "en")
    state, detail = "degraded", "probe not run"
    for lang in langs:
        try:
            res, secs = check_doc(a.base, AI_PROBE, lang=lang, timeout=a.timeout)
            log(f"prewarm: {lang:3s} check {secs:6.1f}s  verdict={res.get('document_verdict')}")
            if lang == "en":
                if ai_found(res):
                    state, detail = "up", f"49:13 located by the semantic tier ({secs:.1f}s incl. model load)"
                else:
                    detail = ("49:13 NOT located: the AI tier is not active, answers come from "
                              "the deterministic path")
        except Exception as exc:                            # noqa: BLE001
            log(f"prewarm: {lang} check failed: {exc}")
            if lang == "en":
                detail = f"probe failed: {exc}"
    write_status(a.status_file, state, detail)
    log(f"prewarm: AI tier {state.upper()} - {detail}; total {time.perf_counter() - t_start:.1f}s")
    return 0


# ---------------------------------------------------------------- check

def cmd_check(a: argparse.Namespace) -> int:
    try:
        h, secs = get_json(a.base, "/api/health", timeout=a.health_timeout)
        if h.get("status") != "ok":
            log(f"check: health status={h.get('status')!r}")
            return 1
    except Exception as exc:                                # noqa: BLE001
        log(f"check: /api/health failed: {exc}")
        return 1
    if not a.ai:
        return 0
    try:
        res, secs = check_doc(a.base, AI_PROBE, timeout=a.timeout)
    except urllib.error.HTTPError as exc:
        log(f"check: engine answered HTTP {exc.code}")
        write_status(a.status_file, "unknown", f"engine HTTP {exc.code}")
        return 2
    except Exception as exc:                                # noqa: BLE001
        log(f"check: engine did not answer: {exc}")
        write_status(a.status_file, "unknown", f"engine did not answer: {exc}")
        return 2
    if ai_found(res):
        write_status(a.status_file, "up", f"49:13 located in {secs:.1f}s")
    else:
        write_status(a.status_file, "degraded", "49:13 not located: deterministic path only")
        log("check: AI tier DEGRADED (deterministic answers still served)")
    return 0


# ---------------------------------------------------------------- smoke

def cert_days_left(base: str) -> str:
    u = urllib.parse.urlsplit(base)
    if u.scheme != "https":
        return "n/a (not https)"
    host, port = u.hostname, u.port or 443
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            cert = ss.getpeercert()
    exp = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    days = (exp - dt.datetime.now(dt.timezone.utc)).days
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    return f"{days} days (expires {exp:%Y-%m-%d}, issuer {issuer.get('organizationName', '?')})"


def bench_document(base: str) -> str:
    parts = []
    for i, ref in enumerate(BENCH_REFS):
        if i < len(BENCH_PROSE):
            parts.append(BENCH_PROSE[i])
        r, _ = get_json(base, "/api/verse?" + urllib.parse.urlencode({"ref": ref, "lang": "en"}))
        rend = sorted(r.get("renderings") or [], key=lambda x: x["book_id"])
        if not rend:
            continue
        text = rend[0]["text"].split("____")[0].strip()
        parts.append(f'God says: "{text}" (Quran {ref})')
    parts.extend(BENCH_PROSE[len(BENCH_REFS):])
    return "\n\n".join(parts)


def cmd_smoke(a: argparse.Namespace) -> int:
    ok = True
    rows = []
    try:
        rows.append(("TLS certificate", cert_days_left(a.base)))
    except Exception as exc:                                # noqa: BLE001
        rows.append(("TLS certificate", f"ERROR {exc}"))
        ok = False
    try:
        h, secs = get_json(a.base, "/api/health")
        rows.append(("GET /api/health", f"{secs * 1000:.0f} ms  status={h.get('status')} "
                                        f"index={h.get('index_version')} pipeline={h.get('pipeline')}"))
        ok &= h.get("status") == "ok"
    except Exception as exc:                                # noqa: BLE001
        rows.append(("GET /api/health", f"ERROR {exc}"))
        ok = False
    try:
        _, body, secs = _req(a.base, "/")
        rows.append(("GET / (page)", f"{secs * 1000:.0f} ms  {len(body):,} bytes"))
    except Exception as exc:                                # noqa: BLE001
        rows.append(("GET / (page)", f"ERROR {exc}"))
        ok = False
    for label, text in (("POST /api/check  UI sample", SAMPLE), ("POST /api/check  AI probe", AI_PROBE)):
        try:
            res, secs = check_doc(a.base, text)
            extra = ""
            if text is AI_PROBE:
                extra = "  AI tier ACTIVE" if ai_found(res) else "  AI tier NOT ACTIVE (deterministic only)"
                ok &= ai_found(res) or a.allow_degraded
            rows.append((label, f"{secs:6.2f} s  verdict={res.get('document_verdict')}{extra}"))
        except Exception as exc:                                # noqa: BLE001
            rows.append((label, f"ERROR {exc}"))
            ok = False
    if not a.skip_bench:
        try:
            doc = bench_document(a.base)
            times = []
            for _ in range(a.reps):
                res, secs = check_doc(a.base, doc, timeout=300)
                times.append(secs)
            n = len(res.get("findings") or [])
            rows.append((f"POST /api/check  {len(doc.split())}-word doc",
                         " / ".join(f"{t:.1f}" for t in times) + f" s  findings={n} "
                         f"verdict={res.get('document_verdict')}"))
        except Exception as exc:                                # noqa: BLE001
            rows.append(("POST /api/check  bench doc", f"ERROR {exc}"))
            ok = False
    width = max(len(r[0]) for r in rows)
    print(f"MIZAN smoke test  {a.base}  {dt.datetime.now():%Y-%m-%d %H:%M}")
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")
    print("  RESULT" + " " * (width - 4) + ("  PASS" if ok else "  FAIL"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prewarm")
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--status-file")
    p.add_argument("--wait", type=float, default=120)
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(fn=cmd_prewarm)
    p = sub.add_parser("check")
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--ai", action="store_true")
    p.add_argument("--status-file")
    p.add_argument("--health-timeout", type=float, default=10)
    p.add_argument("--timeout", type=float, default=150)
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("smoke")
    p.add_argument("--base", required=True)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--skip-bench", action="store_true")
    p.add_argument("--allow-degraded", action="store_true",
                   help="do not fail when the AI tier is off (e.g. deliberate MIZAN_SEMANTIC=0)")
    p.set_defaults(fn=cmd_smoke)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
