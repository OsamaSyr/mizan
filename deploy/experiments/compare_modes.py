#!/usr/bin/env python3
"""Compare two bench_encoder.py outputs: did retrieval or any verdict change?

  python3 deploy/experiments/compare_modes.py result-fp32.json result-int8.json
"""
import json
import sys

import numpy as np

a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
va, vb = (np.load(p.replace(".json", ".vecs.npz")) for p in sys.argv[1:3])

out = {"modes": [a["mode"], b["mode"]]}
cos = []
for k in va.files:
    x, y = va[k], vb[k]
    cos.extend((x * y).sum(1) / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1)))
cos = np.array(cos)
out["embedding_cosine_fp32_vs_other"] = {"n": int(cos.size), "mean": round(float(cos.mean()), 4),
                                         "min": round(float(cos.min()), 4)}

qa = {(q["lang"], q["ref"], i): q for i, q in enumerate(a["queries"])}
qb = {(q["lang"], q["ref"], i): q for i, q in enumerate(b["queries"])}
per_lang: dict = {}
for key in qa:
    lg, ref, _ = key
    ta, tb = qa[key]["top"], qb[key]["top"]
    d = per_lang.setdefault(lg, {"n": 0, "top1_same": 0, "top5_set_same": 0,
                                 f"R@1_{a['mode']}": 0, f"R@1_{b['mode']}": 0,
                                 f"R@5_{a['mode']}": 0, f"R@5_{b['mode']}": 0})
    d["n"] += 1
    d["top1_same"] += ta[0] == tb[0]
    d["top5_set_same"] += set(ta[:5]) == set(tb[:5])
    d[f"R@1_{a['mode']}"] += ta[0] == ref
    d[f"R@1_{b['mode']}"] += tb[0] == ref
    d[f"R@5_{a['mode']}"] += ref in ta[:5]
    d[f"R@5_{b['mode']}"] += ref in tb[:5]
out["retrieval_real_quotes"] = per_lang
same = {}
for name in a["findings"]:
    fa = [(f["ref"], f["state"], f["tier"], f["attributed_to"]) for f in a["findings"][name]]
    fb = [(f["ref"], f["state"], f["tier"], f["attributed_to"]) for f in b["findings"][name]]
    same[name] = {"identical_findings": fa == fb, "n": [len(fa), len(fb)]}
    if fa != fb:
        same[name]["only_" + a["mode"]] = sorted(set(fa) - set(fb), key=str)
        same[name]["only_" + b["mode"]] = sorted(set(fb) - set(fa), key=str)
out["check_document_findings"] = same
out["latency"] = {m["mode"]: {"encode_long_chunks_s": m["encode_long_chunks_s"],
                              "check_document_s": m["check_document_s"]} for m in (a, b)}
out["memory"] = {m["mode"]: {"after_load": m["mem_after_load"], "end": m["mem_end"],
                             "after_quant": m.get("mem_after_quant")} for m in (a, b)}
print(json.dumps(out, indent=1, ensure_ascii=False))
