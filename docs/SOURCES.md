# Sources, licences and how each one is used

Every external input MIZAN touches, what its terms say (quoted or summarised
from the source itself, checked 2026-10-03), how MIZAN uses it, and whether any
of it is in the public repository.

| Source | Role | In the repo? | How a clean clone gets it |
|---|---|---|---|
| Quranpedia.net official dumps | the **approved translations** — what quotations are attributed *to* | no (index is built locally) | `make setup` downloads, verifies, builds |
| Tanzil Quran text | the **Arabic source** — what quotations are detected *from* | no | `make setup` |
| الجمهرة (islamic-content.com) dictionary | terminology panel | term ↔ approved-English mappings + source URLs only — **no definitions** (§3) | committed `jamhara.public.jsonl`; `scripts/fetch_jamhara.py` re-collects the rest locally |
| BAAI/bge-m3 (bge-reranker-v2-m3 measured, not used) | optional semantic tier | no | `requirements-ml.txt`, `scripts/build_embeddings.py --download` |
| islamreligion.com, islamqa.info | **test inputs only** — measurement, never redistributed | locators + checksums only | `make real-corpus` |
| AlQuran.cloud editions outside the index | **test inputs only** — semantic held-out test | no | not yet scripted (§5b) |

The MIT licence in `LICENSE` covers MIZAN's **code and documentation**. It
does not relicense any data below; each keeps its own terms.

The challenge terms (Article 9) also require a register of every tool, model,
service, data source and component used — type, origin, purpose, date and
legal basis/licence — including the AI coding assistant MIZAN was built with.
That register is **[docs/REGISTRY.md](REGISTRY.md)**; this page explains the
data sources in depth.

---

## 1. Quranpedia.net — approved translations

**What:** the official versioned dumps at <https://quranpedia.net/dumps/>:
`manifest.json` and `translations-all.zip` (138 translation books). MIZAN
indexes the 22 books in its five languages:

| book_id | lang | title (as published by Quranpedia) | verses |
|---|---|---|---|
| 1947 | en | الترجمة الإنجليزية - صحيح انترناشونال | 6,236 |
| 1948 | en | الترجمة الإنجليزية | 6,236 |
| 13602 | en | Dr. Ghali - English translation | 6,236 |
| 13603 | en | Muhsin Khan - English translation | 6,236 |
| 13604 | en | Pickthall - English translation | 6,236 |
| 13605 | en | Yusuf Ali - English translation | 6,236 |
| 13638 | en | Sahih International - English translation | 6,236 |
| 13640 | en | Abul Ala Maududi (With tafsir) - English translation | 6,236 |
| 13644 | en | Abdul Haleem - English translation | 6,236 |
| 13645 | en | Mufti Taqi Usmani - English translation | 6,236 |
| 13661 | en | Dr. Mustafa Khattab, The Clear Quran - English translation | 6,236 |
| 13662 | en | Dr. Mustafa Khattab - English translation | 6,236 |
| 27811 | en | الترجمة الإنجليزية - مركز رواد الترجمة | 6,236 |
| 27824 | en | الترجمة الإنجليزية للمختصر في تفسير القرآن الكريم | 6,236 |
| 27833 | en | ترجمة معاني القرآن الكريم - عادل صلاحي | 6,224 |
| 1966 | ur | الترجمة الأردية | 6,236 |
| 13625 | ur | محمد جوناگڑھی - Urdu translation | 6,236 |
| 1967 | bn | الترجمة البنغالية | 6,236 |
| 27826 | bn | الترجمة البنغالية للمختصر في تفسير القرآن الكريم | 6,236 |
| 1963 | tl | الترجمة الفلبينية (تجالوج) | 6,236 |
| 2010 | tl | الترجمة الفلبينية (تجالوج) للمختصر في تفسير القرآن الكريم | 6,236 |
| 1986 | hi | الترجمة الهندية | 6,236 |

137,180 renderings in total.

**Dump version.** The pinned content is from dump **2026-10-01**. Quranpedia
regenerates the dumps daily; the 22 files above were verified byte-identical
(SHA-256) in the dump served on 2026-10-03 (manifest label 2026-10-02, zip
`LICENSE.md` dated 2026-10-03), in which only `LICENSE.md`'s date line
differed from 2026-10-01. `/api/v1/changes?since=2026-10-01` reported no changes on
2026-10-03. The version label stored in a freshly built index is the dump
date it was downloaded from; the **content** is guaranteed identical by the
pins and by the index fingerprint (§6).

