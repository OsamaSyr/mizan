# The semantic layer — MIZAN's AI, and what it measurably adds

> Tier (c) of detection: `BAAI/bge-m3`, run locally, proposing verse ids for
> passages whose wording matches no approved translation.
> Every number below was measured on 2026-10-03; §9 says how each was produced
> (three dev-split experiments ran from scratch scripts and are recorded, not
> re-runnable with one command). `tests/results_semantic.json` is
> authoritative over any number typed here.

## 1. In one paragraph

MIZAN finds Quranic quotations inside a translated text and attributes each
one to the approved published translation it came from. Its deterministic
tiers find a quotation by its *words*. That fails exactly when the words are
not an approved edition's words: a passage copied from a translation we do not
index (Asad, Arberry, an in-house rendering), or paraphrased. The semantic tier
finds such a passage by its *meaning*. On 983 quotations taken from 11
published translations that are **not** in the approved index (English and
Hindi placed inside ordinary prose, Urdu and Bengali checked bare), with no
reference printed, it raised the share located at the
correct verse from **30.7% to 54.5%** (English full verses: **60.0% → 83.0%**),
lost none, and sent 98% of what it located to human review rather than
attributing it. It never decides a verdict and never emits text. Since
2026-10-03 it also runs on a 4-vCPU CPU server in ~4.5 s for a 1,000-word
article (was ~14 s) with identical findings on every measured set (§12).

## 2. The red line, and how the code enforces it

| The model may | The model may not |
|---|---|
| propose `(surah, ayah, score)` for a sentence | return any string — verse text, a correction, an explanation |
| make a sentence a *candidate* quotation | decide MATCH / NEAR / UNATTRIBUTED |
| | touch the approved text, or the Arabic |

This is structural, not a promise:

- `detect.semantic_candidates()` returns a list of integer/float tuples. There
  is no code path from the model to a string in the report.
- Every candidate is then compared, by `engine.sim()`, against the **approved
  published string**. The state comes from `engine.attribute()` unchanged. A
  passage the model located but whose words match no edition is
  UNATTRIBUTED — a referral — whatever the model's confidence.
- In the held-out measurement the semantic tier located 207 quotations and
  produced **0 MATCH** verdicts (§6).
- If the ML packages, the pinned model or the index are missing,
  `semantic_candidates()` **raises** `SemanticUnavailable`. It never returns an
  empty list, which would read as "nothing here resembles a verse".

### Degraded mode — the alternative for a critical dependency

Plain `python3` with no ML packages runs the deterministic tiers only, with
identical code paths otherwise. `semantic.available()` decides this without
importing torch. `SpanDetector(..., semantic=None)` (the default) turns the
tier on exactly when it can run; `MIZAN_SEMANTIC=0` forces it off. Detection
records why in `detector.last_semantic`, and `semantic.status()` gives a UI
badge. The deterministic path is not a stub: it is the system, measured in §5
and §6 in its own right.

## 3. Index design — chosen by measurement

Three candidate designs were measured on a **dev split** (other editions,
other verses and other negative paragraphs than anything in §5–§6):

- **(a) Arabic only.** Embed the 6,236 Arabic verses once and rely on bge-m3's
  cross-lingual space for every language.
- **(b) Renderings.** Embed the approved renderings per language, either every
  edition separately (`all`, best row per verse) or one centroid per verse
  (`cent`).
- **(c) Both.** `w·cent + (1−w)·arabic`.

Queries were 2,746 dev items: held-out dev editions (en.shakir, en.qaribullah,
ur.jalandhry, bn.hoque, hi.hindi), full and partial, plus real published
quotations with their citation stripped. Negatives were 1,410 sentences of
dev-negative prose. The column that matters is the right one: a semantic span
is reported on the model's score alone, so a design is only as good as its
accuracy **at a threshold that rejects ordinary prose**.

| design | en full | en partial | en real | en full at 1% neg | en partial at 1% neg | hi full | ur full | bn full |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| (a) Arabic only | 0.844 | 0.540 | 0.468 | 0.669 | 0.274 | 0.676 | 0.812 | 0.815 |
| (b) every edition | 0.966 | **0.761** | 0.596 | 0.947 | 0.379 | 0.852 | **0.944** | 0.924 |
| (b) centroid | 0.980 | 0.724 | 0.585 | 0.966 | 0.444 | 0.852 | 0.940 | 0.908 |
| **(c) centroid 0.8 + Arabic 0.2** | **0.982** | 0.744 | **0.628** | **0.970** | **0.483** | 0.872 | 0.940 | **0.932** |
| (c) centroid 0.7 + Arabic 0.3 | 0.980 | 0.742 | 0.617 | 0.959 | 0.465 | **0.884** | 0.936 | **0.932** |

