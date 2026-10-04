# Known limits — measured

Stated before a judge finds them. Numbers on this page are re-derived by
`make eval` (seeds 20261001–20261003) and appear, with their spread, in
[`results/SUMMARY.md`](../results/SUMMARY.md); the values below are from the
run of 2026-10-03. Three are computed directly from the index rather than by
the eval, and say so: the identical-verse shares in §1 (each pair compared
over all 6,236 verses), and the token statistics and the Hindi example in §4.
The semantic held-out figures come from `tests/results_semantic.json`.

The common thread: **every limit below fails towards referral, not towards
false approval.** `UNATTRIBUTED` means "not in this index — a person should
look". The one error MIZAN must never make — naming an approved translation
for text that is not from it — is measured separately (§2) and is the number
to watch.

---

## 1. Exact edition vs. approved translation (near-identical English editions)

| | |
|---|---|
| Approved text fed back, any language | **0** false alarms in 8,321 renderings (control) |
| Attributed to the *exact* edition it came from | **90.7 %** (en 88.5 %, ur 88.7 %, bn 100 %, tl 99.9 %) |
| Wrong-edition answers that are **exact score ties** | **100 %** of them |

Quranpedia publishes several translations as two book entries — typically one
plain and one carrying translator footnotes:

| Edition fed back | Named instead | verses identical after normalization (all 6,236, computed from the index) |
|---|---|---|
| Dr. Mustafa Khattab (13662) | Dr. Mustafa Khattab, The Clear Quran (13661) | 81.6 % |
| Sahih International (13638) | الترجمة الإنجليزية - صحيح انترناشونال (1947) | 41.6 % |
| Muhsin Khan (13603) | الترجمة الإنجليزية (1948) | 35.6 % |
| محمد جوناگڑھی (13625) | الترجمة الأردية (1966) | 22.3 % |

Where the two entries print the same words for a verse, no text comparison
can tell them apart; `engine.attribute` keeps the first maximum, so the lower
book id is named. Every wrong-edition answer in the calibration sample was such
a tie (`ties.*` rows in SUMMARY.md). The answer is still *an approved
translation, correctly identified as such* — it names the wrong copy of the
right translation, and accuses no one. What MIZAN cannot do is tell a
publisher which of two identical copies they used.

## 2. Real published text is much harder than the index fed back to itself

473 quotations from islamreligion.com and islamqa.info (en 130, ur 126, hi 94,
tl 85, bn 38); ground truth is the publisher's own printed reference
([REAL_CORPUS.md](REAL_CORPUS.md)). Deterministic path, no model:

| | as published (citation in place) | citation stripped |
|---|---|---|
| quotation located | **50.1 %** | 22.2 % |
| …located on the verse the publisher cited | **97.1 %** (of 204 cited) | 75.0 % |
| attributed to an approved translation (`MATCH`) | **3.0 %** | 2.1 % |
| `NEAR` — close, words differ (referred) | 8.7 % | — |
| `UNATTRIBUTED` (referred) | 38.5 % | — |
| `UNRESOLVED` | 49.9 % | — |

By language, as published — located: bn 84.2 %, en 63.8 %, hi 55.3 %,
tl 44.7 %, **ur 25.4 %**; attributed: en 10.0 %, ur 0.8 %, bn/hi/tl 0 %.
Urdu items carry no printed reference MIZAN can read, so Urdu reference
accuracy cannot be measured at all.

**Why attribution is low here — and why that is the correct answer.** Until
2026-10-04, "attributed" counted NEAR too (13.1 %). Since the red-team review
([REDTEAM.md](REDTEAM.md)) only a word-for-word approved rendering is
attributed. On this corpus most located quotations differ from every
approved translation in their words — e.g. "God" printed for "Allah", "you"
for "ye", an added gloss, or a translation outside the 22 indexed — so they
are referred with the differing words shown. That says nothing about how
often publishers misquote (not measured, not claimable from two hosts).
The opposite direction is measured separately: verses copied verbatim from
the 22 approved editions, placed in prose with their printed reference,
come out MATCH in 499 of 500 random cases across all five languages (a
one-off check, seed 20261006, 100 per language; without the printed
reference 465 of 500 — very short verses and word-identical refrains such as
55:13/55:16 account for the misses). A fixed 40-verse version runs in
`tests/test_redteam.py::test_approved_verses_quoted_verbatim_pass_in_every_language`.