**Licence** — from `LICENSE.md` inside the zip (version 2026-10-01):

> Free to use inside apps, websites, bots, and research tools — no attribution
> required (a visible link to https://quranpedia.net is appreciated).
> Republishing this data — in full or in part — as a downloadable database or
> dataset requires: (1) crediting **Quranpedia.net** as the source with a
> link, and (2) stating this dump's version.
> The content is continuously corrected. Keep any copy current via
> `https://quranpedia.net/api/v1/changes?since=<version>` — distributing
> outdated Quranic text is the distributor's responsibility.
> Provided as-is, without warranty; verification remains the user's duty.
> Translations and contemporary works remain the intellectual property of
> their respective authors and publishers.

And from the API usage policy (<https://quranpedia.net/api-docs#usage-policy>):
*do not bulk-scrape the API; download the official versioned dumps instead;
high-volume clients should include a contact address in their User-Agent.*

**How MIZAN complies**

* Only the official dumps are used — never the API for bulk text. Setup sends
  `User-Agent: Mizan/0.1 (Islamic AI Challenge 2026; contact osamaabdullh2002@gmail.com)`
  and makes requests one at a time.
* The manifest's per-file URLs point at `http://localhost/...` (a publisher
  bug); `setup.sh --per-file` rewrites them to `https://quranpedia.net/...`.
* Every file is checked twice: byte size against the publisher's manifest
  (`translation_books[].bytes`) and SHA-256 against pins in `scripts/setup.sh`.
  The manifest itself carries **no** SHA-256 for translation books (its
  `files[]` checksums cover other datasets), which is why MIZAN pins its own.
* The index is **not** committed (it would also exceed GitHub's 100 MB file
  limit). The repository republishes no Quranpedia data.
* Attribution anyway: the web UI footer links to quranpedia.net;
  `/api/health`, `/api/languages` and check responses carry
  `source: quranpedia.net`, and each attributed finding links to the verse on
  quranpedia.net; this file and the README credit Quranpedia.net with a link.
* **If you publish the Docker image** (which contains the built index), you are
  publishing a database: keep README.md and this file in it (they are), and
  state the dump version shown by `/api/health`.
* Freshness: setup calls the changes endpoint once and warns if anything
  relevant changed. MIZAN deliberately *pins* content so its measured numbers
  stay true; refreshing is a deliberate act (`MIZAN_ACCEPT_NEW_DUMP=1`, then
  re-measure). This is the one place MIZAN's needs (reproducibility) and the
  publisher's request (stay current) pull in different directions, and we
  resolve it by making staleness visible rather than silent.

## 2. Tanzil — the Arabic Quran text

**What:** Tanzil Quran Text, *Uthmani* and *Simple Clean*, **version 1.1**,
from <https://tanzil.net/download/>, fetched by the same form request the site
uses (`quranType`, `outType=txt-2`). 6,236 verses × 2 spellings.

**Licence** — the copyright block in every file (reproduced in full in
`data/raw/tanzil/*.txt`):

> Tanzil Quran Text — Copyright (C) 2007-2026 Tanzil Project —
> License: Creative Commons Attribution 3.0.
> Permission is granted to copy and distribute verbatim copies of this text,
> but CHANGING IT IS NOT ALLOWED.
> This Quran text can be used in any website or application, provided that its
> source (Tanzil Project) is clearly indicated, and a link is made to
> tanzil.net to enable users to keep track of changes.
> This copyright notice shall be included in all verbatim copies of the text,
> and shall be reproduced appropriately in all files derived from or containing
> substantial portion of this text.

**How MIZAN complies**

* Stored **verbatim**, byte for byte, in `arabic_ayat.text_uthmani` and
  `text_simple` (`scripts/fetch_arabic.py` refuses to write unless both
  editions parse to exactly 6,236 verses / 114 surahs with identical keys).
  Setup additionally pins the SHA-256 of the verse lines.
* Never changed. `text_norm` is a separate **matching key** derived by
  `engine.normalize` (`docs/NORMALIZATION.md`); the text shown to a person is
  always the stored original. No model ever reads or writes it.
* Used only to *detect* that Arabic prose quotes a verse — never as an
  attribution target.
* Attribution: this file and the README name Tanzil Project with a link to
  <https://tanzil.net>; the index records `arabic_source(name, source, version,
  licence)`; the Docker image keeps the two Tanzil files, with their copyright
  block, next to the index built from them. **Open item:** the web UI footer
  credits Quranpedia but not yet Tanzil; Tanzil's terms ask any application
  using the text to name Tanzil Project and link to tanzil.net.
* Not committed to the repository.

## 3. الجمهرة — Jamhara dictionary (islamic-content.com)

**What:** «موسوعة الجمهرة - مفردات المحتوى الإسلامي»,
<https://islamic-content.com/dictionary>. The official scientific package names
it as *the* reference for translation and terminology: «يقدم على الترجمة
التلقائية في المصطلحات الشرعية الحساسة». MIZAN's terminology panel
(`src/mizan/terms.py`, documented in `docs/TERMS.md`) checks whether a
translation uses the dictionary's approved English equivalent of each shar'i
term in the Arabic source. It flags and suggests — naming the dictionary page —
and never rewrites.

**Collected** by `scripts/fetch_jamhara.py` on 2026-10-03: 304 seed terms → 283
resolved (21 are not in the dictionary's listings), **335 dictionary records**;
623 page requests, all HTTP 200, ≥ 5.5 s apart, contact-bearing User-Agent,
robots.txt read live on every run, nothing under `/api/` or `/ayah/`. English
equivalents only. 22 entries whose only sense is a different word (e.g. «الدين»
as *debt*) are flagged `wrong_sense` and not used.

**Terms** — the site's copyright page (<https://islamic-content.com/page/copyright>)
grants «الاستفادة العلمية من محتويات الموقع في الاستخدام الشخصي غير التجاري» —
scholarly benefit from the site's content, for personal, non-commercial use.
That is not a licence to redistribute, and the challenge terms forbid including
a third party's protected material without a legal basis («لا تُدرج مواد أو
بيانات... للغير دون حق»). **robots.txt** (checked 2026-10-03): `/dictionary/` is
open to general crawlers; `/api/`, `/admin/`, `/attachment/`, `/config/`,
`/legacy-dictionary/` are disallowed; AI crawlers are asked to stay off `/ayah/`
with a 5 s crawl delay.

**What is redistributed, and what is not.**

| File | In the public repo? | Contents |
|---|---|---|
| `data/glossary/jamhara.public.jsonl` | **yes** | per record: Arabic term ↔ approved English equivalent(s), category, dictionary entry id, **source URL**, retrieval date, review flag — short factual mappings, each attributed and linked. **No definitions.** |
| `data/glossary/package_terms.json` | yes | the official scientific package's ten sample terms with their «ضابط الاستخدام» (competition material) |
| `data/glossary/manifest.json`, `measurement.json` | yes | crawl audit (requests, robots decisions); the panel's own measurements |
| `data/glossary/jamhara.jsonl` | **no** (local) | the full records, including the dictionary's Arabic and English prose definitions |
| `data/glossary/parallel_definitions.jsonl` | **no** (local) | 246 Arabic/English definition pairs, used only by one calibration measurement |
| `data/glossary/raw/` | **no** (local) | the page cache |

`scripts/export_public_glossary.py` (run by `make setup` / `make
glossary-public` whenever the full local copy exists) writes the public file
from the local one. A clean clone runs the terminology panel from the public
file; the one calibration that needs the definitions
(`calib.dictionary_definitions` in the `terms` eval suite) is skipped there
unless the dictionary is re-collected locally with `scripts/fetch_jamhara.py`.

**Attribution:** every suggestion the panel makes names and links its الجمهرة
entry; this file and the README credit «موسوعة الجمهرة» (مركز أصول) with a link.
**Permission** to redistribute the term ↔ equivalent mappings has been
requested from مركز أصول by the repository owner; until it is granted, the
public repository carries only the mappings above.

## 4. BAAI/bge-m3 and BAAI/bge-reranker-v2-m3 — optional semantic tier

| Model | Licence (Hugging Face model card, verified via the HF API 2026-10-03) | Pinned revision |
|---|---|---|
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | **MIT** | `5617a9f61b028005a4858fdac845db406aefb181` |
| [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) | **Apache-2.0** | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |

bge-m3 is used only when `requirements-ml.txt` is installed and the
embeddings are built (`src/mizan/semantic.py`, `docs/SEMANTIC.md`). The
reranker was **measured and not adopted** (on the dev split it hurt full-verse
ranking and the rejection of ordinary prose, `docs/SEMANTIC.md` §4); its revision stays pinned so that experiment can be
re-run. The model **proposes verse
ids**; it never emits text and has no say in the verdict, which is always the
deterministic comparison against the approved string. Weights are downloaded
from Hugging Face at setup time (never at request time) and are not
committed. The precomputed vectors in `data/embeddings/` (77 MB, derived
from this index) are committed, because rebuilding them on a CPU takes hours.
Without the model MIZAN runs its lexical path and reports that it does.

## 5. Test-only inputs — islamreligion.com and islamqa.info

**What:** 473 quotations and 560 quotation-free paragraphs of published
da'wah prose in en / hi / tl (islamreligion.com) and ur / bn / hi
(islamqa.info), collected 2026-10-02 by `scripts/collect_corpus.py` to measure
MIZAN on text nobody involved in building it wrote. Details:
`docs/REAL_CORPUS.md`.

**Terms:** islamreligion.com pages state «Copyright © 2006 - 2026
IslamReligion.com. All rights reserved.»; islamqa.info pages state that all
rights are reserved to the Islam Q&A website (1997–2025). Both robots.txt
files permit the article paths used (islamqa.info disallows only `/_next/`);
the collector honours robots.txt per URL, a ≥ 2.5 s delay, one host at a time,
and a contact-bearing User-Agent.

**Decision — conservative:** the text is used for measurement and **not
redistributed**. The public repository contains

* `data/real/manifest.json` — the crawl audit (robots decisions, counts);
* `data/real/corpus.manifest.json` — for each of the 1,033 records: source
  URL, language, the extractor's ordinal on that page, printed reference,
  markup hint, length and SHA-256 of the measured text. **No text.**

and not `corpus.jsonl`, `negatives.jsonl`, the HTML cache, or
`tests/results_real.json` (which quotes up to 400 characters per item).

`make real-corpus` (`scripts/rebuild_real_corpus.py`) refetches exactly the
159 listed pages under the same crawl rules, runs the **same** extractor
(`collect_corpus.build_records`, imported), keeps the records whose id and
SHA-256 match, and writes them in the original order. Verified 2026-10-03 in
a clean copy of the repository: a **live** rebuild from the publishers' pages
(159 pages, ~12 min at the crawl delay) reproduced **473/473** quotations and
**560/560** paragraphs byte-for-byte — on the first pass one islamqa.info page
timed out (471/473); a second run fetched it (the script now retries once
automatically). The rebuilt corpus fingerprint equals the reference, and an
offline rebuild from the HTML cache does the same in under a second. A page
edited after
2026-10-02 drops its records — loudly, with a count in
`data/real/rebuild_report.json` — and the evaluation then reports that the
corpus differs from the reference. That is the price of not redistributing
someone else's text, and we think it is the right one.

### 5b. Held-out translations for the semantic-tier test

`tests/test_semantic.py --measure` (owned with the semantic tier; results in
`tests/results_semantic.json`, method in `docs/SEMANTIC.md` §6) measures what
the model adds on quotations from published translations that are **not** in
the approved index: en Asad, Arberry, Daryabadi, Itani, Wahiduddin, Ahmed Ali;
hi Farooq; ur Ahmed Ali, Jawadi, Kanz-ul-Iman; one Bengali edition — fetched
from AlQuran.cloud during the prototype phase. They are test inputs only, are
**not** in this repository, and remain under their translators' and
publishers' terms. The harness reads them from `../prototype/data/full/`
(`MIZAN_PROTOTYPE_DATA` overrides) and records each file's SHA-256 under
`protocol.provenance`. There is no fetch script for them in the repository
yet, so that one experiment is not re-derivable from a clean clone; the
real-corpus comparison in `mizan.eval --suite semantic` is.

## 6. What "the same data" means — fingerprints

| Fingerprint | Over | Checked by |
|---|---|---|
| per-file SHA-256 (22 + 2) | each Quranpedia translation file; Tanzil verse lines | `scripts/setup.sh` |
| index fingerprint `c5ce4090…` | every translation's metadata, every rendering and its `text_norm`, every Arabic verse — not the date label | `python3 -m mizan.eval --check-index` (setup step 5) |
| corpus fingerprint `e185c9b4…` | the measured fields of every real-corpus record, in order | `mizan.eval`, reported in `results/SUMMARY.md` |
| per-record SHA-256 | each real-corpus record | `scripts/rebuild_real_corpus.py` |

## Required attribution (copy this when you reuse MIZAN's data)

> Qur'an translations: **Quranpedia.net** — <https://quranpedia.net> — official
> dump 2026-10-01 (content verified identical through 2026-10-03). The
> translations remain the property of their respective translators and
> publishers.
> Arabic Qur'an text: **Tanzil Project** — <https://tanzil.net> — Tanzil Quran
> Text v1.1 (Uthmani, Simple Clean), CC BY 3.0, used verbatim.
