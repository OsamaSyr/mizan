#!/usr/bin/env python3
"""
MIZAN — the semantic retrieval tier (tier c), running BAAI/bge-m3 locally.

What it is for
--------------
The lexical tier finds a quotation by the rare words it shares with an approved
rendering. A passage taken from a translation we do NOT index (Asad, Arberry,
an in-house rendering) shares far fewer of them, so the lexical tier either
misses it or pins it to the wrong verse. bge-m3 maps text with the same meaning
to nearby vectors regardless of wording — and regardless of language — so it
can say "this sentence is about 2:255" when the words alone cannot.

What it is NOT allowed to do
----------------------------
It proposes verse ids. That is all. It returns (surah, ayah, score) tuples and
never a string: no verse text, no corrected text, no explanation. Whether a
located passage MATCHes, is NEAR, or is UNATTRIBUTED is decided afterwards by
`engine.sim()` against the approved published string, exactly as before. The
model has no authority over the verdict, and because it never emits text it
cannot put words in scripture's mouth even by accident.

Degraded mode
-------------
Everything here is optional. numpy / torch / transformers are imported lazily,
and `available()` answers "can the semantic tier run?" WITHOUT importing them.
With plain `python3` and no ML packages the detector runs its deterministic
path unchanged; that is the documented fallback for this critical dependency.
When the tier is asked to run but cannot, it raises `SemanticUnavailable`
rather than returning an empty list, so "nothing found" and "could not look"
are never confused.

Index
-----
Vectors are precomputed by `scripts/build_embeddings.py` into
`data/embeddings/`. The design (which texts are embedded, and why) was chosen
by measurement; see docs/SEMANTIC.md and `index.json` in that directory.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:                      # pragma: no cover - typing only
    import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
EMB_DIR = os.environ.get("MIZAN_EMB_DIR", os.path.join(ROOT, "data", "embeddings"))
INDEX_JSON = "index.json"

# Pinned so a silent upstream re-upload cannot change every measured number.
MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
RERANKER_ID = "BAAI/bge-reranker-v2-m3"
RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
EMBED_DIM = 1024

# Token budget per text. Queries are spans of a document (8-120 words) and fit
# easily. Renderings are cut at their footnote separator first (see
# clean_rendering) and then truncated here; the verse itself always comes
# first in every indexed edition, so truncation drops commentary, not verse.
QUERY_MAX_TOKENS = 192
DOC_MAX_TOKENS = 256

# Encoder batch sizes. Measured on 4 Linux vCPUs (torch 2.14.1+cpu) for the 28
# chunks the detector embeds from a 987-word article: batch 1 2.38 s, 4 1.92 s,
# 8 2.13 s, 16 2.45 s, 32 3.92 s; embeddings identical to within 2e-7, far
# below the 4-decimal rounding applied to scores. One batch of 32 pads every
# chunk to the longest one, and on a CPU padding is paid for in full.
CPU_BATCH = 4
GPU_BATCH = 32

# Scores are rounded before ranking so that float noise between batch layouts
# (padding changes the reduction order on GPU backends) can never reorder two
# candidates. 4 decimals is far below any threshold we act on.
SCORE_DECIMALS = 4

_FOOTNOTE_SPLIT = re.compile(r"_{4,}")
_FOOTNOTE_MARK = re.compile(r"\[\s*[0-9০-৯٠-٩۰-۹०-९]+\s*\]")
# A leading verse number in any of the forms the editions use: "(2:255)",
# "(2)", "2." or "2)".
_LEAD_REF = re.compile(r"^\s*(?:\(\s*\d{1,3}\s*(?::\s*\d{1,3}\s*)?\)|\d{1,3}\s*[.)])\s*")
_BASMALA = "بسم الله الرحمن الرحيم"


class SemanticUnavailable(RuntimeError):
    """The semantic tier was asked to run but cannot (deps, model or index missing)."""


# ---------------------------------------------------------------- text prep

def clean_rendering(text: str) -> str:
    """
    The part of an approved rendering worth embedding.

    Several indexed editions append translator footnotes after a line of
    underscores, and the Bengali edition 1967 runs to 1,200+ words of
    commentary under a 60-word verse. Embedding the commentary would place the
    vector "near" every topic the footnote discusses, so we keep only what
    precedes the separator, drop inline footnote markers ("[8]", "[১]") and a
    leading verse number. Commentary printed INLINE (no separator) cannot be
    separated mechanically and is left to the token cap.
    """
    t = _FOOTNOTE_SPLIT.split(text or "", maxsplit=1)[0]
    t = _FOOTNOTE_MARK.sub(" ", t)
    t = _LEAD_REF.sub("", t)
    return " ".join(t.split())


def clean_arabic(text: str, surah: int, ayah: int) -> str:
    """
    Tanzil's simple-clean text prefixes the basmala to ayah 1 of every surah
    except 1 and 9. It is not part of that verse's meaning, and leaving it in
    makes 113 different first verses look alike.
    """
    t = " ".join((text or "").split())
    if ayah == 1 and surah not in (1, 9) and t.startswith(_BASMALA):
        t = t[len(_BASMALA):].strip()
    return t


# ---------------------------------------------------------------- availability

def deps_available() -> bool:
    """True if numpy, torch and transformers are importable. Imports nothing."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None
               for m in ("numpy", "torch", "transformers"))