(R@1: the true verse ranked first. "at 1% neg": ranked first AND above the
score that only 1% of negative sentences reach. Urdu and Bengali have no
negative prose, so they have no calibrated column. Full per-group tables,
including R@5/R@20/MRR, are in `tests/results_semantic.json`.)

Paired over all 2,746 queries against the centroid (wins / losses at R@1):

| design | raw | at the 1%-negative threshold |
|---|---:|---:|
| Arabic only | +126 / −523 | +49 / −466 |
| every edition | +82 / −52 | +27 / **−70** |
| centroid + Arabic, 0.6 | +128 / −74 | +76 / −66 |
| centroid + Arabic, 0.7 | +108 / −48 | +62 / −42 |
| **centroid + Arabic, 0.8 — shipped** | **+85 / −26** | **+49 / −18** |

What chose it:

1. **Arabic-only is clearly worst** (full English verses 0.844 vs 0.980). The
   cross-lingual space works — 84% top-1 from English to classical Arabic is
   real — but a sentence is much closer to another rendering *in its own
   language* than to the Arabic.
2. **Keeping every edition separately loses once prose must be rejected**
   (−70 vs +27). Fifteen vectors per English verse give a sentence of ordinary
   prose fifteen chances to land near one. It is also 190 MB for English alone,
   over GitHub's 100 MB per-file limit for a repository that must be public.
3. **Adding the Arabic as a minority signal helps** at every weight, and 0.8
   has the best ratio of wins to losses. That is what ships.

The shipped index (`data/embeddings/`, 76.7 MB): one float16 matrix per
language (`{lang}.centroid.f16.npy`, 6,236 × 1024, 12.8 MB) plus the shared
Arabic matrix, a canonical verse order (`refs.json`), and `index.json` with
model, pinned revision, dimension, corpus version, build date and device.

Text preparation: renderings are cut at their footnote separator (`____`),
footnote markers and leading verse numbers are stripped, and the remainder is
truncated at 256 tokens. The verse always comes first, so this drops
commentary, not verse. The Bengali edition 1967 stores up to 1,272 words under
one verse, nearly all of it notes. Tanzil's simple-clean text prefixes the
basmala to ayah 1 of 112 surahs, and it is removed so those verses do not all
look alike.

## 4. The reranker — measured, not used

`BAAI/bge-reranker-v2-m3` re-scored the top 10 candidates for 600 dev queries
and 500 dev-negative sentences:

| | full | partial | real |
|---|---:|---:|---:|
| R@1, embedding only | **0.942** | 0.669 | **0.717** |
| R@1, reranked | 0.911 | **0.702** | 0.700 |
| R@1 at 1%-negative threshold, embedding | **0.825** | **0.322** | **0.463** |
| R@1 at 1%-negative threshold, reranked | 0.643 | 0.189 | 0.407 |

It helps raw ranking for partial quotations only, hurts full ones, and is
worse everywhere once it must also reject prose: its scores saturate near 1.0
on negatives (the 1% threshold was 0.997). It ran at 89 pairs/s on MPS, so
reranking 10 candidates for ~100 sentence chunks would add ~11 s to a
1,000-word document. **Skipped.**

## 5. False positives on ordinary prose (the most important number)

The deterministic tier used to fire on **38.3%** of no-quotation paragraphs.
The measured cause was short spans: the spurious ones clustered at the 6-word
floor. Short windows of religious prose share function words and one or two
theological terms with *some* short verse among 6,236.

**The fix.** A lexical span shorter than 12 words must now score at least
**0.75**, which is `engine.T_NEAR`. A short span is reported only if it is
itself at least NEAR-quality to an approved rendering. Chosen on the dev split
from every span the old gate accepted:

| rule for spans < 12 words | correct spans kept | spurious spans in 440 dev negatives |
|---|---:|---:|
| old (≥ 0.55, ≥ 6 words) | 423 / 423 | 139 |
| ≥ 0.70 | 398 (94.1%) | 11 |
| **≥ 0.75 (shipped)** | **387 (91.5%)** | **8** |
| ≥ 0.80 | 382 (90.3%) | 7 |
| ≥ 0.75 and 2 tokens with IDF ≥ 4 | fewer (short Hindi verses use common words) | 8 |

