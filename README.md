# مِيزان — MIZAN

**بوابة إسناد الاقتباس — تسبق النشر**
A pre-publication gate for Qur'anic quotations in translated da'wah content.

> لا يُفتي · لا يترجم · يُسنِد فقط — it does not issue fatwa, does not translate; it attributes, only.

| | |
|---|---|
| **Live demo** | https://mizan-ai.duckdns.org |
| **Video (≤ 2 min)** | https://www.youtube.com/watch?v=eSpAGijaUDc |
| **Challenge** | تحدي الذكاء الاصطناعي في خدمة المحتوى الإسلامي — **المسار الرابع: أدوات المعرفة والتحقق لتمكين المعرفين بالإسلام** |
| **For judges** | criteria → evidence: [COMPLIANCE](docs/COMPLIANCE.md) · sources & licences register: [REGISTRY](docs/REGISTRY.md) · we tried to break it: [REDTEAM](docs/REDTEAM.md) · stated limits: [LIMITS](docs/LIMITS.md) · starting version and what was added on 4–6 October: [BASELINE](docs/BASELINE.md) |

## What it does

Paste a translated da'wah text (English, Urdu, Bengali, Hindi or Tagalog; the
Arabic original is optional). MIZAN finds every Qur'anic quotation in it and
answers one question for each: **which approved published translation did
these words come from?**

| Result | When |
|---|---|
| ✓ **مطابق لترجمة معتمدة** — names the translation and links to it | the words *are* that approved translation's words |
| **يُحال للمراجعة** — shows the closest approved text, differences highlighted | any word differs — even one — or nothing approved matches, or the printed verse number is wrong |

One referred quotation refers the whole document, and a human reviewer
decides: approve as published, replace with the approved text, or escalate to
a scholar — every decision is logged. MIZAN never says a text is "wrong",
never generates or corrects scripture, and has no question-answer interface.
Hadith is out of scope.

## Run it

```bash
git clone https://github.com/OsamaSyr/mizan && cd mizan
make setup      # ~40 s: downloads and verifies the official sources, builds the index
make run        # http://127.0.0.1:8000
make test       # 247 tests, standard library only
```

Python 3.11+, **standard library only**. `make setup` downloads the
Quranpedia.net dump and the Tanzil text, checks every file against pinned
SHA-256 values and fails unless the index is byte-for-byte the one measured
below. Docker: `docker build -t mizan . && docker run --rm -p 8000:8000 mizan`.

**AI tier (optional):** `pip install -r requirements-ml.txt`, then
`python scripts/build_embeddings.py --download` fetches the pinned BGE-M3
model; the precomputed vectors are already in `data/embeddings/`. Without it,
MIZAN runs its deterministic path and says so. Server deployment (HTTPS,
auto-restart, watchdog): [docs/DEPLOY.md](docs/DEPLOY.md).

## How it works

```
Arabic (optional) ─► find verses in the Arabic (Tanzil)              ─┐
Translation ───────► locate quotations: printed verse number · rare words ·
                     AI model (BGE-M3, suggests verse ids by meaning) ◄┘
                  ─► compare the words with EVERY approved translation of that verse
                  ─► MATCH only if word-identical · otherwise refer · document verdict
```

**Where the AI sits.** One local model — BGE-M3, a multilingual embedding
model — compares the *meaning* of each sentence with the 6,236 verses and
**suggests** which verse a re-worded quotation may be. It never emits text and
never decides: the verdict is always a deterministic word comparison with the
approved published text, and every finding it contributed is labelled as
AI-assisted. Measured: **+16.1 points** in locating quotations when the verse
number is removed, **+0** false attributions. Details:
[ARCHITECTURE](docs/ARCHITECTURE.md) · [SEMANTIC](docs/SEMANTIC.md).

**Similarity finds; identity decides.** A similarity score locates the verse,
but one changed word in a long verse ("does not forgive" → "does forgive")
still scores ≈ 0.98 — so MATCH also requires the words to be identical
(ignoring only case, punctuation, footnote markers, diacritics and
letter-form variants). An independent red-team review found that gap; it is
fixed and locked in by tests ([REDTEAM](docs/REDTEAM.md)).

A second panel checks shar'i terms against the approved English equivalents of
the الجمهرة dictionary ([TERMS](docs/TERMS.md)).

## Results

