# Architecture

MIZAN answers one question per Qur'anic quotation in a translated da'wah text:
**which approved published translation did this rendering come from?** It
never says a text is "wrong", never generates or corrects scripture, and has no
question-answer interface.

## 1. The pipeline

```
 Arabic source (optional)        Translation (required)          audience level (optional)
          │                               │                                │
          ▼                               ▼                                │
 ┌─────────────────────┐   ┌──────────────────────────────────┐            │
 │ (1) Arabic detection│   │ (2) Locate quotations in the     │            │
 │  arabic.py          │──►│     translation  — detect.py     │            │
 │  n-gram lookup over │refs│  a  printed reference "(2:255)"  │            │
 │  Tanzil, both       │   │  b  rare-token lexical index     │            │
 │  spellings          │   │  c  semantic candidates (bge-m3) │◄─ AI: ids only
 │  DETERMINISTIC      │   │     — optional, proposes verse ids│            │
 └─────────────────────┘   └───────────────┬──────────────────┘            │
                                           ▼                               │
                           ┌──────────────────────────────────┐            │
                           │ (3) Boundary refinement           │            │
                           │  hill-climb each span's edges     │            │
                           │  against the best approved text   │            │
                           │  DETERMINISTIC                    │            │
                           └───────────────┬──────────────────┘            │
                                           ▼                               │
                           ┌──────────────────────────────────┐            │
                           │ (4)+(5) Attribution — engine.py   │            │
                           │  every approved rendering of that │            │
                           │  verse in that language, from     │            │
                           │  data/corpus.sqlite               │            │
                           │  sim = 0.6·SequenceMatcher        │            │
                           │      + 0.4·token Jaccard          │            │
                           │  ≥0.75 NEAR · else UNATTRIBUTED   │            │
                           │  MATCH = ≥0.92 AND word-identical │            │
                           │  (engine.verbatim) + partial rule │            │
                           │  DETERMINISTIC — no model         │            │
                           └───────────────┬──────────────────┘            │
                                           ▼                               │
                           ┌──────────────────────────────────┐            │
                           │ (6) Document verdict — report.py  │            │
                           │  context checks (quote marks,     │            │
                           │  printed refs); one referred      │            │
                           │  item → whole document REFER      │            │
                           └───────────────┬──────────────────┘            │
                                           ▼                               ▼
                 ┌────────────────────┐  ┌─────────────────────┐  ┌──────────────────┐
                 │ Terminology panel  │  │ Clarity panel        │  │ Human review     │
                 │ terms.py — Jamhara │  │ clarity.py — allow-  │  │ review.py — queue│
                 │ equivalents; flags,│  │ list gate, frozen    │  │ + append-only    │
                 │ never rewrites     │  │ spans byte-exact,    │  │ audit log        │
                 │                    │  │ suggestions only     │  │                  │
                 └────────────────────┘  └─────────────────────┘  └──────────────────┘
```

Served by `app.py` (Python standard-library `http.server`, one server path)
with a vanilla-JS RTL interface in `web/`.

### The five states

| State | Meaning |
|---|---|
| `MATCH` | ≥ 0.92 against a named approved translation **and** the published words are that translation's words (`engine.verbatim`) |
| `NEAR` | ≥ 0.75, but the words differ — even by one word → **referred for review**, with a word diff against the closest approved translation |
| `UNATTRIBUTED` | < 0.75 against every indexed approved rendering → **referred for review**, never "wrong" |
| `NO_APPROVED_TRANSLATION` | the verse is known, but no approved translation is indexed in this language |
| `UNRESOLVED` | the quotation could not be resolved to a verse |

The thresholds are frozen in `engine.py`; the comparison form is frozen in
`docs/NORMALIZATION.md`. Changing either is a versioned event that requires
re-measuring every number.