def _model_dir(repo: str, revision: str) -> str | None:
    """Local snapshot directory for a pinned model, or None if not downloaded."""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME", os.path.join(os.path.expanduser("~"), ".cache",
                                                "huggingface")), "hub")
    path = os.path.join(hub, "models--" + repo.replace("/", "--"), "snapshots", revision)
    return path if os.path.isfile(os.path.join(path, "config.json")) else None


def index_manifest() -> dict | None:
    """Parsed data/embeddings/index.json, or None if the index was never built."""
    path = os.path.join(EMB_DIR, INDEX_JSON)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def available(lang: str | None = None) -> bool:
    """
    Can the semantic tier run (for `lang`, if given)? Cheap: imports no ML code.

    Three things must all be true: the ML packages are installed, the pinned
    model snapshot is on disk (we never download at request time), and the
    precomputed index exists (for that language). `MIZAN_SEMANTIC=0` forces
    False, so the deterministic path can be measured on a machine that has
    everything installed.
    """
    if os.environ.get("MIZAN_SEMANTIC", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    if not deps_available() or _model_dir(MODEL_ID, MODEL_REVISION) is None:
        return False
    man = index_manifest()
    if not man:
        return False
    if lang is not None and lang not in man.get("languages", {}):
        return False
    return True


def status() -> dict:
    """Human-readable state of the AI layer, for a UI badge or /health."""
    man = index_manifest() or {}
    return {
        "semantic_available": available(),
        "deps_installed": deps_available(),
        "model": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_on_disk": _model_dir(MODEL_ID, MODEL_REVISION) is not None,
        "index_built": bool(man),
        "index_languages": sorted(man.get("languages", {})),
        "index_design": man.get("design"),
        "loaded": _ENCODER is not None,
        "load_failure": (None if _LOAD_FAILURE is None else {
            "attempts": _LOAD_FAILURE["attempts"], "error": _LOAD_FAILURE["error"],
            "retry_in_s": max(0, round(_LOAD_FAILURE["retry_at"] - time.time()))}),
        "fallback": "deterministic lexical tier (no model)",
    }


# ---------------------------------------------------------------- encoder

def pick_device() -> str:
    """Apple MPS when present, else CUDA, else CPU. `MIZAN_DEVICE` overrides."""
    forced = os.environ.get("MIZAN_DEVICE")
    if forced:
        return forced
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class Encoder:
    """
    bge-m3 dense encoder: CLS pooling, L2-normalised, as the model card specifies.

    Loaded once per process (see `encoder()`); loading costs a few seconds and
    ~2.3 GB of memory, encoding a page of text costs well under a second.
    """

    def __init__(self, device: str | None = None, fp16: bool | None = None):
        model_dir = _model_dir(MODEL_ID, MODEL_REVISION)
        if model_dir is None:
            raise SemanticUnavailable(
                f"{MODEL_ID}@{MODEL_REVISION[:8]} is not on disk. Run "
                f"scripts/build_embeddings.py --download first.")
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:                       # pragma: no cover
            raise SemanticUnavailable(f"ML dependencies missing: {exc}") from exc

        self.torch = torch
        self.device = device or pick_device()
        # fp16 on GPU backends halves memory and roughly doubles throughput;
        # on CPU fp16 matmul is slow, so stay in fp32 there.
        self.fp16 = (self.device != "cpu") if fp16 is None else fp16
        t0 = time.perf_counter()
        self.tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
        model = AutoModel.from_pretrained(model_dir, local_files_only=True)
        if self.fp16:
            model = model.half()
        self.model = model.to(self.device).eval()
        self.load_seconds = time.perf_counter() - t0
        self._lock = threading.Lock()

    def encode(self, texts: Sequence[str], max_length: int = QUERY_MAX_TOKENS,
               batch_size: int | None = None) -> "np.ndarray":
        """
        Embed `texts` -> float32 array (n, 1024), each row unit-length.

        Texts are processed in length-sorted batches: padding a 10-token span
        to the length of a 190-token one wastes most of the batch. The output
        is returned in the caller's order.

        Batch size defaults by device (see CPU_BATCH): on a CPU every padded
        token costs as much as a real one, so small batches win; a GPU
        amortises padding and prefers large ones.
        """
        import numpy as np
        torch = self.torch
        if batch_size is None:
            batch_size = int(os.environ.get(
                "MIZAN_BATCH", CPU_BATCH if self.device == "cpu" else GPU_BATCH))
        n = len(texts)
        out = np.zeros((n, EMBED_DIM), dtype=np.float32)
        if n == 0:
            return out
        order = sorted(range(n), key=lambda i: len(texts[i]))
        with self._lock, torch.inference_mode():
            for b in range(0, n, batch_size):
                idx = order[b:b + batch_size]
                enc = self.tok([texts[i] or " " for i in idx], padding=True,
                               truncation=True, max_length=max_length,
                               return_tensors="pt").to(self.device)
                hidden = self.model(**enc).last_hidden_state[:, 0]
                hidden = torch.nn.functional.normalize(hidden.float(), dim=-1)
                out[idx] = hidden.cpu().numpy()
        return out


_ENCODER: Encoder | None = None
_ENCODER_LOCK = threading.Lock()

# A failed model load is remembered. Without this, a server whose load fails
# (out of memory, a corrupt or missing snapshot) retried the 2.2 GB load on
# EVERY check: measured +4 s per check and 4.3 GB memory peaks, while every
# answer still came from the deterministic path. After a failure, encoder()
# raises immediately until the back-off expires: 60 s, then doubling per
# consecutive failure, capped at one hour. MIZAN_SEMANTIC_RETRY_S overrides
# the first interval (0 = retry on every call, the old behaviour).
LOAD_RETRY_FIRST_S = 60.0
LOAD_RETRY_MAX_S = 3600.0
_LOAD_FAILURE: dict | None = None


def _retry_after(attempts: int) -> float:
    first = float(os.environ.get("MIZAN_SEMANTIC_RETRY_S", LOAD_RETRY_FIRST_S))
    return min(LOAD_RETRY_MAX_S, first * (2 ** max(0, attempts - 1)))


def encoder() -> Encoder:
    """The process-wide encoder, loaded on first use (see _LOAD_FAILURE)."""
    global _ENCODER, _LOAD_FAILURE
    if _ENCODER is not None:
        return _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is not None:
            return _ENCODER
        if not deps_available():
            raise SemanticUnavailable(
                "numpy/torch/transformers are not installed; "
                "pip install -r requirements-ml.txt")
        now = time.time()
        if _LOAD_FAILURE is not None and now < _LOAD_FAILURE["retry_at"]:
            raise SemanticUnavailable(
                f"model load failed {_LOAD_FAILURE['attempts']}x "
                f"({_LOAD_FAILURE['error']}); not retrying for another "
                f"{_LOAD_FAILURE['retry_at'] - now:.0f} s")
        try:
            enc = Encoder()
        except Exception as exc:                          # noqa: BLE001
            attempts = (_LOAD_FAILURE or {}).get("attempts", 0) + 1
            _LOAD_FAILURE = {"attempts": attempts,
                             "error": f"{type(exc).__name__}: {exc}"[:300],
                             "failed_at": now, "retry_at": now + _retry_after(attempts)}
            raise SemanticUnavailable(
                f"model load failed: {_LOAD_FAILURE['error']}") from exc
        _ENCODER, _LOAD_FAILURE = enc, None
    return _ENCODER


def count_tokens(texts: Sequence[str]) -> list[int]:
    """Tokens each text will occupy in the encoder (after truncation).

    Tokenising is cheap next to the forward pass, so the detector uses this to
    spend its per-document token budget exactly rather than by word counts.
    """
    tok = encoder().tok
    ids = tok(list(texts), truncation=True, max_length=QUERY_MAX_TOKENS)["input_ids"]
    return [len(x) for x in ids]


# ---------------------------------------------------------------- the index

# Weight of the same-language centroid in the fused score; the Arabic verse
# vector gets the remainder. Chosen on the DEV split (docs/SEMANTIC.md):
# centroid-only vs 0.8/0.2 fusion, paired over 2,746 dev queries, R@1 moved
# +85/-26, and +49/-18 at a threshold admitting 1% of negative sentences.
# Arabic alone was far worse (en full-verse R@1 0.844 vs 0.980), so the
# Arabic vector is a tie-breaker here, not the primary signal.
DEFAULT_FUSION = (("renderings:centroid", 0.8), ("arabic:tanzil-simple", 0.2))


@dataclass
class VerseIndex:
    """
    One language's searchable vectors: score = sum_k weight_k * cos(q, M_k[verse]).

    Every part is aligned to the same canonical list of 6,236 verse ids, so a
    fused score is a single matrix product per part. A verse missing from a
    part has a zero row there and simply scores 0 for that part.
    """

    lang: str
    refs: list[tuple[int, int]]
    parts: list[tuple[str, float, "np.ndarray"]]      # (source, weight, (n, 1024))

    @property
    def source(self) -> str:
        return " + ".join(f"{w:g}*{src}" for src, w, _ in self.parts)

    def score_matrix(self, queries: "np.ndarray") -> "np.ndarray":
        """(q, n_verses) fused similarity."""
        out = None
        for _, w, mat in self.parts:
            s = (queries @ mat.T) * w
            out = s if out is None else out + s
        return out

    def search(self, queries: "np.ndarray", top_k: int = 20) -> list[list[tuple[int, int, float]]]:
        """Top-k verses per query as (surah, ayah, score in [0,1]), best first."""
        import numpy as np
        if len(queries) == 0:
            return []
        sims = self.score_matrix(queries)
        k = min(top_k, sims.shape[1])
        part = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        out = []
        for qi in range(sims.shape[0]):
            row = [(self.refs[j][0], self.refs[j][1],
                    round(float(min(1.0, max(0.0, sims[qi, j]))), SCORE_DECIMALS))
                   for j in part[qi]]
            row.sort(key=lambda t: (-t[2], t[0], t[1]))
            out.append(row)
        return out


_INDEXES: dict[str, VerseIndex] = {}
_INDEX_LOCK = threading.Lock()
_ARRAYS: dict[str, "np.ndarray"] = {}       # shared parts (the Arabic matrix) load once


def _load_array(name: str) -> "np.ndarray":
    import numpy as np
    if name not in _ARRAYS:
        _ARRAYS[name] = np.load(os.path.join(EMB_DIR, name)).astype(np.float32)
    return _ARRAYS[name]


def load_index(lang: str) -> VerseIndex:
    """Load (and cache) the precomputed vectors used to search `lang`."""
    if lang in _INDEXES:
        return _INDEXES[lang]
    with _INDEX_LOCK:
        if lang in _INDEXES:
            return _INDEXES[lang]
        man = index_manifest()
        if not man:
            raise SemanticUnavailable(
                f"no semantic index in {EMB_DIR}; run scripts/build_embeddings.py")
        entry = man.get("languages", {}).get(lang)
        if entry is None:
            raise SemanticUnavailable(f"semantic index has no entry for {lang!r}")
        with open(os.path.join(EMB_DIR, man["refs"]), encoding="utf-8") as fh:
            refs = [tuple(r) for r in json.load(fh)]
        parts = []
        for part in entry["parts"]:
            mat = _load_array(part["vectors"])
            if mat.shape != (len(refs), man["dimension"]):
                raise SemanticUnavailable(f"index part {part['vectors']} has shape "
                                          f"{mat.shape}, expected {(len(refs), man['dimension'])}")
            parts.append((part["source"], float(part["weight"]), mat))
        idx = VerseIndex(lang=lang, refs=refs, parts=parts)
        _INDEXES[lang] = idx
        return idx


# ---------------------------------------------------------------- the tier

def candidates_batch(texts: Sequence[str], lang: str,
                     top_k: int = 20) -> list[list[tuple[int, int, float]]]:
    """
    Verse candidates for many spans at once — the efficient entry point.

    One forward pass over the batch, one matrix product against the index.
    Raises SemanticUnavailable if anything needed is missing.
    """
    if not available(lang):
        raise SemanticUnavailable(
            f"semantic tier unavailable for {lang!r}: " + json.dumps(status()))
    idx = load_index(lang)
    q = encoder().encode(list(texts), max_length=QUERY_MAX_TOKENS)
    return idx.search(q, top_k=top_k)


def candidates(text: str, lang: str, top_k: int = 20) -> list[tuple[int, int, float]]:
    """Single-span form of `candidates_batch` (the detect.semantic_candidates contract)."""
    return candidates_batch([text], lang, top_k=top_k)[0]


def warmup(langs: Iterable[str] = ()) -> dict:
    """Load model and indexes now rather than on the first request. Returns status()."""
    if available():
        encoder().encode(["warm up"])
        for lg in langs:
            if available(lg):
                load_index(lg)
    return status()