Every number is re-derived by `make eval` (three seeded runs; the published-
prose test set is rebuilt from the publishers' pages by `make real-corpus`).
Full tables: [`results/SUMMARY.md`](results/SUMMARY.md).

<!-- mizan:results:start -->

_Generated from `results/SUMMARY.md` by `.venv/bin/python -m mizan.eval --suite all --runs 3 --update-readme --fresh` on 2026-10-04 — index `2026-10-01`, 3 runs, Python 3.11.0._

| Metric | mean ± sd | n | suite |
|---|---|---|---|
| A control — false alarms (approved text flagged) | 0.000 ± 0 % | 8,321 | `index` |
| B identity — attributed to the exact edition | 90.68 ± 0.05 % | 8,301 | `index` |
| Edition confusion — of those, exact score ties | 100.0 ± 0 % |  | `index` |
| D rejection — wrong verse returned UNATTRIBUTED | 100.0 ± 0 % | 301 | `index` |
| As published — quotation located | 50.1 ± 0 % | 473 | `real` |
| As published — same verse the publisher cited | 97.1 ± 0 % | 204 | `real` |
| As published — attributed to an approved translation | 3.0 ± 0 % | 473 | `real` |
| Citation stripped — quotation located | 22.2 ± 0 % | 473 | `real` |
| FP probe, every no-quotation ¶ — paragraphs with any finding | 2.1 ± 0 % | 560 | `fp` |
| FP probe, every no-quotation ¶ — FALSE ATTRIBUTION (MATCH on prose) | 0.000 ± 0 % | 560 | `fp` |
| `curious` — refusal rate | 81.7 ± 0 % | 333 | `clarity` |
| Semantic tier adds — quotations located, citation stripped | +16.1 pp |  | `semantic` |
| Semantic tier adds — FALSE ATTRIBUTION | +0.0 pp |  | `semantic` |

<!-- mizan:results:end -->

In plain words: MIZAN has **never called ordinary prose «مطابق»** (0 of 560
paragraphs); all **26 red-team attacks** were referred; **499 of 500** random
approved verses in all five languages came out «مطابق». "Attributed 3.0 %"
is low by design: most quotations in the real articles differ from every
approved translation in their words (e.g. "God" printed for "Allah", or a
translation outside the index), so they are referred with the differences
shown. The clarity panel is built and tested but hidden in the interface.

## Limits

- Very short verses (5–6 words) printed **without** a verse number may go unnoticed.
- Verses with word-identical wording (e.g. 16:42 = 29:59) can't be told apart without a printed number.
- Coverage follows the approved index: English 15 translations; Urdu has one (Junagarhi), Hindi one, Bengali and Tagalog two each — anything else is referred, never approved.
- Terminology is English-only. Full list with measurements: [LIMITS](docs/LIMITS.md).

## Data and required attribution

> Qur'an translation data is sourced from **[Quranpedia.net](https://quranpedia.net)**
> (official dump 2026-10-01; 22 translations, 137,180 renderings). The
> translations remain the property of their translators and publishers. The
> Arabic Qur'an text is from the **[Tanzil Project](https://tanzil.net)**
> (Tanzil Quran Text v1.1, CC BY 3.0, used verbatim).

Terms: «موسوعة الجمهرة» (مركز أصول), [islamic-content.com/dictionary](https://islamic-content.com/dictionary) —
its terms allow personal, non-commercial use, so this repository carries only
term ↔ approved-English mappings with a source link each, never the
definitions. Model: [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) (MIT).
The index (124 MB) and the third-party test articles are not in the
repository; setup rebuilds the index, and `make real-corpus` rebuilds the test
set from committed URLs and checksums. Every source and licence:
[SOURCES](docs/SOURCES.md) · [REGISTRY](docs/REGISTRY.md).

**Privacy:** the full text is never stored — only the quotations and a hash
of the text, deleted after 30 days. No accounts, no analytics.

**How it was built:** with AI-assisted development (Claude Code) under the
author's direction, disclosed in [REGISTRY §4](docs/REGISTRY.md). The running
system calls no hosted AI service; its only model is the local BGE-M3.

## Documentation

| Read | For |
|---|---|
| [COMPLIANCE](docs/COMPLIANCE.md) · [REGISTRY](docs/REGISTRY.md) · [SOURCES](docs/SOURCES.md) · [BASELINE](docs/BASELINE.md) | every challenge requirement → evidence; sources, tools and licences; starting version and the work done during the challenge |
| [REDTEAM](docs/REDTEAM.md) · [LIMITS](docs/LIMITS.md) | what an independent review broke and how it was fixed; stated limits |
| [ARCHITECTURE](docs/ARCHITECTURE.md) · [SEMANTIC](docs/SEMANTIC.md) · [NORMALIZATION](docs/NORMALIZATION.md) | how detection, the AI tier and the comparison work |
| [TERMS](docs/TERMS.md) · [CLARITY](docs/CLARITY.md) · [REAL_CORPUS](docs/REAL_CORPUS.md) · [DEPLOY](docs/DEPLOY.md) | the panels, the real-world test set, the server kit |

`make help` lists every target. Configuration (`MIZAN_*` environment
variables) is documented at the top of `app.py`.

## Licence

Code and documentation: [MIT](LICENSE). The data MIZAN builds from keeps its
own terms ([SOURCES](docs/SOURCES.md)).
