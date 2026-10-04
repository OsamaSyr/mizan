#!/usr/bin/env python3
"""
Precompute the semantic index for MIZAN's tier (c).

    .venv/bin/python scripts/build_embeddings.py --download     # once: fetch pinned model
    .venv/bin/python scripts/build_embeddings.py                # build data/embeddings/

What gets embedded, and why
---------------------------
Chosen by measurement on a dev split of held-out translations (docs/SEMANTIC.md
has the tables), not by assumption. For every indexed language, each verse is
represented by TWO vectors that are searched together:

  * the CENTROID of its approved renderings in that language (mean of the
    per-edition vectors, re-normalised)                         weight 0.8
  * the Arabic verse (Tanzil simple-clean), shared by all languages  weight 0.2

Measured alternatives, on the same 2,746 dev queries:
  * Arabic only (rely on bge-m3's cross-lingual space): clearly worst — full
    English verses R@1 0.844 vs 0.980 for the centroid.
  * Every edition kept separately (max over editions): equal raw accuracy but
    WORSE once a threshold must reject ordinary prose (paired +27/-70 vs the
    centroid), because fifteen vectors per verse give prose fifteen chances to
    land near one. It is also 15x larger for English (190 MB) — over GitHub's
    100 MB per-file limit for a repository that must be public.
  * Centroid + Arabic at 0.8/0.2 beat the centroid alone (paired +85/-26 raw,
    +49/-18 calibrated), so that is what ships.

The per-edition vectors are still computed (the centroid needs them) and can be
kept with --keep-editions DIR, which is how the design experiment was run.

Text preparation
----------------
`semantic.clean_rendering` cuts every rendering at its footnote separator and
strips footnote markers before embedding; the Bengali edition 1967 stores up to
1,272 words under one verse, nearly all of it commentary. The remaining text is
truncated at DOC_MAX_TOKENS (256), which keeps the verse — it is always first.

Determinism
-----------
Same corpus + same pinned model revision -> same vectors up to fp16 rounding
on the GPU backend (measured cosine agreement fp16 vs fp32 >= 0.99994). The
manifest records model, revision, dimension, corpus version and build date.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mizan import semantic as S          # noqa: E402
from mizan.engine import DB              # noqa: E402

DESIGN = "renderings-centroid+arabic"
DESIGNS = ("renderings-centroid+arabic", "renderings-centroid", "arabic")
CENTROID_WEIGHT = dict(S.DEFAULT_FUSION)["renderings:centroid"]


def download() -> None:
    """Fetch exactly the files the dense encoder (and optional reranker) need."""
    from huggingface_hub import snapshot_download
    files = ["config.json", "sentencepiece.bpe.model", "tokenizer.json",
             "tokenizer_config.json", "special_tokens_map.json"]
    p = snapshot_download(S.MODEL_ID, revision=S.MODEL_REVISION,
                          allow_patterns=files + ["pytorch_model.bin"])
    print("model   ", p)


def verse_order(con: sqlite3.Connection) -> list[tuple[int, int]]:
    """Canonical (surah, ayah) order: the 6,236 Tanzil verses."""
    return [(s, a) for s, a in con.execute(
        "SELECT surah, ayah FROM arabic_ayat ORDER BY surah, ayah")]


def embed_book(con: sqlite3.Connection, enc: "S.Encoder", book_id: int,
               refs: list[tuple[int, int]], batch: int):
    """
    One edition -> (vectors aligned to `refs`, present-mask).

    A few editions lack some verses (27833 has 6,224 of 6,236); those rows are
    zero and masked out so they cannot drag a centroid toward the origin.
    """
    import numpy as np
    rows = {(s, a): t for s, a, t in con.execute(
        "SELECT surah, ayah, text FROM ayat WHERE book_id=?", (book_id,))}
    texts, present = [], np.zeros(len(refs), dtype=bool)
    for i, r in enumerate(refs):
        t = S.clean_rendering(rows.get(r, ""))
        present[i] = bool(t)
        texts.append(t)
    keep = [i for i in range(len(refs)) if present[i]]
    vecs = np.zeros((len(refs), S.EMBED_DIM), dtype=np.float32)
    vecs[keep] = enc.encode([texts[i] for i in keep],
                            max_length=S.DOC_MAX_TOKENS, batch_size=batch)
    return vecs, present


def embed_arabic(con: sqlite3.Connection, enc: "S.Encoder",
                 refs: list[tuple[int, int]], batch: int):
    rows = {(s, a): t for s, a, t in con.execute(
        "SELECT surah, ayah, text_simple FROM arabic_ayat")}
    texts = [S.clean_arabic(rows[r], *r) for r in refs]
    return enc.encode(texts, max_length=S.DOC_MAX_TOKENS, batch_size=batch)


def centroid(stack, masks):
    """Mean of unit vectors per verse over the editions that have it, re-normalised."""
    import numpy as np
    total = np.zeros_like(stack[0])
    count = np.zeros(len(stack[0]), dtype=np.float32)
    for v, m in zip(stack, masks):
        total[m] += v[m]
        count += m
    total /= np.maximum(count, 1)[:, None]
    norms = np.linalg.norm(total, axis=1, keepdims=True)
    return total / np.maximum(norms, 1e-12), count > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true",
                    help="download the pinned model snapshot and exit")
    ap.add_argument("--design", choices=DESIGNS, default=DESIGN)
    ap.add_argument("--langs", default="",
                    help="comma list; default = every language in the index")
    ap.add_argument("--out", default=S.EMB_DIR)
    ap.add_argument("--batch", type=int, default=None,
                    help="encoder batch size (default: 4 on CPU, 32 on GPU/MPS; "
                         "see semantic.CPU_BATCH)")
    ap.add_argument("--keep-editions", default="",
                    help="also save every per-edition matrix to this directory "
                         "(used by the index-design experiment)")
    args = ap.parse_args()

    if args.download:
        download()
        return 0

    import numpy as np
    con = sqlite3.connect(DB)
    books = list(con.execute("SELECT book_id, language, version FROM translations"))
    corpus_version = books[0][2] if books else "?"
    langs = sorted({lg for _, lg, _ in books})
    if args.langs:
        langs = [lg for lg in args.langs.split(",") if lg]
    refs = verse_order(con)

    t_all = time.perf_counter()
    enc = S.Encoder()
    print(f"model {S.MODEL_ID}@{S.MODEL_REVISION[:8]} on {enc.device} "
          f"(fp16={enc.fp16}), loaded in {enc.load_seconds:.1f}s", flush=True)
    os.makedirs(args.out, exist_ok=True)
    if args.keep_editions:
        os.makedirs(args.keep_editions, exist_ok=True)

    refs_file = "refs.json"
    with open(os.path.join(args.out, refs_file), "w", encoding="utf-8") as fh:
        json.dump([list(r) for r in refs], fh)

    manifest = {
        "refs": refs_file, "n_verses": len(refs),
        "model": S.MODEL_ID, "model_revision": S.MODEL_REVISION,
        "dimension": S.EMBED_DIM, "dtype_on_disk": "float16",
        "pooling": "CLS, L2-normalised (bge-m3 dense)",
        "doc_max_tokens": S.DOC_MAX_TOKENS,
        "design": args.design,
        "corpus": os.path.relpath(DB, ROOT), "corpus_version": corpus_version,
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "device": enc.device, "languages": {}, "timings_s": {},
    }

    want_arabic = "arabic" in args.design or args.keep_editions
    arabic_file = None
    if want_arabic:
        t0 = time.perf_counter()
        ar = embed_arabic(con, enc, refs, args.batch)
        manifest["timings_s"]["arabic"] = round(time.perf_counter() - t0, 1)
        if "arabic" in args.design:
            arabic_file = "arabic.f16.npy"
            np.save(os.path.join(args.out, arabic_file), ar.astype(np.float16))
        if args.keep_editions:
            np.save(os.path.join(args.keep_editions, "arabic.npy"), ar.astype(np.float16))
        print(f"arabic   6236 verses  {manifest['timings_s']['arabic']}s", flush=True)

    for lang in langs:
        parts = []
        if "renderings" in args.design:
            t0 = time.perf_counter()
            ids = [b for b, lg, _ in books if lg == lang]
            stack, masks = [], []
            for b in ids:
                tb = time.perf_counter()
                v, m = embed_book(con, enc, b, refs, args.batch)
                stack.append(v)
                masks.append(m)
                if args.keep_editions:
                    np.save(os.path.join(args.keep_editions, f"book_{b}.npy"),
                            v.astype(np.float16))
                    np.save(os.path.join(args.keep_editions, f"book_{b}.mask.npy"), m)
                print(f"  {lang} book {b}: {int(m.sum())} renderings "
                      f"{time.perf_counter() - tb:.1f}s", flush=True)
            cen, has = centroid(stack, masks)
            cen[~has] = 0.0          # verse absent from every edition: scores 0
            vec_file = f"{lang}.centroid.f16.npy"
            np.save(os.path.join(args.out, vec_file), cen.astype(np.float16))
            weight = CENTROID_WEIGHT if "arabic" in args.design else 1.0
            parts.append({"source": "renderings:centroid", "vectors": vec_file,
                          "weight": weight, "editions": ids,
                          "verses_covered": int(has.sum())})
            manifest["timings_s"][lang] = round(time.perf_counter() - t0, 1)
        if arabic_file:
            weight = round(1.0 - CENTROID_WEIGHT, 6) if "renderings" in args.design else 1.0
            parts.append({"source": "arabic:tanzil-simple", "vectors": arabic_file,
                          "weight": weight, "verses_covered": len(refs)})
        manifest["languages"][lang] = {"parts": parts}

    manifest["timings_s"]["total"] = round(time.perf_counter() - t_all, 1)
    with open(os.path.join(args.out, S.INDEX_JSON), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    size = sum(os.path.getsize(os.path.join(args.out, f)) for f in os.listdir(args.out)
               if os.path.isfile(os.path.join(args.out, f)))
    print(f"wrote {args.out}  ({size / 1e6:.1f} MB)  in {manifest['timings_s']['total']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
