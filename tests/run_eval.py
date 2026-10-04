#!/usr/bin/env python3
"""
MIZAN evaluation — rerun against the APPROVED index (quranpedia.net dumps).

A  CONTROL      approved text fed back in must never be flagged.  < 5%
B  IDENTITY     attributed to the RIGHT edition, not merely to one.
C  SEPARATION   is there room for a threshold at all?
D  REJECTION    a different verse's text must come back UNATTRIBUTED.

Fixed seed; a judge can rerun and get the same numbers.
"""
import json, os, random, statistics as st, sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from mizan.engine import Mizan, sim, T_MATCH, T_NEAR

random.seed(20261001)
HERE = os.path.dirname(os.path.abspath(__file__))
M = Mizan()
BAR = "=" * 74
REFS = [(r["surah"], r["ayah"]) for r in
        M.con.execute("SELECT DISTINCT surah, ayah FROM ayat ORDER BY surah, ayah")]


def test_control(n=400):
    print(BAR); print("A — CONTROL: approved text must never be flagged"); print(BAR)
    rows = 0; flagged = []; per = defaultdict(lambda: [0, 0])
    for lang, ids in sorted(M.languages().items()):
        if len(ids) < 2:
            continue
        for s, a in random.sample(REFS, n):
            for bid in ids:
                t = M.verse(bid, s, a)
                if not t or len(t) < 25:
                    continue
                r = M.attribute(t, s, a, lang)
                rows += 1; per[lang][1] += 1
                if r["state"] == "UNATTRIBUTED":
                    flagged.append((lang, bid, f"{s}:{a}", r["score"]))
                else:
                    per[lang][0] += 1
    bad = len(flagged)
    rate = bad / max(1, rows)
    print(f"  approved renderings tested : {rows:,}")
    print(f"  FALSE ALARMS               : {bad}  ({rate:.3%})")
    print(f"  verdict                    : {'PASS (<5%)' if rate < .05 else 'FAIL'}")
    for lang, (ok, tot) in sorted(per.items()):
        print(f"    {lang}  {ok}/{tot}  false alarms {tot-ok}")
    for f in flagged[:4]:
        print(f"    e.g. {f[0]} book {f[1]} {f[2]} score={f[3]}")
    return {"n": rows, "false_alarms": bad, "rate": round(rate, 5),
            "per_lang": {k: {"ok": v[0], "total": v[1]} for k, v in per.items()}}


def test_identity(n=400):
    print(); print(BAR); print("B — IDENTITY: the RIGHT edition?"); print(BAR)
    tot = ok = 0; per = defaultdict(lambda: [0, 0]); confus = defaultdict(int)
    for lang, ids in sorted(M.languages().items()):
        if len(ids) < 2:
            continue
        for s, a in random.sample(REFS, n):
            for bid in ids:
                t = M.verse(bid, s, a)
                if not t or len(t) < 25:
                    continue
                r = M.attribute(t, s, a, lang)
                tot += 1; per[lang][1] += 1
                if r["attributed_to"] == bid:
                    ok += 1; per[lang][0] += 1
                else:
                    confus[(bid, r["attributed_to"])] += 1
    print(f"  cases {tot:,} · correct edition {ok:,} ({ok/max(1,tot):.1%})")
    for lang, (o, t) in sorted(per.items()):
        print(f"    {lang}  {o}/{t}  ({o/max(1,t):.1%})")
    if confus:
        print("  most confused (true -> predicted):")
        for (x, y), c in sorted(confus.items(), key=lambda i: -i[1])[:4]:
            tx = (M.books.get(x) or {}).get("title", "?")[:30]
            ty = (M.books.get(y) or {}).get("title", "?")[:30] if y else "UNATTRIB"
            print(f"    {tx:32s} -> {ty:32s} {c}")
    return {"n": tot, "correct": ok, "accuracy": round(ok / max(1, tot), 4)}


def test_separation(n=500):
    print(); print(BAR); print("C — SEPARATION: same vs different edition"); print(BAR)
    diff = []
    en = M.editions("en")
    for s, a in random.sample(REFS, n):
        have = [b for b in en if M.verse(b, s, a)]
        if len(have) < 3:
            continue
        x = random.choice(have)
        y = random.choice([b for b in have if b != x])
        diff.append(sim(M.verse(x, s, a), M.verse(y, s, a)))
    over = sum(1 for d in diff if d >= T_MATCH)
    print(f"  different approved editions, same verse:")
    print(f"    mean {st.mean(diff):.3f} · median {st.median(diff):.3f} · max {max(diff):.3f}")
    print(f"    collisions >= MATCH ({T_MATCH}): {over}/{len(diff)} ({over/max(1,len(diff)):.1%})")
    print(f"  -> two APPROVED translations of one verse differ at mean {st.mean(diff):.2f}.")
    print(f"     No human memorises {len(en)} English translations to tell them apart.")
    return {"diff_mean": round(st.mean(diff), 3), "diff_median": round(st.median(diff), 3),
            "collisions": over, "n": len(diff)}


def test_rejection(n=300):
    print(); print(BAR); print("D — REJECTION: a different verse must be UNATTRIBUTED"); print(BAR)
    caught = tot = 0
    langs = [l for l, i in M.languages().items() if len(i) >= 2]
    for lang in langs:
        ids = M.editions(lang)
        for s, a in random.sample(REFS, n // len(langs) + 5):
            os_, oa = random.choice(REFS)
            if (os_, oa) == (s, a):
                continue
            t = M.verse(random.choice(ids), os_, oa)
            if not t or len(t) < 40:
                continue
            r = M.attribute(t, s, a, lang)
            tot += 1
            if r["state"] == "UNATTRIBUTED":
                caught += 1
    print(f"  wrong-verse texts tested : {tot}")
    print(f"  correctly UNATTRIBUTED   : {caught} ({caught/max(1,tot):.1%})")
    return {"n": tot, "caught": caught, "recall": round(caught / max(1, tot), 4)}


if __name__ == "__main__":
    n_ayat = M.con.execute("SELECT COUNT(*) c FROM ayat").fetchone()["c"]
    print(f"APPROVED index: quranpedia.net dump {M.version}")
    print(f"{len(M.books)} translations · {n_ayat:,} verse renderings\n")
    out = {"index": {"source": "quranpedia.net", "version": M.version,
                     "translations": len(M.books), "renderings": n_ayat}}
    out["control"] = test_control()
    out["identity"] = test_identity()
    out["separation"] = test_separation()
    out["rejection"] = test_rejection()
    json.dump(out, open(os.path.join(HERE, "results_approved.json"), "w"), indent=2)
    print(); print(BAR); print("SUMMARY (approved index)"); print(BAR)
    print(f"  A control false alarms : {out['control']['rate']:.3%}")
    print(f"  B correct edition      : {out['identity']['accuracy']:.1%}")
    print(f"  C approved-vs-approved : {out['separation']['diff_mean']:.2f} mean")
    print(f"  D rejection recall     : {out['rejection']['recall']:.1%}")
    print("  results_approved.json written")