Short spans that score *above* 0.75 turned out to be almost all real Quranic
wording, including formula verses the Quran repeats verbatim ("Thus do We
reward the doers of good" is 37:80, 37:105, 37:110 and 37:121).

Dev results for the whole detector (560 inserted quotations, 440 negatives):

| | located at correct verse | negatives with any finding |
|---|---:|---:|
| `detect.py` before this work | 0.754 | 26.4% |
| after, deterministic | 0.718 | **1.8%** |
| after, with semantic tier | **0.764** | 2.0% |

The deterministic recall cost is concentrated in *partial* English
quotations (0.757 → 0.614): a short fragment of a long verse is exactly what
the gate distrusts. The semantic tier recovers part of the loss.

**Real-corpus probe (120 paragraphs, `tests/real_corpus.py`): 38.3% → 3.3%
deterministic, 4.2% with the semantic tier (§8).**

### A caveat that applies to every false-positive figure in this repo

The negative files were filtered by removing quote glyphs and printed
references, so some "negative" paragraphs **do** contain Quranic text, either
verbatim without quotation marks (3:190 and 85:22 in the dev set) or closely
paraphrased (84:8–9: "will have an easy reckoning… will happily return to his
family"). Part of every measured false-positive rate is therefore genuine
unquoted scripture. We did not relabel the files. The figures are reported
as measured.

## 6. What the AI adds: the held-out "unknown translation" test

`.venv/bin/python tests/test_semantic.py --measure`

**Items.** Published translations from AlQuran.cloud that are **not** in the
approved index. `scripts/fetch_heldout.py` downloads them (one request per
edition) into `data/heldout/`, which is gitignored: the translators' text is
never committed, only `data/heldout/manifest.json` with the SHA-256 of every
file. The fetched text is byte-identical to the copies this measurement was
first run on. Verified by title (no index title contains Asad, Arberry,
Daryabadi, Itani, Wahiduddin, Ahmed Ali, Farooq, Jawadi or Kanz) and by text
(mean best similarity to any approved edition 0.43–0.69):

- English: asad, arberry, daryabadi, itani, wahiduddin, ahmedali (50 verses each)
- Hindi: farooq (60). Urdu: ahmedali, jawadi, kanzuliman (40 each). Bengali: bengali (60).

The verses come from the half of a seeded verse split that tuning never saw,
and are 8–90 words long. **8 items were excluded** because the held-out
rendering is ≥ 0.92 similar to an approved one, i.e. it is in the index.
Each rendering is used whole ("full") and as a contiguous 50–70% slice
("partial"), placed at a sentence boundary inside a paragraph from the
120-paragraph probe (English, Hindi). That prose was never used for tuning.
No reference is printed anywhere. Urdu and Bengali have no negative prose in
the corpus, so they are checked bare and reported separately. Each item runs
through `report.check_document`, the same call the app makes.

**Located** means a finding at the correct verse id whose span overlaps the
inserted quotation.

| group | n | lexical only | + semantic |
|---|---:|---:|---:|
| English, full, in prose | 300 | 60.0% | **83.0%** |
| English, partial, in prose | 243 | 28.4% | 35.8% |
| Hindi, full, in prose | 60 | 23.3% | 48.3% |
| Hindi, partial, in prose | 51 | 3.9% | 9.8% |
| Urdu, full, bare | 120 | 26.7% | 77.5% |
| Urdu, partial, bare | 103 | 1.9% | 20.4% |
| Bengali, full, bare | 60 | 5.0% | 70.0% |
| Bengali, partial, bare | 46 | 0.0% | 21.7% |
| **all** | **983** | **30.7%** | **54.5%** |

Paired on the same items: **+234 located, −0 lost.**

Per English edition (full and partial together): ahmedali 36.0 → 51.7,
arberry 47.8 → 62.2, asad 41.5 → 58.5, daryabadi 46.2 → 63.7, itani 55.7 →
69.3, wahiduddin 48.4 → 65.9.

Per Hindi/Urdu/Bengali edition: farooq 14.4 → 30.6, ur.ahmedali 16.2 →
58.1, jawadi 17.6 → 54.1, kanzuliman 12.0 → 41.3, bn.bengali 2.8 → 49.1.

**Referral, not attribution.** Of the 536 located with the semantic tier on,
68 were NEAR, 459 UNATTRIBUTED and 9 MATCH, so **98.3% were referred**
(99.1% before `report.py` gained partial-quotation attribution on 2026-10-03;
the located set is unchanged). The 207 located by the semantic tier itself
produced **0 MATCH**.

**The MATCH cases, examined one by one.** 13 of 983 items (1.3%) have a MATCH
finding overlapping the quotation, 12 of them with the semantic tier switched
off (7 and 6 before partial attribution). The one added by tier (c) is Asad
75:21 below: the model proposed the verse, and the lexical tier matched words
that are an approved edition's words. In every case the matched words really
are an approved edition's words:

- Asad 37:82 "…and then We caused the others to drown" ends with Salahi's
  "Then We caused the others to drown." verbatim (1.0).
- Asad 75:21 "and give no thought to the life to come [and to Judgment Day]!"
  is Salahi's 75:21 plus Asad's bracketed gloss.
- Wahiduddin 28:8 is ≥ 0.92 identical to Abdul Haleem's; Itani 74:24 to
  Rowwad's; Daryabadi 50:3 to edition 1948.
- "He is the All-hearing, the All-knowing" inside Arberry's 6:13 is verbatim
  Abdul Haleem's **26:220**.

Translators borrow from each other, and the Quran repeats its formulas. When
the published string *is* an approved string, no text comparison can know
which book it was copied from. That is a limit of attribution by text, not an
error the model introduced. The pre-filter compared whole renderings, and
these are sub-spans.

The six new MATCHes come from `report.py`'s partial attribution, with or
without tier (c), and are short clauses. Three of them match a **different
verse** that repeats the same formula: Arberry 16:42 → 29:59 ("…patient, and
put their trust in their Lord"), Ahmed Ali 38:72 → 15:29 (near-identical
verses), and Arberry 6:13 → 26:220 as above. The words are approved text; the
verse id is the one a reader of that clause cannot tell apart either. This is
flagged to the owner of `report.py`, not fixed here.

**False positives, same run.** On the 120-paragraph probe the deterministic
path fired on 4 paragraphs (3.3%) and the semantic path on **5 (4.2%)**. The
extra one is a genuine semantic false positive: "Those that believed were
followers of Islam, or Muslims." proposed as 10:63 (UNATTRIBUTED, a
referral). We did not adjust any threshold after seeing it. Both are far
below the 38.3% this work started from, but **the semantic tier did add one
false positive on the test probe**, and we report that rather than hide it.

**Latency, same run.** Median per item 110 ms lexical-only vs 181 ms with
semantic (p95 442 vs 586 ms). These are short documents (one paragraph); see
§7 for full pages.

## 7. Latency

> Timings in this section are Apple M5 (MPS). For a Linux CPU server — what
> the public demo runs on — see §12: 1,000-word article 14.1–14.5 s →
> 4.3–4.7 s on 4 vCPUs.

The 12 longest real documents (mean 974 words; one process; model warm),
measured three times. The machine was shared with a virtual machine using up
to ~4 cores, which is why the same code varies between runs. All three are
reported:

| | median | mean | max |
|---|---:|---:|---:|
| `detect.py` before this work (`real_corpus.py`) | 6.9 s | 11.1 s | 31.1 s |
| after, deterministic — `real_corpus.py` final run | 1.73 s | 1.92 s | 3.21 s |
| after, deterministic — earlier run, same code minus the §7b precedence fix | 1.17 s | 1.34 s | 2.16 s |
| after, deterministic — direct timing, load average 11 | 1.16 s | 1.47 s | 3.28 s |
| after, with semantic — `real_corpus.py` final run | 2.82 s | 2.89 s | 4.56 s |
| after, with semantic — earlier run | 1.86 s | 2.02 s | 3.28 s |
| after, with semantic — direct timing, load average 11 | 2.67 s | 2.73 s | 3.86 s |

**Against the target** (< 3 s for a typical 1,000-word document including the
semantic pass), the median meets it in every run: 1.86–2.82 s. The longest
English document (1,271 words; English has 15 editions to compare against)
does not: 3.2–4.6 s with semantic. Per quotation: 169 ms mean before, 26–35 ms
after (deterministic), 52–78 ms with semantic.

Where the time went, and what removed it. Every change was measured on the dev
split for recall and false positives before being kept.

- **Exact speedups.** Approved renderings are normalised once (`text_norm`
  equals `engine.normalize(text)` for all 137,180 rows, verified). Each
  rendering keeps a `SequenceMatcher` whose character index is already built.
  A comparison is skipped only when a provable upper bound on `sim()`
  (character-length ratio plus exact Jaccard) says it cannot win. Postings
  are aggregated to verse level at build time, 15× shorter for English. With
  the old gate this alone was a 3× speedup. Output was identical on 119/120
  negatives and 9/12 documents; the remaining differences were one-word shifts
  at an identical score (a punctuation-only token) and the score increases
  explained in the next point.
- **A real bug found on the way.** `_length_ceiling` (a word-count ratio) was
  used as an upper bound on `sim()` and is not one, because `SequenceMatcher`
  works on characters. It could discard the best edition of a verse. One
  printed-reference span in the corpus was scored 0.744 when its true best
  edition gives 1.0. It no longer prunes anything.
- **Seeding economics**, measured on dev: stride `size//3` instead of
  `size//4`; only lexical candidates scoring ≥ 40% of the best; 2 editions
  per candidate during seeding (the reported score is still exact over all
  editions); 5 candidates instead of 8; and no re-seeding inside an accepted
  span. Measured back to back on dev: the first four took the deterministic
  path from 359 to 225 ms per 100 words (recall 0.721 → 0.720); 8 → 5
  candidates took a further 17% (226 → 188, semantic tier on) at identical
  recall. Absolute dev timings move with machine load; an identical later
  run measured 233.
- **Semantic cost**: one batched forward pass over ~100–140 sentence chunks
  per 1,000 words (0.25–0.7 s on MPS) plus one 6,236-row matrix product per
  index part (< 0.03 s).
- **Index shared per process.** `report.check_document` builds a
  `SpanDetector` per call when none is passed, and the index is now cached at
  module level, so that costs nothing after the first call.

## 7b. Defects found by clicking the demo, and their fixes

The app's three hardcoded samples (`web/app.js`) exposed four detection
defects. All four are fixed, and each has a regression test in
`tests/test_detect.py`.

| sample | before | after |
|---|---|---|
| 3:59 "the likeness of Jesus…" (Quran 3:59). Being created… | span ran past the citation into the next sentence; in the original detector a spurious **37:181** span ("peace be upon him: "Indeed, the likeness", 0.588) beat 3:59 (0.582), so 3:59 vanished | span is exactly the quoted words; 37:181 gone (short-span gate) |
| 2:255 "Allah! There is no deity save Him, the Alive, the Eternal." (Quran 2:255) | reported **3:2**, whose text is identical to 2:255's opening | publisher's 2:255 wins; coverage 1.0 |
| 51:56 "…except to serve Me." | span stopped at "except to" (closest edition says "worship Me") | span ends at the author's closing quotation mark |
| any text with `arabic_text` | Arabic-placed spans labelled `reference` | labelled `arabic` |

The mechanisms:

- **A citation is a wall.** `citation_word_ranges()` gives every printed
  reference's word extent, and no span may cross one.
- **Tier (a) searches both sides.** It looks before the citation (the default)
  and after it (it must win by 0.10), each confined between neighbouring
  citations.
- **Two placement routes for a cited verse.** One is whole-verse similarity.
  The other is the run of words *contained* in a plain rendering, for partial
  quotations, using renderings cut at the footnote separator and excluding
  tafsir-length editions. Edition 27824 renders 3:59 in 71 words that include
  "without a father", the author's next sentence.
- **Precedence in overlap resolution.** The printed id comes first, then an
  Arabic-resolved id, then the best whole-verse similarity. A span snaps to the
  author's quotation marks only if that costs at most 0.03 similarity.

A regression these changes first introduced was caught by the real corpus and
fixed before the numbers below. Preferring the coverage route had turned 7
correctly placed MATCH/NEAR quotations (e.g. 24:30 MATCH 0.945) into
UNATTRIBUTED sub-spans, and unbounded snapping had stretched a 56:25 span over
56:26. The remaining state changes against the old run are intended: the
publisher's cited verse replacing a different verse (2:163 instead of 1:3; 54:1
instead of 54:2).

**Two things detection cannot fix, flagged to their owners.** The 2:255 and
3:59 demo quotations are faithful *partial* quotations, and `engine.attribute()`
scores them against the whole verse, so they still end UNATTRIBUTED (0.20 and
0.51). `Span.coverage` (1.0 and 0.92 here) and `Span.coverage_book` are
exposed for the report layer to use. With the Arabic supplied, `arabic.py`
resolves 3:2 as well as the cited 2:255, because 3:2's text is 2:255's opening.
Detection places the publisher's 2:255, and `report.check_document` then lists
3:2 as "quoted in the Arabic but not located".

## 8. `tests/real_corpus.py`, before and after

`tests/real_corpus.py` is the unedited measurement harness: 473 published
quotations, and the 120-paragraph no-quotation probe. "Before" is `detect.py`
as it was when this work started. "After" is plain `python3`, the
deterministic path. "+ semantic" is the same harness under `.venv/bin/python`,
where `SpanDetector` enables tier (c) by default.

| | before | after | after + semantic |
|---|---:|---:|---:|
| **FP probe: paragraphs with any finding** | **46 / 120 = 38.3%** | **4 / 120 = 3.3%** | **5 / 120 = 4.2%** |
| spurious findings | 64 | 5 | 6 |
| false attributions (MATCH/NEAR on prose; since 2026-10-04 MATCH-only, which is 0 in all three) | 2 | 2 | 2 |
| detected, citation stripped | 150 (31.7%) | 105 (22.2%) | **181 (38.3%)** |
| — correct cited verse, citation stripped | 55 of 103 | 54 of 72 | **96 of 120** |
| detected, citation in place | 211 (44.6%) | 237 (50.1%) | **274 (57.9%)** |
| — reference accuracy, citation in place | 130/164 = 79.3% | **198/204 = 97.1%** | 204/213 = 95.8% |
| tier (a) answers surviving to the report | 136/262 = 51.9% | 201 = 76.7% | 209 = 79.8% |
| MATCH / NEAR / UNATTRIBUTED / UNRESOLVED (in place) | 17 / 37 / 157 / 262 | 15 / 37 / 185 / 236 | 15 / 37 / 222 / 199 |
| Urdu detected (no citations exist) | 34 (27.0%) | 32 (25.4%) | **59 (46.8%)** |
| per-document latency, median / max | 6.9 s / 31.1 s | 1.73 s / 3.21 s | 2.82 s / 4.56 s |

How to read it:

- **False positives fell by an order of magnitude** (38.3% → 3.3%). The two
  remaining false attributions are the same two as before: "the Books of
  Abraham, the Torah of Moses" against 87:19 ("The Books of Abraham and
  Moses", 0.773), and a 5-word Tagalog span against 114:3. The bar was set on
  dev and was not moved to remove them. The semantic tier adds one paragraph
  ("Those that believed were followers of Islam, or Muslims." → 10:63).
- **With the citation stripped, the deterministic path detects less but
  better.** 45 fewer detections, of which only 1 was the publisher's cited
  verse; the stricter short-span gate mostly removed wrong-verse hits
  (accuracy 53.4% → 75.0%).
- **The semantic tier is what raises detection.** 96 correct cited verses with
  the citation stripped, against 55 before and 54 deterministic. Urdu, which
  this corpus can only judge by detection, goes from 25.4% to 46.8%.
- **The publisher's citation now survives.** Reference accuracy went from
  79.3% to 97.1% with the citation in place, through the citation wall,
  containment placement and cited-verse precedence (§7b). Tier (a) survival
  went from 51.9% to 76.7%. The rest are mostly very short fragments
  ("·Worker bees being female." cited as 16:68) that are not quotations.
- **Most UNATTRIBUTED results are not detection failures.** The rise in
  UNATTRIBUTED is mostly quotations that are now located and referred: partial
  quotations, and renderings from translations outside the index. Turning a
  faithful partial quotation into "partial quotation of <edition>" is a
  report-layer change (§11).

## 9. Reproducing

```bash
# deterministic path — no ML packages needed
python3 -m pytest tests/test_detect.py tests/test_semantic.py   # semantic tests skip
python3 tests/real_corpus.py

# semantic path
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-ml.txt                    # 916 MB (Linux: CPU torch first, see the file)
.venv/bin/python scripts/build_embeddings.py --download         # 2.1 GB, pinned revision
.venv/bin/python scripts/build_embeddings.py                    # ~9 min on Apple MPS; index is in the repo
.venv/bin/python -m pytest tests/test_detect.py tests/test_semantic.py
python3 scripts/fetch_heldout.py                                # held-out editions, 16 requests, gitignored
.venv/bin/python tests/test_semantic.py --measure               # writes tests/results_semantic.json
.venv/bin/python tests/real_corpus.py                           # semantic path of the same harness
```

The held-out test reads `data/heldout/` (run `python3 scripts/fetch_heldout.py`
first; `--verify` checks the files against the committed manifest), falling
back to `../prototype/data/full/` (`MIZAN_HELDOUT_DIR` overrides). The content
SHA-256 of every edition used, and whether it matches the manifest, is in
`tests/results_semantic.json` under `protocol.provenance`.

`tests/results_semantic.json` is committed and **text-free**: verse ids, item
ids, states, scores, counts and SHA-256s only. Span text from the probe goes
to `data/heldout/results_semantic_detail.json`, which is gitignored with the
rest of that directory.

The dev-split tuning runs, the index-design experiment and the reranker
experiment were run from scratch scripts that are not part of the repository;
their outputs are recorded in `tests/results_semantic.json`
(`dev_split`, `index_design_experiment`, `reranker_experiment`). The
definitions are in §3–§5 and are reproducible from them, but not with one
command.

## 10. What this shows, and what it does not

### It shows

- A local multilingual embedding model, used **only to propose verse ids**,
  raises recall on quotations from unindexed published translations
  substantially: +234 of 983, none lost. The gain is largest exactly where
  the lexical tier is weakest (Urdu 26.7 → 77.5%, Bengali 5.0 → 70.0% on full
  renderings).
- It does so while the verdict stays deterministic: 98.3% of what it helped
  locate was referred, and it produced no MATCH of its own.
- The index design was chosen on held-out data, and the obvious alternative of
  embedding the Arabic once and relying on cross-lingual alignment was
  measured and was clearly worse.
- The false-positive rate on real no-quotation prose fell by an order of
  magnitude, from a change to the deterministic gate rather than from the
  model.

### It does not show

- **That any text is wrong.** UNATTRIBUTED means "matches none of the 22
  approved translations we index". Every held-out item is a respected
  published translation. Referring it is the correct behaviour for a gate
  whose index does not contain it.
- **Performance on paraphrase in general.** The held-out items are other
  translators' renderings, which is closer to "unknown translation" than to
  free paraphrase. Fresh paraphrases written for the tests are located for
  some verses and missed for others. The misses we looked at are verses
  whose wording the Quran itself repeats ("every soul shall taste death" is
  3:185, 21:35 and 29:57), where the margin gate declines to pick one.
- **Partial quotations are solved.** Partial English items reach only 35.8%
  with the semantic tier, and the stricter short-span gate costs the
  deterministic tier partial-quotation recall on dev (0.757 → 0.614 in English).
- **Urdu/Bengali behaviour in prose.** They were measured bare because the
  corpus has no negative prose in those languages. Their thresholds were never
  checked against Urdu or Bengali non-quotation text.
- **Provenance when strings are identical.** See the MATCH cases in §6.
- **Anything about publishers.** Nothing here measures how often anyone
  misquotes.

## 11. Known gaps

- One semantic false positive on the test probe (§6). Thresholds were not
  adjusted after seeing it.
- Partial quotations: `report.py` gained partial-quotation attribution on
  2026-10-03 (not part of this work). On the held-out set it attributes some
  short repeated formulas to a different verse that shares them (§6).
  Detection exposes `Span.coverage` / `Span.coverage_book` for it.
- Sentence chunks cover one or two sentences. A three-sentence verse whose
  sentences individually resemble other verses (2:255 opens with the whole of
  3:2) is reported as those verses, not as one quotation.
- On a CPU server a document longer than the token budget (~250+ words of
  unexplained text) gets a partial semantic pass: the most verse-like
  chunks first, the rest listed in `last_semantic["dropped"]` (§12). The
  1,000-word benchmark article and the 2,501-word document lose nothing
  measurable (identical findings), but that is two documents, not a proof.
  `MIZAN_SEM_TOKEN_BUDGET` raises the budget on bigger servers.
- 1,000-word article on 4 vCPUs: 4.3–4.7 s, not under 4 s (§12).
- First request after start-up pays the model load (2.7–10 s). Callers should
  call `mizan.semantic.warmup(langs)` at start-up. A failed load is now
  remembered and retried with back-off (60 s doubling to 1 h) instead of on
  every check; `semantic.status()["load_failure"]` reports it.

## 12. Making tier (c) fast on a CPU server (2026-10-03)

The deployment measurement (`docs/DEPLOY.md`) showed the semantic tier was
fine on Apple MPS but slow on a Linux CPU: a 1,000-word article embedded 111
chunks / 4,384 tokens and took ~17 s on 4 vCPUs. The brief: make it fast
**without changing what it finds**, by an algorithmic cut rather than only a
faster runtime.

### Where the time went

Embedding is linear in tokens: ~3.5 ms per token on 4 vCPUs at batch 32.
Measured on the dev split, only **56 of 17,445 embedded chunks (0.3%)** ever
changed the output: 31 produced a semantic-only span (20 of them from
sentence *pairs*) and 25 corrected a lexical span's verse. Almost all the
work was spent establishing that ordinary prose is not scripture.

### What ships

1. **Explained text is not embedded.** `find()` now runs in phases. Phase 1
   is the deterministic tiers, exactly as on the plain-python path. A
   sentence at least half covered by a span phase 1 reported is not
   embedded, because tier (c) cannot report a span there.
2. **A per-request token budget, spent in priority order.**
   `SEM_TOKEN_BUDGET` = 1,024 tokens (env `MIZAN_SEM_TOKEN_BUDGET`), in four
   rounds:
   1. sentences that are lexically **verse-like**: `scripture_likeness` ≥
      0.35, i.e. at least 35% of the sentence's IDF-weighted vocabulary is in
      one approved rendering of one of its 40 lexical candidate verses.
      Model-free; 0.04–0.16 s per document.
   2. adjacent **pairs** one of whose sentences the model already placed with
      score ≥ 0.70;
   3. every other sentence;
   4. every other pair.

   A text that fits the budget gets all four rounds, i.e. the full semantic
   pass. A longer one gets the most promising chunks first. What did not fit
   is listed in `SpanDetector.last_semantic["dropped"]` (word range, stage,
   priority, tokens) and logged by the `mizan.detect` logger. Nothing is
   dropped silently.
3. **Batch 4 on CPU.** One batch of 32 pads every chunk to the longest. On
   a CPU, padding costs as much as real tokens: the same 28 chunks took 3.92
   s at batch 32 and 1.92 s at batch 4, with embeddings equal to within
   2e-7. `CPU_BATCH = 4`, `GPU_BATCH = 32`.
4. **Exact work removal in the deterministic phase:**
   - a second provable upper bound on `sim()` (difflib's `quick_ratio`
     argument: matched characters ≤ Σ min(char counts)), applied in
     seeding, best-edition search and hill-climbing;
   - each window's phase-1 seed is cached, so the augmented pass scores
     only the model's *new* candidate against it. Augmented windows went
     from 0.41 s to 0.08 s.

   Both are verified bit-identical: 12/12 real documents, 120/120 probe
   paragraphs, and 1,000/1,000 dev documents.

### What was tried and rejected: the gate as a filter

The first version embedded **only** verse-like sentences (and pairs
confirmed by the model). On the English/Hindi/Tagalog dev split it was
lossless: recall 0.764 → 0.764, false positives 2.0% → 2.0%, 997/1,000
documents identical, at 20% of the tokens. It then failed on held-out and
real text in the languages the dev split did not cover. Urdu, Bengali and
Tagalog quotations from translations we do not index share too little
vocabulary with our approved renderings to pass a lexical gate:

| | real corpus, citation stripped | as published | Urdu | Tagalog (stripped) | held-out located |
|---|---:|---:|---:|---:|---:|
| previous code (all chunks) | 181 | 274 | 59 | 19 | 536 |
| gate as a filter (rejected) | 157 | 257 | 42 | 12 | 515 |
| **budget + priority (ships)** | **181** | **274** | **59** | **19** | **536** |

Hence priority, not filter. Every real-corpus item (max 955 tokens of
chunks) and all but a handful of held-out items (max 1,091) fit the budget,
so they get the full pass. A long article falls back to the gate's ordering.

### Accuracy before and after, same report.py in every run

| | before (previous `detect.py`) | after (ships) |
|---|---:|---:|
| dev split: located / FP paragraphs / false attributions | 0.764 / 2.0% / 4 | 0.764 / 2.0% / 4 (999/1,000 docs identical) |
| held-out 983: located | 536 (54.5%) | 536 (54.5%); paired: 0 lost, 0 gained, 0 state changes |
| held-out: false-MATCH items / probe false attributions | 13 / 2 | 13 / 2 |
| real corpus, citation stripped: located / correct cited verse | 181 / 96 of 120 | 181 / 96 of 120 |
| real corpus, as published: located / reference accuracy | 274 / 95.8% | 274 / 95.8% |
| real corpus, Urdu located | 59 | 59 |
| real-corpus probe: paragraphs with a finding / false attributions | 5 / 2 | 5 / 2 (identical findings) |

Real corpus: 0 of 473 items differ in either condition. The deterministic
path (plain `python3`) is unchanged by this work: 105 / 237 located, 4/120
probe paragraphs, 2 false attributions.

### Latency on a Linux CPU server (4 vCPUs)

`deploy/experiments/bench_server.py`: `app.py` over HTTP, model warm, Docker
`mizan-bench` (torch 2.14.1+cpu), `--cpuset-cpus=0-3`. "Before" mounts the
previous `detect.py` with the old batch size. The 1,000-word article is
`make_docs.py`'s 987-word real article (10 quotations). The 2,501-word
document is that article plus the most citation-rich other English articles
in `data/real/raw`, cut at a paragraph boundary.

| | before | after | deterministic only |
|---|---:|---:|---:|
| 1,000-word article | 14.1–14.5 s | **4.3–4.7 s** | 1.66–1.73 s |
| 2,501-word document | 32.9–34.6 s | **7.1–7.5 s** | 4.2–4.3 s |
| 37-word sample | 0.38–0.41 s | 0.34–0.41 s | 0.04 s |
| AI probe (49:13 paraphrase) | located, 1.0 s | located, 1.0–1.1 s | not located |
| findings on the two documents | 10 / 23 | 10 / 23 (identical) | 10 / 23 |
| peak RSS | 2.42–2.51 GB | 2.28–2.33 GB | 0.45 GB |

The after column comes from three separate server starts for the 1,000-word
article (4.64/4.31, 4.52/4.52/4.31, 4.73/4.43/4.33). One first check took
8.72 s and did not reproduce in the two repeats; it is in the JSON.

Where the 4.3 s goes (in-process, 1,000 words): phase-1 windows 1.30 s,
verse-likeness 0.04 s, encoding 2.49 s (33 chunks, 1,022 tokens), augmented
windows 0.09 s, the rest (report building) ~0.4 s. **The < 4 s target is not
met** on this article with the default budget. The rejected filter version
reached 3.75–4.10 s by embedding 873 tokens. Lowering the budget to ~850
would get under 4 s and would truncate the semantic pass for more long
texts. That is a hosting decision, so it is a knob
(`MIZAN_SEM_TOKEN_BUDGET`), not a silent default. At ~2.4 ms per token, each
100 tokens of budget costs ~0.24 s on 4 vCPUs.

ONNX Runtime fp32 (1.4× faster, identical results per `DEPLOY.md`) was not
added: it needs a 2.2 GB export and a separate code path. int8 was rejected
by the deployment measurement because it changes rankings.

### Also in this round

- A failed model load is remembered. `semantic.encoder()` raises at once
  until a back-off expires (60 s, doubling to 1 h; `MIZAN_SEMANTIC_RETRY_S`).
  Before, it re-read 2.2 GB on every check.
- `requirements-ml.txt` documents installing torch from the CPU wheel index
  on Linux. Checked: pip then keeps `2.14.1+cpu` and pulls no CUDA packages.
- The shipped index is **unchanged** (`data/embeddings/`, built
  2026-10-03T15:42Z); nothing here touches what is embedded into it.

