# مِيزان — MIZAN

**بوابة إسناد الاقتباس — تسبق النشر**
A pre-publication gate for Qur'anic quotations in translated da'wah content.

> لا يُفتي · لا يترجم · يُسنِد فقط
> It does not issue fatwa. It does not translate. It attributes, only.

| | |
|---|---|
| **Live demo** | https://mizan-ai.duckdns.org |
| **Video (≤ 2 min)** | https://www.youtube.com/watch?v=eSpAGijaUDc |
| **Challenge** | تحدي الذكاء الاصطناعي في خدمة المحتوى الإسلامي — **المسار الرابع: أدوات المعرفة والتحقق لتمكين المعرفين بالإسلام** (moved from Track 2 with the organisers' permission) |
| **Track criterion → evidence** | [docs/COMPLIANCE.md §و](docs/COMPLIANCE.md) |

MIZAN takes a translated da'wah text (and, optionally, its Arabic original),
finds the Qur'anic quotations inside the translation, and answers **one**
question for each:

> **Which approved published translation did this rendering come from?**

It never says a text is "wrong". It names the approved translation the
quotation matches, shows a word-level diff when it was modified, or says it
matches nothing in the index and must be **referred to a person**. It never
generates or corrects scripture, and there is no question-answer interface.

> **Disclosures.** Sources and licences: [docs/SOURCES.md](docs/SOURCES.md) ·
> register of every tool, model, service and data source (challenge terms,
> Article 9): **[docs/REGISTRY.md](docs/REGISTRY.md)** · built with
> AI-assisted development (Claude Code) — see [How MIZAN was built](#how-mizan-was-built).

### Why

Da'wah material is translated, re-typed and re-edited many times before it is
printed. A verse that drifts from every approved translation along the way is
hard to spot: «قرّاء هذه الكتب عموماً لا يتكلمون العربية. فلا يميِّز أحدهم الخطأ
من الصواب» — the readers of these books generally do not speak Arabic and
cannot tell the sound from the unsound (Maḥmūd b. Riḍā Murād, «الأسس العلمية
التي تقوم عليها الترجمة», a paper presented to the symposium on translating
the Sunnah and the Sīra, Imam Muhammad ibn Saud Islamic University, Riyadh,
1429 AH). And no reviewer remembers fifteen English translations well enough
to tell which one a sentence came from: two *approved* translations of the
same verse differ at a mean similarity of about 0.46. MIZAN does that
comparison exhaustively, deterministically, and before publication.

---

## Run it in under a minute

```bash
git clone <this repo> mizan && cd mizan
make setup      # ~40 s: downloads + verifies the official sources, builds the index
make run        # http://127.0.0.1:8000
```

Python 3.11+, **standard library only** — nothing to `pip install`.
`make setup` (= `bash scripts/setup.sh`) downloads Quranpedia.net's official
dump and the Tanzil text, checks every file's size against the publisher's
manifest and its SHA-256 against pinned values, builds `data/corpus.sqlite`,
and fails loudly unless the result is byte-for-byte the index every number
below was measured on. Run it again and it does nothing (≈3 s). Measured on a
clean copy of this repository: 37 s end to end.

Or in a container:

```bash
docker build -t mizan .          # ~40 s: runs the same setup (and fingerprint check) inside the build
docker run --rm -p 8000:8000 mizan
```

The image is `python:3.11-slim` + the index (≈385 MB), runs as a non-root user,
binds `0.0.0.0:$PORT` and has a health check on `/api/health`.
`docker build --build-arg WITH_ML=1 -t mizan:ml .` adds the semantic tier
(CPU torch, pinned model, embeddings built in the image — large and slow).

The page opens empty; the three sample chips fill both boxes with a reviewed
example, and «افحص الإسناد» runs the check. Port: `MIZAN_PORT`
(default 8000); in the container, `PORT`.

### The five states

| State | Meaning | Shown as |
|---|---|---|
| `MATCH` | the published words **are** an indexed approved translation's words (whole verse, or a marked excerpt of it) | «مطابق لترجمة معتمدة» — named, with its edition and a link to it on quranpedia.net |
| `NEAR` | ≥ 0.75 similarity, but the words differ — even by one word | «قريب من ترجمة معتمدة، والكلمات مختلفة» → **referred for review**, with a word-level diff against the closest approved translation |
| `UNATTRIBUTED` | < 0.75 against every indexed approved rendering | «لا يُسنَد» → **referred for review**, *not* "wrong" |
| `NO_APPROVED_TRANSLATION` | the verse is known, but no approved translation is indexed in this language | «استخدم العربي مع حاشية مترجم» |
| `UNRESOLVED` | the quotation could not be resolved to a verse | «لم نتعرّف على الاقتباس» |

**Why `UNATTRIBUTED` never means "wrong".** A narrow index accuses the
innocent. During development, passages we assumed were re-translated turned
out to be faithful quotations of Pickthall — an approved translation that was
simply not indexed yet. A quotation that matches nothing here may come from an
approved translation we do not hold. That is a reason to show it to a
specialist, not to call it an error.

**The document verdict.** One referred item pulls the whole document to
«يُحال». A gate whose result can be diluted is not a gate. Besides the state,
four things the author printed *around* a quotation refer it: words inside the
same quotation marks that are not part of the verse, a printed reference that
names a different verse, quoted text labelled with a reference but resembling
nothing, and a reference to a verse that does not exist.

**Similarity ranks; identity decides.** Similarity —
`0.6 × SequenceMatcher + 0.4 × token Jaccard` over a frozen normalized form
([docs/NORMALIZATION.md](docs/NORMALIZATION.md)), thresholds `0.92` / `0.75`
frozen in `src/mizan/engine.py` — finds the verse and the closest approved
translation. It does not decide MATCH: one changed word in a long verse
("does not forgive" → "does forgive") still scores ≈ 0.98. MATCH additionally
requires word identity (`engine.verbatim`): only case, punctuation other than
the question mark, footnote markers, invisible characters, Arabic diacritics,
Latin accents and letter-form variants are ignored; parenthesised words count.
This rule, and the context checks above, came from an independent red-team
review on 2026-10-03 — see [docs/REDTEAM.md](docs/REDTEAM.md) and
`tests/test_redteam.py`.

---

## Results

Every number here is re-derived by one command, with fixed seeds, from the
index a clean clone builds:

```bash
make setup          # the index (verified against its reference fingerprint)
make real-corpus    # the published-prose test set, rebuilt from the publishers' pages (~10 min)
make eval           # = PYTHONPATH=src python3 -m mizan.eval --suite all --runs 3
```

→ `results/summary.json` (every run, every metric) and
[`results/SUMMARY.md`](results/SUMMARY.md) (all tables, seeds, fingerprints,
Python version). The table below is generated from it by `make eval`; values
are mean ± sd over three seeded runs.

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

**Reproduced on 2026-10-04.** The run above (`.venv/bin/python`, ML extra
installed) and a plain-`python3` run in this checkout
([`results/stdlib/`](results/stdlib/SUMMARY.md)) agree on every value of all
146 shared non-timing metrics, run by run; the 34 semantic-suite metrics exist
only where the model is installed. A clean copy built from scratch by
`make setup` reproduces the index fingerprint and passes `make test` (checked
2026-10-04); the same full-evaluation comparison against a clean copy was
last run on 2026-10-03. Wall time: ≈ 9 min for `make eval` with plain `python3`, ≈ 12 min with
the semantic suite.

What the suites measure:

* **index** — calibration on the approved index itself (`tests/run_eval.py`):
  approved text fed back must never be flagged; it should be attributed to
  its exact edition; a different verse's text must come back `UNATTRIBUTED`.
* **real** — 473 quotations from published da'wah prose in en / ur / bn / hi /
  tl, ground truth = the publisher's own printed reference
  (`tests/real_corpus.py`, [docs/REAL_CORPUS.md](docs/REAL_CORPUS.md)).
* **fp** — the false-positive probe: 560 paragraphs of the same authors' prose
  with no quotation in them. The headline number is **false attribution**
  (MIZAN naming an approved translation for prose) — the one error the system
  must never make.
* **clarity** — the clarity panel's refusal rate and readability on real
  publisher prose ([docs/CLARITY.md](docs/CLARITY.md)).
* **terms** — the terminology panel on the same prose, plus its calibration
  against approved translations ([docs/TERMS.md](docs/TERMS.md)).
* **semantic** — lexical vs lexical + semantic tier on the same items, when
  the ML extra is installed ([docs/SEMANTIC.md](docs/SEMANTIC.md)). Run
  `make eval PYTHON=.venv/bin/python` from an environment with
  `requirements-ml.txt`; with plain `python3` the suite is skipped and says why.

`real` and `fp` always measure the deterministic path (`MIZAN_SEMANTIC=0`), so
a standard-library install reproduces them exactly; what the model adds is
reported only as the `semantic` delta.

---

## Architecture at a glance

```
Arabic (optional) ─► (1) Arabic detection — Tanzil n-grams ──────────┐ verse ids
Translation ───────► (2) locate quotations: printed ref · lexical ·  ◄┘
                         semantic (bge-m3, optional — proposes ids only)
                     (3) boundary refinement
                     (4-5) attribution: engine.sim vs EVERY approved
                           rendering of that verse — deterministic
                     (6) document verdict: one referral → REFER
                     + terminology panel (Jamhara) · clarity panel ·
                       human review queue with an append-only audit log
```

**Where the AI sits.** One optional component — the bge-m3 semantic tier —
may *propose verse ids* for a passage whose wording matches no approved
rendering. It never emits text and has no say in the verdict: every state is
decided by deterministic string comparison against the approved published
text. Without the ML extra MIZAN runs the deterministic path unchanged and
says so in `/api/health`. What it adds, measured on 983 quotations from 11
published translations *outside* the index: located at the correct verse
30.7 % → 54.5 %, none lost; its finds produced 0 MATCH verdicts and 99 % of
them were sent to a reviewer ([docs/SEMANTIC.md](docs/SEMANTIC.md),
`tests/results_semantic.json`). Full detail:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Beyond scripture — two more panels, neither of which rewrites anything.**
*Terminology* (`terms.py`): for each shar'i term in the Arabic source, does the
translation use the approved English equivalent from الجمهرة — the dictionary
the official scientific package names for sensitive terms? 335 dictionary
records for 283 terms, each suggestion linked to its entry
([docs/TERMS.md](docs/TERMS.md)). *Clarity* (`clarity.py`): an allow-list gate
that refuses to touch scripture, rulings, creed or attributions, freezes
quotations and approved terms byte-for-byte, and offers the editor
suggestions for the rest ([docs/CLARITY.md](docs/CLARITY.md)). It is built and
tested but **hidden in the interface by default** (`SHOW_CLARITY` in
`web/app.js`): it is English-only and, after the red-team fixes, leaves 81.7 %
of real publisher prose untouched; the API still returns it.

---

## Honest limits

Measured, not guessed — numbers in [docs/LIMITS.md](docs/LIMITS.md):

* **Exact edition vs approved translation.** Quranpedia publishes several
  translations as two book entries each (Sahih International, Khattab, Muhsin
  Khan, Junagarhi — one entry often carrying translator footnotes). Where the
  two print identical words for a verse no text comparison can separate them,
  and MIZAN names the lower book id; every wrong-edition answer in the
  calibration sample is such an exact tie. Naming the wrong *copy* of an
  approved translation accuses no one.
* **Real published prose is much harder than the index fed back to itself.**
  Partial quotations, ellipses and paraphrase lower detection; see the `real`
  rows above, by language, in `results/SUMMARY.md`.
* **Footnoted editions.** Some editions store translator commentary in the
  same field as the verse; a faithful quotation of only the verse text then
  scores low against them.
* **Thin languages.** Hindi has one indexed approved translation; Urdu,
  Bengali and Tagalog two each, English fifteen. Normalization fragments Hindi
  and Bengali words at vowel signs, so vowel-only changes are invisible there.
* **Terminology is English-only**, and the dictionary lacks 21 of the seed
  terms; 22 entries whose only sense is a different word are flagged and not
  used. Other languages get an honest `NOT_IN_GLOSSARY`.

`UNATTRIBUTED` always means "not in this index — a person should look", which
is why every limit above fails towards referral, not towards false approval.

---

## API

All endpoints are local (`app.py`, Python `http.server`). The server makes no
outbound network call. The page links one Google webfont; offline it falls
back to the system Arabic font.

| Method | Path | |
|---|---|---|
| `GET` | `/` · `/review` | the checker and the reviewer page |
| `GET` | `/api/health` | index version and counts, thresholds, pipeline, panels, limits |
| `GET` | `/api/languages` | indexed languages and their approved translations |
| `POST` | `/api/check` | `{translation, arabic?, lang, level?}` → findings, `document_verdict`, `refer_refs`, terms, clarity |
| `POST` | `/api/clarity` | `{check_id, translation, level}` |
| `GET` | `/api/check/<id>` · `/api/check/<id>/export.json` · `/report/<id>.html` | a saved check as JSON, as a download, as a self-contained Arabic report |
| `GET` | `/api/verse?ref=S:A&lang=xx` | approved renderings of one verse |
| `POST`/`GET` | `/api/review` · `/api/review/<item>` · `/api/review/<item>/decision` · `/api/review/log` | review queue and the append-only audit log |

Document verdicts: `ATTRIBUTED`, `NO_APPROVED_TRANSLATION`, `NO_QUOTES`,
`REFER`. (`ATTRIBUTED_WITH_EDITS` exists only in records saved before
2026-10-04, when NEAR did not refer.)

**What is stored.** Checks are kept in `data/review.sqlite` in a minimal form —
the quoted spans, the approved texts they were compared with, and a SHA-256 of
the translation; never the full document or the Arabic. Unreviewed checks are
purged after 30 days (`MIZAN_RETENTION_DAYS`). Reviewer decisions are
append-only, enforced by database triggers. No accounts, no IPs, no analytics.

**Configuration:** `MIZAN_PORT` (8000), `MIZAN_HOST` (127.0.0.1 — on loopback
the server also refuses non-local `Host` headers; the container sets
`0.0.0.0`), `MIZAN_REVIEW_DB`, `MIZAN_RETENTION_DAYS` (30),
`MIZAN_CHECK_TIMEOUT` (90 s), `MIZAN_MAX_CHARS` (20,000 per field; request
bodies are capped at 512 KB; checks run one at a time), `MIZAN_TERMS_MODULE`,
`MIZAN_CLARITY_MODULE`; behind a reverse proxy, `MIZAN_ALLOWED_HOSTS` (the
public host name(s) the server accepts — required). AI tier:
`MIZAN_SEMANTIC` (`0` turns it off), `MIZAN_DEVICE` (`cpu`/`mps`/`cuda`),
`MIZAN_SEM_TOKEN_BUDGET` (1,024 tokens per check). Setup:
`MIZAN_ACCEPT_NEW_DUMP=1` accepts a newer Quranpedia dump than the pinned one
(the index fingerprint then differs from the reference, and the evaluation
says so). The version label shown in the app is the dump's own date; what is
pinned is the files' SHA-256.

---

## Data and required attribution

| | |
|---|---|
| Approved translations | **[Quranpedia.net](https://quranpedia.net)** official dump `2026-10-01` (content verified identical through 2026-10-03) — 22 translations · en 15 · ur 2 · bn 2 · tl 2 · hi 1 · 137,180 renderings |
| Arabic source text | **[Tanzil Project](https://tanzil.net)** — Tanzil Quran Text v1.1 (Uthmani, Simple Clean), CC BY 3.0, used verbatim, for detection only |
| Terminology | «موسوعة الجمهرة» (مركز أصول) — [islamic-content.com/dictionary](https://islamic-content.com/dictionary): 335 records for 283 terms, English equivalents, collected under its robots.txt (≥ 5.5 s between requests); the repo carries term ↔ equivalent mappings with a source link each, no definitions ([docs/TERMS.md](docs/TERMS.md)) |
| Optional model | [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) (MIT), pinned revision; [bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) (Apache-2.0) was measured and not adopted |
| Test-only inputs | islamreligion.com, islamqa.info — for measurement, **not redistributed** |

> Qur'an translation data in this project is sourced from
> **[Quranpedia.net](https://quranpedia.net)** (dump 2026-10-01). The
> translations remain the property of their respective translators and
> publishers. The Arabic Qur'an text is from the **[Tanzil Project](https://tanzil.net)**.

الجمهرة's terms grant personal, non-commercial use, so the repository carries
only term ↔ approved-English mappings with a source link per entry — never the
dictionary's definitions; a written permission request to مركز أصول has not
been sent yet.

The index (124 MB — over GitHub's file limit) is not in this repository; setup
builds it from the publishers' files. The real-world test corpus is not in it
either — only URLs, ordinals, references and SHA-256 checksums
(`data/real/corpus.manifest.json`) from which `make real-corpus` rebuilds it.
Every source, its licence and how MIZAN complies: [docs/SOURCES.md](docs/SOURCES.md).
The register of every tool, model, service, data source and component — type,
origin, purpose, date, legal basis — required by the challenge terms:
**[docs/REGISTRY.md](docs/REGISTRY.md)**.

### How MIZAN was built

MIZAN was built with **AI-assisted development (Claude Code)**: code,
measurements and documentation were produced with an AI coding assistant
working under the author's direction, and are disclosed as such in
[docs/REGISTRY.md](docs/REGISTRY.md). No part of the running system calls a
hosted AI service; the only model in it is the optional, local bge-m3 tier
described above.

---

## Repository

```
app.py                  local server (stdlib http.server)
web/                    RTL Arabic interface + reviewer page, no build step
src/mizan/
  engine.py             normalization, similarity, thresholds — the verdict
  arabic.py             Arabic quotation detection (Tanzil)
  detect.py             locating quotations in a translation
  semantic.py           optional bge-m3 candidate tier
  report.py             findings, diffs, document verdict
  terms.py              terminology panel (Jamhara)
  clarity.py            clarity / audience panel
  review.py             review queue + append-only audit log
  eval.py               one-command evaluation
scripts/
  setup.sh              one-command setup: fetch, verify, build, fingerprint
  build_index.py        index from the Quranpedia dump
  fetch_arabic.py       Tanzil text into the index
  collect_corpus.py     the original real-world crawl (robots.txt, delays)
  rebuild_real_corpus.py  rebuild that corpus from committed locators
  build_embeddings.py   optional semantic index
  fetch_jamhara.py      terminology glossary
tests/                  unit tests (make test), red-team regressions, measurement harnesses
deploy/                 server kit: provision, push, Caddy (HTTPS), systemd, watchdog
results/                summary.json + SUMMARY.md from `make eval`
docs/                   ARCHITECTURE · SOURCES · REGISTRY · COMPLIANCE · REDTEAM · NORMALIZATION · LIMITS · REAL_CORPUS · CLARITY · TERMS · SEMANTIC · DEPLOY
```

`make help` lists every target: `setup`, `run`, `test`, `eval`, `real-corpus`,
`verify`, `index`, `docker`, `docker-ml`, `clean`.

## Licence

Code and documentation: [MIT](LICENSE). The data MIZAN builds from keeps its
own terms ([docs/SOURCES.md](docs/SOURCES.md)); the MIT licence does not
extend to it.
