# Normalization — the frozen comparison form

Every MIZAN verdict is a comparison of two strings after they have been
*normalized*. This page states the rules **exactly as implemented** — not as
intended — so that anyone can predict what the engine will consider equal.

| Where | Function | Used for |
|---|---|---|
| `src/mizan/engine.py` | `normalize(t)` | every attribution score, every language |
| `scripts/build_index.py` | `normalize(t)` | `ayat.text_norm`, stored in the index at build time |
| `src/mizan/arabic.py` | `fold_arabic(t)` | Arabic quotation detection only (on top of `normalize`) |
| `src/mizan/engine.py` | `sim(a, b)` | the similarity the thresholds apply to |

`engine.normalize` and `build_index.normalize` are two copies of the same
function. They are identical today (same regexes, same order; checked by
running both over the corpus), and they must stay identical: `detect.py` reads
the stored `text_norm` and assumes it equals `engine.normalize(text)` for every
row. `make setup` enforces this indirectly — the index fingerprint covers
`text_norm`, so a drifted build-side copy fails setup.

---

## 1. `engine.normalize(t)` — step by step

Applied in this order. Order matters (e.g. footnotes are removed before
punctuation is, so `[12]` disappears rather than leaving `12`).

| # | Rule | Implementation | Why |
|---|---|---|---|
| 0 | Empty input → `""` | `if not t: return ""` | |
| 1 | Unicode **NFKC** | `unicodedata.normalize("NFKC", t)` | Compatibility forms (presentation forms, ligatures such as `ﷲ` → `الله`, `ﷺ` → `صلى الله عليه وسلم`, circled digits) compare as their plain letters. |
| 2 | Drop **footnote markers** | `\[\d+\]` → space | Translator numbering, not text. `\d` is Unicode-aware, so `[১২]` (Bengali digits) is removed too. |
| 3 | Drop a **leading verse number** | `^\s*\d+\s*[.)]\s*` → space | Some editions print `255.` or `2)` at the start of a verse. Only at the very start of the string. |
| 4 | Drop **parenthesized text** | `\([^)]*\)` → space | Translators' interpolations: `Allah (God)`, `(O Muhammad)`. Not nested; the first `)` closes. An unbalanced `(` is left to step 7. **Square brackets are kept** — `[He] who made…` keeps `he`. |
| 5 | Strip **Arabic diacritics** | `[\u0617-\u061A\u064B-\u0652\u0670\u0640]` → nothing | Small high marks (U+0617–061A), the harakat fathatan…sukun (U+064B–0652), superscript alef (U+0670), tatweel (U+0640). |
| 6 | Fold **Arabic letter variants** | `أ إ آ` → `ا`, `ة` → `ه`, `ى` → `ي` | Hamza seats and final forms vary between typists and sources. |
| 7 | Punctuation **and non-letter marks** → space | `[^\w\s]` → space | Quotes, dashes, commas, apostrophes (`Allah’s` → `allah s`). See §3 — this rule also removes combining vowel signs in Indic scripts. |
| 8 | Lowercase, collapse whitespace | `" ".join(t.lower().split())` | |

Examples (real output of the function):

| Input | `normalize(...)` |
|---|---|
| `ﷲ` | `الله` |
| `and the Hereafter.[12]` | `and the hereafter` |
| `255. Allah - there is no deity except Him` | `allah there is no deity except him` |
| `Allah (God) is the Light` | `allah is the light` |
| `Say (O Muhammad: He is` | `say o muhammad he is` |
| `“Allah’s Messenger”` | `allah s messenger` |
| `أَحْمَدُ إِلَى الصَّلَاةِ` | `احمد الي الصلاه` |
| `ٱلْحَمْدُ لِلَّهِ` | `ٱلحمد لله` (alef wasla survives — see §2) |
| `اللہ کی رحمت` (Urdu) | `اللہ کی رحمت` (Urdu letters ہ ی ک untouched) |
| `Sa ngalan ni Allāh` (Tagalog) | `sa ngalan ni allāh` (precomposed `ā` is a letter, kept) |
| `अल्लाह के नाम से` (Hindi) | `अल ल ह क न म स` |
| `পরম করুণাময় আল্লাহর নামে` (Bengali) | `পরম কর ণ ময আল ল হর ন ম` |

## 2. `arabic.fold_arabic(t)` — Arabic detection only

`fold_arabic` first maps five Uthmani ornaments, then calls `normalize`:

| Codepoint | Name | Folded to |
|---|---|---|
| U+0671 `ٱ` | ALEF WASLA | `ا` |
| U+0670 `ٰ` | SUPERSCRIPT ALEF | nothing |
| U+06E5 `ۥ` | SMALL WAW | nothing |
| U+06E6 `ۦ` | SMALL YEH | nothing |
| U+0640 `ـ` | TATWEEL | nothing |

`fold_arabic("ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ")` → `الحمد لله رب العلمين`.
Note `العلمين`, not `العالمين`: the Uthmani long vowel is written as a
superscript alef, and removing a mark cannot restore a letter. That is why the
Arabic n-gram index holds **both** the Uthmani and the simple-clean spelling
of every verse (`arabic.py`, `_build`), rather than "repairing" either — the
Tanzil licence forbids changing the text, and MIZAN never does: normalization
produces a *matching key*, the verse shown to a person is always the stored
original.

## 3. What the rules do to each indexed language (measured)

Over the first 2,000 verses of one edition per language:

| Language | edition | normalized tokens per raw word | vocabulary raw → normalized | single-character tokens |
|---|---|---|---|---|
| English | 1947 | 0.97 | 6,075 → 4,360 | 4% |
| Urdu | 1966 | 0.94 | 20,656 → 14,167 | 0% |
| Tagalog | 1963 | 1.01 | 5,054 → 4,932 | 1% |
| **Hindi** | 1986 | **1.58** | 6,789 → **1,028** | **69%** |
| **Bengali** | 1967 | **2.26** | 36,274 → **4,677** | **70%** |

**Rule 7 removes Devanagari and Bengali vowel signs.** Python's `\w` matches
characters for which `str.isalnum()` is true. Dependent vowel signs (matras,
categories Mc/Mn — e.g. U+093F `ि`, U+09BE `া`), the virama (U+094D, U+09CD),
nukta, anusvara and candrabindu are *not* alphanumeric, so step 7 turns each
into a space. Hindi and Bengali words are split into consonant fragments:
`अल्लाह के नाम से` → `अल ल ह क न म स`.

Consequences, stated plainly:

* The rule is applied identically to both sides of every comparison, so an
  approved Hindi or Bengali text still matches itself (the calibration control
  shows no false alarms for Bengali; Hindi has only one indexed edition, see
  `docs/LIMITS.md`).
* **Two renderings that differ only in vowel signs are indistinguishable.**
  `sim("अल्लाह के नाम से", "अल्लाहु की नामो सी") = 1.0` → `MATCH`. In
  Hindi and Bengali, MIZAN cannot detect a modification that changes only
  vowels.
* Token Jaccard (40% of `sim`) degenerates towards a comparison of consonant
  inventories, and the rare-token tier in `detect.py` has far fewer distinct
  tokens to work with.
* Unrelated verses still separate: the best score a *different* verse's text
  reaches is well below the NEAR threshold in every language (the `margin.*`
  rows of `results/SUMMARY.md`).

Arabic-script combining marks outside step 5's ranges are also removed by
rule 7 (e.g. U+0653 maddah above, U+0654/0655 hamza above/below when they
were not composed by NFKC, U+06D6–06DC Quranic annotation signs). Urdu letters
(`ہ ی ک ے ں`) are letters and are kept as they are; they are *not* folded to
their Arabic counterparts.

## 4. `engine.sim(a, b)`

```python
na, nb = normalize(a), normalize(b)
if not na or not nb: return 0.0
seq = difflib.SequenceMatcher(None, na, nb).ratio()      # character order
jac = |set(na.split()) ∩ set(nb.split())| / |set(na.split()) ∪ set(nb.split())|
sim = 0.6 * seq + 0.4 * jac
```

* `SequenceMatcher` runs with difflib's default `autojunk=True`: in strings of
  200+ characters, characters that make up more than 1% of the second string
  are ignored when finding matching blocks. This is part of the frozen
  definition, not an accident to be "fixed" quietly.
* Thresholds, frozen in `engine.py`: **`MATCH ≥ 0.92`**, **`NEAR ≥ 0.75`**,
  below that **`UNATTRIBUTED`** (referred, never "wrong").
* The partial-quotation rule in `report.py` (a clause of ≥ 8 words may be
  upgraded to `MATCH` when a verbatim clause of an approved rendering scores
  ≥ 0.92 and contains ≥ 95 % of its words) uses this same `sim()` and the same
  `T_MATCH`; it adds no new similarity measure (`docs/ARCHITECTURE.md`).
* Ties are broken by edition order (ascending `book_id`), because
  `engine.attribute` keeps the first maximum. When two indexed book entries
  print identical text for a verse, the lower id is named. `docs/LIMITS.md`
  quantifies how often that happens.

## 5. Why these rules are frozen

Every published number — the 0% calibration false-alarm rate, edition
identity, rejection recall, every real-corpus figure — was measured with these
exact rules over an index built with these exact rules. Change any of them and:

1. **The stored index goes stale.** `ayat.text_norm` and `arabic_ayat.text_norm`
   were computed at build time. A runtime `normalize` that no longer matches
   them makes `detect.py`'s candidate retrieval disagree with the scorer —
   silently.
2. **The thresholds lose their calibration.** 0.92 / 0.75 were chosen against
   the score distribution these rules produce. Keeping a rule (say, square
   brackets) changes every score near the boundary.
3. **The measured numbers no longer describe the system.** Publishing them
   would be publishing a claim about different software.

So a change is a **versioned** event, never an edit:

1. change both copies (`engine.py`, `build_index.py`) together;
2. `make setup` will fail at the fingerprint check — that is intended; rebuild
   with `MIZAN_ALLOW_INDEX_DRIFT=1 bash scripts/setup.sh --force`;
3. re-run `make eval` and replace every number in `results/`, `README.md` and
   `docs/`;
4. update `REFERENCE_INDEX_FINGERPRINT` in `src/mizan/eval.py`.

The obvious candidate for such a change is §3: keeping combining marks
(`[^\w\s\p{M}]`-style behaviour) would restore Hindi and Bengali words. It has
not been made because it would invalidate every Hindi and Bengali number, and
it should be made only together with a re-measurement.

## 6. What is stored, and what is shown

`build_index.clean()` removes the dump's HTML wrapper (tags → space, `&nbsp;`
→ space, `&amp;` → `&`) and collapses whitespace. That cleaned string is
`ayat.text` — the approved text MIZAN shows a person. Nothing else about it is
changed. Normalized forms are used only to compare.