Why so far below the calibration numbers:

* **Partial quotations and ellipses** (“…and the sky a ceiling…”) are the
  norm in da'wah writing and nearly absent from a self-referential test. A
  printed citation is read correctly 100 % of the time and 76.7 % of those
  answers survive to the final report; the rest are mostly very short
  fragments. A faithful clause of 8+ words can now be attributed as a partial
  `MATCH` (below), but most fragments stay referred.
* **Translations MIZAN does not index.** Most located quotations score below
  NEAR against every indexed rendering — the publisher used a translation (or
  an in-house rendering) outside the 22. That is exactly what `UNATTRIBUTED`
  is for; it is a statement about the index as much as about the text.
* **The citation-stripped condition** measures the lexical tier alone and is
  harsher than reality; the "as published" column is how text arrives.

**False positives on ordinary prose** (all 560 paragraphs of the same
authors' prose with no quotation marks or printed reference in them): 2.1 % of
paragraphs get any finding; **0 get a false attribution** (`MATCH` on
prose). Until 2026-10-04 this read 1.1 % (6 paragraphs), counting spans of
6–22 words that scored NEAR against 87:19, 82:15, 84:8, 85:22, 3:190 or
114:3; NEAR now refers rather than attributes, so those six are referrals —
a reviewer's glance, not a claim. Part of the residue is the test set's own
noise: some negatives contain unmarked Quranic wording (3:190, 85:22,
84:8–9 — [SEMANTIC.md](SEMANTIC.md) §5). Hindi prose is the noisiest (3.5 %
of paragraphs with a finding).

**Partial-quotation MATCH is a calibrated risk, not a free win.** The report
layer upgrades a clause (≥ 8 words) to `MATCH` when it is the verbatim
wording of an approved rendering's clause (sim ≥ 0.92, ≥ 95 % of its words
present, and word-identical since 2026-10-04). On clauses cut from six published translations that are *not* in
the index, **6.1 %** were still upgraded — translators borrow heavily from
each other, and at clause level two translators can print the same words.
Those clauses really are word-for-word an approved translator's wording, but
the publisher may have taken them from elsewhere. The looser rule that also
allowed `NEAR` upgraded 52 % and was rejected (`report.py`, table above
`PARTIAL_MIN_WORDS`). Recognition of genuine approved clauses is 88.1 %.

What these numbers do **not** show: how often publishers misquote. That was
not measured and this corpus could not support it.

## 3. Footnoted editions

Some editions store translator commentary in the same field as the verse,
after a `____` separator. The engine compares against the whole stored
rendering, so a publisher who faithfully quotes only the verse text is
compared against verse + commentary. Measured by feeding each edition's verse
text (commentary removed) back to MIZAN:

| Edition | verses with commentary | verse text alone → UNATTRIBUTED | why it matters |
|---|---|---|---|
| الترجمة البنغالية (1967), bn | 3,592 / 6,236 | **97 %** | the other Bengali edition does not rescue it |
| الترجمة الهندية (1986), hi | 2,107 / 6,236 | **67 %** | the **only** Hindi edition |
| الترجمة الإنجليزية (1948), en | 718 | 7 % | usually re-attributed to its plain twin |
| صحيح انترناشونال (1947), en | 1,612 | 2 % | idem |
| الترجمة الأردية (1966), ur | 4,803 | 0.4 % | re-attributed to Junagarhi (13625) |

So a verbatim quotation of the Bengali 1967 or Hindi 1986 translation is
usually *referred* rather than attributed. Fixing it means storing verse and
commentary separately at index-build time — a versioned change to the index
(and therefore to every number), not a tweak.

## 4. Languages with few approved translations

| Language | approved translations indexed |
|---|---|
| English | 15 |
| Urdu | 2 |
| Bengali | 2 |
| Tagalog | 2 |
| Hindi | **1** |

* A quotation from any approved translation not in this list is
  `UNATTRIBUTED` — in thin languages that is most translations.