**Similarity ranks; identity decides** (since 2026-10-04, after the red-team
review in [REDTEAM.md](REDTEAM.md)). `sim()` finds the verse and the closest
approved translation, but one changed word in a long verse ("does not
forgive" → "does forgive") still scores ≈ 0.98. So MATCH also requires the
published words to *be* an approved rendering's words, compared on
`engine.strict_tokens`: case, punctuation other than the question mark,
footnote markers, invisible characters, Arabic diacritics and letter-form
variants, and Latin accents are ignored; parenthesised words count (only the
approved side may drop its translator's glosses); marked omissions ("…") are
allowed segment by segment. Anything else is NEAR, and NEAR refers.

**Context checks** (`report._context_checks`). What the author printed around
a quotation can also refer it, whatever its state: words inside the same
quotation marks that are not part of a located verse; a printed reference
naming a different verse from the one whose text is there; quoted text
labelled with a reference but resembling nothing; a reference to a verse that
does not exist.

**Partial quotations** (`report.py`). The index stores whole verses; writers
quote clauses. When a located span's whole-verse result is `NEAR` or
`UNATTRIBUTED`, it has at least 8 words, and it is shorter than 85 % of an
approved rendering, the report layer also compares it with the best-matching
*clause* cut verbatim from that rendering, using the same `sim()`.
The span is upgraded only to `MATCH` — never to `NEAR` — and only when that
clause scores ≥ 0.92, ≥ 95 % of the span's words occur in it, **and** the span
is a verbatim run of that rendering (`engine.verbatim`); the
finding is then marked `partial` and carries the `approved_excerpt` it matched.
Calibrated on clauses cut from approved editions (88.1 % recognised) against
clauses from six published translations outside the index (6.1 % upgraded; a
looser rule that allowed NEAR upgraded 52 %, and was rejected). At clause
level a small edit of an approved translator cannot be told from a different
translator who phrases it the same way, so verbatim identity is the only
evidence accepted.

## 2. Where the AI sits — and what it may not do

There is exactly one learned component: the **semantic candidate tier (c)** in
step (2) — `src/mizan/semantic.py`, BAAI/bge-m3 run locally, documented in
`docs/SEMANTIC.md`.

It exists for one measured failure. The lexical tier finds a quotation by the
rare words it shares with an approved rendering; a passage taken from a
translation MIZAN does not index (or re-worded by the publisher) shares few of
them and is missed or pinned to the wrong verse. An embedding model can say
"this sentence is about 2:255" when the words cannot.

| The model MAY | The model may NOT |
|---|---|
| propose `(surah, ayah, score)` candidates for a span | emit any text — no verse, no correction, no explanation |
| | decide a state — MATCH/NEAR/UNATTRIBUTED is always `engine.sim` plus `engine.verbatim` against the approved published string |
| | see or alter the index, the thresholds, or the Arabic text |
| | run at request time without its pinned revision on disk (nothing is downloaded while serving) |

So a model error can, at worst, propose the wrong verse id — which the
deterministic comparison then scores against that verse's approved texts and,
if the words do not match, reports as `UNATTRIBUTED` (refer). It cannot make an
unapproved rendering look approved, because approval is a string comparison
the model does not take part in.

**What it adds, measured** (`docs/SEMANTIC.md`, `tests/results_semantic.json`):
on 983 quotations taken from 11 published translations that are *not* in the
index, with no printed reference, the share located at the correct verse rose
from 30.7 % to 54.5 % (none lost); the semantic tier's finds produced 0 MATCH
verdicts, and 99 % of them were referred to a person. On the published-prose
corpus (`mizan.eval --suite semantic`, the same items with and without the
model): quotations located with the citation stripped 22.2 % → 38.3 %, as
published 50.1 % → 57.9 %; attribution unchanged (it is the string
comparison); false attribution (MATCH on prose) on 560 paragraphs 0 with and
without the model;
paragraphs with any finding 2.1 % → 2.5 %.

**Fallback.** When `requirements-ml.txt` is not installed, the model is not on
disk, or `data/embeddings/` is not built, `semantic.available()` is false,
tier (c) is skipped and detection runs the deterministic path unchanged.
`MIZAN_SEMANTIC=0` forces that path. `/api/health` reports which path is live.

The terminology panel uses no model: it matches the Arabic term and looks for
the dictionary's approved English equivalent (or an accepted transliteration)
in the translation, reporting `APPROVED`, `VARIANT`, `MISSING` or
`NOT_IN_GLOSSARY` with the dictionary entry it relied on.

The clarity panel has a documented seam for a model adapter (`model_adapt`,
off by default). Even when enabled, every quotation and approved term is
swapped for an opaque token before the model sees the sentence, restored
afterwards, and verified byte-for-byte; any mismatch discards the whole
adapted sentence (`docs/CLARITY.md`).

## 3. Data flow and storage

| Store | Contents | Written by | Read by |
|---|---|---|---|
| `data/corpus.sqlite` | `translations`, `ayat` (+ FTS5), `arabic_ayat`, `arabic_source` | `scripts/build_index.py`, `scripts/fetch_arabic.py` (at setup only) | engine, detect, arabic, app — **read-only at runtime** |
| `data/embeddings/` | per-language verse vectors (optional) | `scripts/build_embeddings.py` | semantic.py |
| `data/review.sqlite` | saved checks in a minimal form (quoted spans, approved texts, SHA-256 of the translation — never the full document or the Arabic), review items, append-only decision log; unreviewed checks purged after 30 days | app.py via review.py | app.py |

Nothing leaves the machine: no request path makes a network call, there are
no accounts and no analytics. Network access happens only in `make setup`,
`make real-corpus` and the optional model download.

## 4. Reproducibility chain

```
quranpedia.net dump ──┐  size = publisher manifest
                      ├─ SHA-256 = pins in scripts/setup.sh
tanzil.net text ──────┘
        │
        ▼  scripts/build_index.py + scripts/fetch_arabic.py
data/corpus.sqlite ── index fingerprint = REFERENCE_INDEX_FINGERPRINT (mizan.eval)
        │
        ▼  python3 -m mizan.eval --suite all --runs 3   (seeds 20261001 + k)
results/summary.json · results/SUMMARY.md · README table
```

`make setup && make real-corpus && make eval` re-derives every published
number from a clean clone. The suites (`index`, `real`, `fp`, `clarity`,
`semantic`) call the measurement harnesses in `tests/` by import rather than
copying their logic.

## 5. Module map

| Path | Role |
|---|---|
| `src/mizan/engine.py` | normalization, `sim`, thresholds, `Mizan.attribute` — the verdict |
| `src/mizan/arabic.py` | Arabic quotation detection over Tanzil |
| `src/mizan/detect.py` | locating quotations in a translation (tiers a, b, c) and boundary refinement |
| `src/mizan/semantic.py` | optional bge-m3 candidate tier |
| `src/mizan/report.py` | findings, word diffs, document verdict, `check_document` |
| `src/mizan/terms.py` | terminology panel: approved English equivalents from الجمهرة (`data/glossary/`), flags and suggestions only |
| `src/mizan/clarity.py` | clarity / audience panel |
| `src/mizan/review.py` | review queue and audit log |
| `src/mizan/eval.py` | one-command evaluation |
| `app.py`, `web/` | local server and interface |
| `scripts/setup.sh` | one-command setup: fetch, verify, build, fingerprint |
| `scripts/build_index.py`, `scripts/fetch_arabic.py` | index construction |
| `scripts/collect_corpus.py`, `scripts/rebuild_real_corpus.py` | real-world test corpus: original crawl, and rebuild from locators |
| `tests/run_eval.py`, `tests/real_corpus.py` | measurement harnesses |
| `tests/test_*.py` | unit tests (`make test`) |
