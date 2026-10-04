#!/usr/bin/env python3
"""
In-process breakdown of the semantic tier on CPU, fp32 vs int8 dynamic quant.

Does NOT modify src/mizan/semantic.py: it builds the project's own Encoder,
optionally swaps its model for a torch dynamic-int8 copy, installs it as the
process-wide encoder, and then runs the project's own code paths.

  python deploy/experiments/bench_encoder.py --mode fp32 --docs DIR \
      --queries queries.jsonl --out result-fp32.json
  python deploy/experiments/bench_encoder.py --mode int8 ...

queries.jsonl: {"lang","text","ref":"S:A"} per line (built from the local real
corpus by the caller; third-party text, kept outside the repo).
Run compare_modes.py on two outputs to see whether retrieval changed.
"""
import argparse
import gc
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("MIZAN_DEVICE", "cpu")

PROBE = ("Many readers find comfort in one passage above all. "
         "O humanity, We formed you from one male and one female and arranged you "
         "into peoples and tribes so that you might come to know one another; the "
         "noblest of you before God is the one most conscious of Him. "
         "It is recited at many weddings.")


def mem_mb() -> dict:
    out = {}
    try:
        for line in open("/proc/self/status"):
            if line.startswith(("VmRSS", "VmHWM")):
                k, v = line.split(":")
                out[k] = round(int(v.split()[0]) / 1024)
    except OSError:                      # macOS: ru_maxrss is bytes
        import resource
        out["VmHWM"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("fp32", "int8", "onnx-fp32", "onnx-int8", "mps"), default="fp32")
    ap.add_argument("--onnx-dir", default="/results/onnx")
    ap.add_argument("--docs", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--with-reranker", action="store_true",
                    help="also load bge-reranker-v2-m3 and report the extra RAM")
    a = ap.parse_args()

    import numpy as np
    import torch
    from mizan import semantic as S
    from mizan.detect import SpanDetector, semantic_chunks
    from mizan.engine import Mizan
    from mizan.report import check_document

    res = {"mode": a.mode, "torch": torch.__version__, "threads": torch.get_num_threads(),
           "quant_engines": list(torch.backends.quantized.supported_engines),
           "mem_start": mem_mb()}

    if a.mode.startswith("onnx"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import onnx_encoder as OX
        import onnxruntime
        res["onnxruntime"] = onnxruntime.__version__
        fp32_path = os.path.join(a.onnx_dir, "fp32", "bge-m3.onnx")
        if not os.path.exists(fp32_path):
            res["export_s"] = round(OX.export(fp32_path), 1)
            gc.collect()
        path = fp32_path
        if a.mode == "onnx-int8":
            path = os.path.join(a.onnx_dir, "int8", "bge-m3.int8.onnx")
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                res["quantize_s"] = round(OX.quantize(fp32_path, path), 1)
                gc.collect()
        res["mem_before_load"] = mem_mb()
        t0 = time.perf_counter()
        enc = OX.OrtEncoder(path, threads=torch.get_num_threads())
        res["load_s"] = round(time.perf_counter() - t0, 2)
    else:
        t0 = time.perf_counter()
        enc = S.Encoder(device="mps" if a.mode == "mps" else "cpu")
        res["load_s"] = round(time.perf_counter() - t0, 2)
    res["mem_after_load"] = mem_mb()

    if a.mode == "int8":
        eng = "qnnpack" if "qnnpack" in torch.backends.quantized.supported_engines and \
            os.uname().machine in ("aarch64", "arm64") else torch.backends.quantized.engine
        if eng and eng != "none":
            torch.backends.quantized.engine = eng
        res["quant_engine"] = torch.backends.quantized.engine
        t0 = time.perf_counter()
        from torch.ao.quantization import quantize_dynamic
        enc.model = quantize_dynamic(enc.model, {torch.nn.Linear}, dtype=torch.qint8)
        gc.collect()
        res["quantize_s"] = round(time.perf_counter() - t0, 2)
        res["mem_after_quant"] = mem_mb()
    S._ENCODER = enc                     # the process-wide encoder the app would use

    langs = ["en", "ur", "hi", "bn", "tl"]
    for lg in langs:
        if S.available(lg):
            S.load_index(lg)
    res["mem_after_indexes"] = mem_mb()

    long_doc = open(os.path.join(a.docs, "long.txt"), encoding="utf-8").read()
    short_doc = open(os.path.join(a.docs, "short.txt"), encoding="utf-8").read()
    words = long_doc.split()
    chunks = [" ".join(words[x:y]) for x, y in semantic_chunks(long_doc)]
    ntok = [len(enc.tok(c, truncation=True, max_length=S.QUERY_MAX_TOKENS)["input_ids"])
            for c in chunks]
    res["long_doc"] = {"words": len(words), "chunks": len(chunks),
                       "tokens_total": sum(ntok), "tokens_max": max(ntok),
                       "tokens_median": statistics.median(ntok)}
    enc.encode(chunks[:4])                       # warm kernels
    times = []
    for _ in range(a.reps):
        t0 = time.perf_counter()
        long_vecs = enc.encode(chunks)
        times.append(round(time.perf_counter() - t0, 2))
    res["encode_long_chunks_s"] = times
    res["mem_after_encode"] = mem_mb()

    # End-to-end detector, same objects the app builds.
    m = Mizan()
    det = SpanDetector(m, "en")
    check_document(m, short_doc, "en", detector=det)       # build caches
    timings, findings = {}, {}
    for name, doc in (("short", short_doc), ("long", long_doc), ("probe", PROBE)):
        ts = []
        for _ in range(a.reps):
            t0 = time.perf_counter()
            rep = check_document(m, doc, "en", detector=det)
            ts.append(round(time.perf_counter() - t0, 2))
        timings[name] = ts
        d = rep.as_dict()
        findings[name] = [{k: f.get(k) for k in ("ref", "state", "tier", "attributed_to",
                                                  "start_word", "end_word")}
                          | {"score": round(float(f.get("score") or 0), 3)}
                          for f in d.get("findings") or []]
    res["check_document_s"] = timings
    res["findings"] = findings
    res["semantic_used_last"] = det.last_semantic

    # Retrieval on real published quotations.
    qs = [json.loads(l) for l in open(a.queries, encoding="utf-8")]
    out_q = []
    by_lang: dict = {}
    for q in qs:
        by_lang.setdefault(q["lang"], []).append(q)
    vec_dump = {}
    for lg, items in by_lang.items():
        if not S.available(lg):
            continue
        vecs = enc.encode([q["text"] for q in items])
        hits = S.load_index(lg).search(vecs, top_k=20)
        vec_dump[lg] = vecs
        for q, h in zip(items, hits):
            out_q.append({"lang": lg, "ref": q["ref"], "top": [f"{s}:{x}" for s, x, _ in h],
                          "top_scores": [sc for _, _, sc in h[:5]]})
    res["queries"] = out_q
    np.savez_compressed(a.out.replace(".json", ".vecs.npz"), long=long_vecs,
                        **{f"q_{k}": v for k, v in vec_dump.items()})

    if a.with_reranker:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        rdir = S._model_dir(S.RERANKER_ID, S.RERANKER_REVISION)
        t0 = time.perf_counter()
        rtok = AutoTokenizer.from_pretrained(rdir, local_files_only=True)
        rmod = AutoModelForSequenceClassification.from_pretrained(rdir, local_files_only=True).eval()
        res["reranker_load_s"] = round(time.perf_counter() - t0, 2)
        with torch.inference_mode():
            pairs = [[chunks[0], chunks[1]]] * 20
            t0 = time.perf_counter()
            rmod(**rtok(pairs, padding=True, truncation=True, max_length=512, return_tensors="pt"))
            res["reranker_20_pairs_s"] = round(time.perf_counter() - t0, 2)
        res["mem_with_reranker"] = mem_mb()

    res["mem_end"] = mem_mb()
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, ensure_ascii=False)
    brief = {k: v for k, v in res.items() if k not in ("queries", "findings")}
    print(json.dumps(brief, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