* With one edition, Hindi cannot be calibrated the way the others are (the
  control and identity tests need two editions). Its wrong-verse margin is
  measured instead: the best score a different verse reaches is 0.37 at p95
  and 0.51 at most, against a NEAR threshold of 0.75.
* **Normalization fragments Hindi and Bengali words.** The frozen rule
  `[^\w\s] → space` removes dependent vowel signs and viramas, so ~70 % of
  normalized Hindi/Bengali tokens are single consonants, and **a change that
  touches only vowels is invisible** (`sim("अल्लाह के नाम से",
  "अल्लाहु की नामो सी") = 1.0`). Details and the fix (a versioned
  normalization change): [NORMALIZATION.md §3](NORMALIZATION.md).
* Urdu-specific letters (`ہ ی ک ے`) are not folded to their Arabic
  counterparts; Urdu text is compared as written.

## 5. Other limits

* **Index coverage is the ceiling.** 22 translations from one publisher's
  dump. Well-known translations outside it (e.g. Asad, Arberry) are not
  indexed; their quotations will be referred.
* **The semantic tier is optional and proposes ids only.** It raises
  *detection* — on quotations from 11 translations outside the index, located
  at the correct verse 30.7 % → 54.5 % (`tests/results_semantic.json`) — but
  it cannot raise *attribution*, which is the string comparison; 99 % of what
  it finds is referred. That held-out experiment reads translation files that
  are not redistributed in this repository (`docs/SOURCES.md` §5b);
  `scripts/fetch_heldout.py` downloads them and checks them against the
  committed SHA-256 manifest (`data/heldout/manifest.json`). Its real-corpus effect is
  re-measured by `mizan.eval --suite semantic`: located with the citation
  stripped +16.1 pp (22.2 % → 38.3 %), as published +7.8 pp (50.1 % →
  57.9 %), attributed +0.0 pp, false attribution on the 560 prose paragraphs
  +0.0 pp, paragraphs with any finding +0.4 pp (2.1 % → 2.5 %). It also costs
  time: on a 4-vCPU server a 1,000-word article takes 4.3–4.7 s with it and
  1.7 s without it ([SEMANTIC.md](SEMANTIC.md) §12). Each check embeds at most
  1,024 tokens; a longer text gets a partial semantic pass, and the result says
  so (`complete: false`) rather than dropping text silently.
* **Verses with identical wording cannot be told apart without a citation.**
  Some verses repeat word for word in Arabic (16:42 = 29:59, 15:29 = 38:72).
  When the author prints the reference, MIZAN checks that verse. When they do
  not, it names whichever twin's approved rendering the words match — a true
  statement about the wording, but possibly not the verse number the author
  meant. Verified on both pairs: cited → the cited verse; bare → the twin.
* **Terminology (Panel 2) is English-only and lexical.** 283 of 304 seed terms
  resolved in الجمهرة; **21 are absent** from the dictionary's listings
  (e.g. الآخرة، الأسماء الحسنى، أركان الإسلام); **22 entries** whose only sense
  is a different word (e.g. «الدين» as *debt*, «الحوض» as *pond*) are flagged
  `wrong_sense` and not used. A paraphrase that carries the meaning without
  the term reads as `MISSING`; Urdu, Hindi, Bengali and Tagalog get
  `NOT_IN_GLOSSARY` rather than a guess. Calibration (`terms` suite): run
  over every verse × the 15 approved English translations, **28.9 %** of term
  findings come out `MISSING` — approved translators legitimately choose
  another word for a term inside a verse, which is why scripture inside ﴿ ﴾
  is skipped by the panel; on the dictionary's own Arabic/English definitions
  (local-only data, not redistributed) 23.3 % are `MISSING`. On real English prose, 97.0 % of findings are
  `APPROVED`. Details: [TERMS.md](TERMS.md).
* **The clarity panel is English-only and conservative** — 81.7 % of real
  English publisher prose is left untouched (mostly creed), and it is hidden
  in the interface by default; see [CLARITY.md](CLARITY.md).
* **Long documents.** Checks are capped at 20,000 characters per field and run
  one at a time with a 90 s timeout.
* **Real-corpus reproducibility depends on the publishers.** The corpus is
  rebuilt from their live pages; on 2026-10-03 all 473 + 560 records
  reproduced byte-for-byte, but an edited page will drop records (reported,
  never silent).
