#!/usr/bin/env python3
"""
MIZAN — Panel 2: terminology lock.

Why this module exists
----------------------
Track 2's success criterion asks whether a solution preserved «المعنى والدلالة
الشرعية ودقة المصطلحات». Panel 1 (engine/detect/report) answers that for
SCRIPTURE: every quotation is attributed to a named approved translation.
This panel answers it for TERMINOLOGY.

The official scientific package names one reference for it — «موسوعة الجمهرة -
مفردات المحتوى الإسلامي islamic-content.com/dictionary» — with the rule
«يقدم على الترجمة التلقائية في المصطلحات الشرعية الحساسة», and gives a
sample glossary whose every row carries a «ضابط الاستخدام» (usage rule): Sharia
must not be reduced to penal law, worship must not be confined to rites,
Tawhid must not read as mere numerical oneness.

So for every shar'i term in the Arabic source this module asks one question:
does the translation use the dictionary's approved equivalent (or an accepted
transliteration of it)? It never rewrites anything. It FLAGS and SUGGESTS,
always naming the source of the suggestion.

The four statuses (output contract — app.py and review.py code against them)
-----------------------------------------------------------------------------
    APPROVED         the translation uses an approved equivalent or an
                     accepted transliteration
    VARIANT          the translation uses a rendering we RECOGNISE but which
                     is not the approved one: a reduction the package's
                     ضابط الاستخدام rules out ("penal law" for الشريعة,
                     "rituals" for العبادة), a spelling the dictionary does
                     not attest ("Koran"), or — with no Arabic — one term
                     spelled several ways in the same text
    MISSING          the term is in the Arabic, and no approved equivalent
                     was found in the translation
    NOT_IN_GLOSSARY  MIZAN holds nothing it can check against: a language
                     other than English, or a dictionary entry with no English
                     equivalent. (Transliterated words outside the glossary
                     are counted, never reported: most are personal names.)

Precedence: a discouraged (reductive) rendering is checked FIRST and wins over
an approved one in the same text — the usage rule is about how the term is
explained, and "Tawhid here simply means numerical oneness" uses the approved
word and reduces it.

Honesty about scope
-------------------
* English is the strong case: the glossary holds الجمهرة's ENGLISH equivalents.
* Latin-script languages (Tagalog, Indonesian, French, ...) get the one check
  that does not depend on the language — is the transliteration there? —
  and otherwise NOT_IN_GLOSSARY, with a link to the dictionary's page in that
  language when the dictionary has one.
* Other scripts (Urdu, Hindi, Bengali, ...) are NOT_IN_GLOSSARY, with that
  same link. We return "cannot check" rather than guess.
* Unvocalised Arabic is ambiguous: «السنة» is the Sunnah and also "the year".
  Terms marked `ambiguous_ar` in the glossary are reported when an approved
  or discouraged rendering is present, but never as MISSING — that would flag
  ordinary words. They are listed in `skipped_ambiguous` instead.
* Quotations inside ﴿ ﴾ are skipped on the Arabic side: scripture is Panel 1's
  job (attribution), not a terminology choice the publisher made.

Deterministic, stdlib only, no network. The glossary is read from
data/glossary/ (built by scripts/fetch_jamhara.py); without it the package's
own ten terms are used and the report says so.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .arabic import fold_arabic

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GLOSSARY_DIR = os.path.join(ROOT, "data", "glossary")
JAMHARA_FILE = "jamhara.jsonl"
# Redistributable form committed to the public repo (no dictionary prose —
# see scripts/export_public_glossary.py). Used when the full local file is
# absent, i.e. on a clean clone that has not re-run fetch_jamhara.py.
JAMHARA_PUBLIC_FILE = "jamhara.public.jsonl"


def _jamhara_path(glossary_dir: str) -> str:
    """The full local glossary when present, else the public export."""
    full = os.path.join(glossary_dir, JAMHARA_FILE)
    return full if os.path.exists(full) else os.path.join(glossary_dir, JAMHARA_PUBLIC_FILE)
PACKAGE_FILE = "package_terms.json"

APPROVED = "APPROVED"
VARIANT = "VARIANT"
MISSING = "MISSING"
NOT_IN_GLOSSARY = "NOT_IN_GLOSSARY"
STATUSES = (APPROVED, VARIANT, MISSING, NOT_IN_GLOSSARY)

MODE_BILINGUAL = "bilingual"
MODE_TRANSLATION_ONLY = "translation_only"

DICTIONARY_NAME = "الجمهرة"

# Languages written in Latin script, where a transliteration such as "Zakat"
# or "Tawhid" is the same string as in English. Only for these does the
# transliteration check mean anything outside English.
LATIN_SCRIPT_LANGS = frozenset({
    "en", "tl", "fil", "id", "ms", "fr", "es", "pt", "it", "de", "nl", "sv",
    "da", "no", "fi", "pl", "cs", "sk", "sl", "hr", "bs", "sq", "ro", "hu",
    "tr", "az", "uz", "sw", "so", "ha", "yo", "ig", "wo", "ff", "rw", "rn",
    "lg", "mg", "ceb", "vi", "et", "lv", "lt", "is", "af", "zu", "ny",
})

# ---------------------------------------------------------------------------
# Fallback: the package's own ten terms, used ONLY when data/glossary/ is
# absent. Same content as data/glossary/package_terms.json; kept here so the
# panel still works in a checkout without the data directory, and says so.
# ---------------------------------------------------------------------------
_FALLBACK_PACKAGE = [
    ("الإسلام", "Islam", "ولا يختزل في معنى ثقافي عام",
     ["Islamic culture", "Muslim culture", "cultural identity", "Muslim identity"]),
    ("التوحيد", "Tawhid / Oneness of God",
     "ولا يختزل في ترجمة قد توحي بمجرد الوحدانية العددية",
     ["numerical oneness", "numerical unity", "unitarianism", "monism"]),
    ("العبادة", "Worship", "ولا تحصر في الشعائر فقط",
     ["rituals", "ritual", "rites", "rite", "ritual practice", "religious rituals"]),
    ("النبوة", "Prophethood", "مع التمييز بينها وبين القيادة الدينية البشرية",
     ["religious leadership", "spiritual leadership", "spiritual leader",
      "religious leader"]),
    ("الوحي", "Revelation", "مع تجنب استعمالات فضفاضة قد توهم الإلهام الشخصي",
     ["inspiration", "personal inspiration", "intuition", "spiritual insight"]),
    ("الشريعة", "Sharia / Islamic law and guidance",
     "ولا يختزل في العقوبات أو القانون الجنائي",
     ["Islamic penal law", "penal law", "penal code", "criminal law",
      "Islamic criminal law", "punishments", "punishment"]),
    ("الحديث", "Hadith", "مع بيان درجة الثبوت عند الاستدلال", []),
    ("السنة", "Sunnah", "ويحدد المقصود بحسب السياق العلمي", []),
    ("الفتوى", "Fatwa", "ولا يساوى بالمعلومة العامة", ["general information"]),
    ("الدعوة", "Da‘wah / Invitation to Islam",
     "ويختار المقابل بحسب السياق والجمهور", []),
]
_FALLBACK_AMBIGUOUS = {"الحديث", "السنة"}

# ---------------------------------------------------------------------------
# Accepted transliterations — MIZAN-curated SPELLINGS, not equivalents.
#
# الجمهرة gives one English equivalent per entry (often a translation such as
# "Monotheism" or "Prayer") and sometimes names its own transliteration in the
# definition ("Tawheed", "Salah"). Publishers write many spellings of the same
# transliteration; the brief requires Tawhid/Tawheed, Salah/Salat, Zakat/Zakah
# to count as APPROVED. `latin_key` already unifies vowel length, doubled
# letters, apostrophes and the ta-marbuta ending (Salah = Salat = Salaah), so
# this table only needs ONE spelling per term plus the genuinely different
# ones (Dhikr/Zikr, Wudu/Wudhu, Ramadan/Ramazan).
#
# Format:  ARABIC[!]: Spelling, Spelling
#   "!" = do not scan these spellings in translation-only mode: they double
#         as ordinary names or words ("Hasan", "Muslim", "Wali"), so without
#         the Arabic beside them a hit means nothing.
# ---------------------------------------------------------------------------
TRANSLITERATIONS = """
الإسلام: Islam
التوحيد: Tawhid, Tauhid
العبادة: Ibadah, Ibadat
النبوة: Nubuwwah
الوحي: Wahy, Wahi
الشريعة: Sharia, Shari'ah
الحديث: Hadith
السنة: Sunnah
الفتوى: Fatwa
الدعوة: Da'wah, Dawa
الصلاة: Salah
الزكاة: Zakat
صيام: Siyam
الصوم: Sawm
الحج: Hajj
الشرك: Shirk
الإيمان: Iman
الإحسان: Ihsan
الجنة: Jannah
النار: Nar
القيامة: Qiyamah
الملائكة: Mala'ikah
الرسول: Rasul
الصحابة: Sahabah
الحلال: Halal
الحرام: Haram
الوضوء: Wudu, Wudhu
الطهارة: Taharah
الجهاد: Jihad
الهجرة: Hijrah
القبلة: Qiblah
الكعبة: Ka'bah
المسجد: Masjid
الإمام: Imam
خطبة الجمعة: Khutbah, Jumu'ah khutbah
التقوى: Taqwa
الصبر: Sabr
الذكر: Dhikr, Zikr
الدعاء: Du'a
البدعة: Bid'ah
الكفر: Kufr
النفاق: Nifaq
العقيدة: Aqidah
الدين!: Din
الملة: Millah
الفطرة: Fitrah
الربوبية: Rububiyyah
توحيد الألوهية: Tawhid al-Uluhiyyah
توحيد الربوبية: Tawhid ar-Rububiyyah
توحيد الأسماء والصفات: Tawhid al-Asma' wa as-Sifat
الغيب: Ghayb
القدر: Qadar
البعث: Ba'th
الميزان!: Mizan
الصراط: Sirat
الشفاعة: Shafa'ah
الحوض: Hawd
البرزخ: Barzakh
عذاب القبر: Adhab al-Qabr
الجن: Jinn
الشيطان: Shaytan, Shaitan
السحر: Sihr
المعجزة: Mu'jizah
الكرامة: Karamah
النبي: Nabi
الأنبياء: Anbiya'
الرسالة: Risalah
القرآن: Qur'an
التوراة: Tawrah, Torah
الإنجيل: Injil
الشهادتان: Shahadatayn
الشهادة!: Shahadah
الطاغوت: Taghut
العبودية: Ubudiyyah
الردة: Riddah
الإلحاد: Ilhad
أهل الكتاب: Ahl al-Kitab
المشرك: Mushrik
الكافر: Kafir
المنافق: Munafiq
المؤمن: Mu'min
المسلم!: Muslim
الأمة: Ummah
السلف: Salaf
أهل السنة والجماعة: Ahl as-Sunnah wal-Jama'ah
الصحابي: Sahabi
التابعي: Tabi'i
العرش: Arsh
الكرسي: Kursi
اللوح المحفوظ: al-Lawh al-Mahfuz
الإسراء: Isra'
المعراج: Mi'raj
الوسيلة: Wasilah
الروح: Ruh
الأذان: Adhan, Azan
الإقامة: Iqamah
الركوع: Ruku'
السجود: Sujud
التشهد: Tashahhud
التكبير: Takbir
تكبيرة الإحرام: Takbirat al-Ihram
التسليم: Taslim
الجمعة: Jumu'ah, Jummah
صلاة الجماعة: Jama'ah
صلاة العيد: Salat al-Eid
صلاة التراويح: Tarawih
صلاة الجنازة: Janazah
صلاة الاستخارة: Istikharah
قيام الليل: Qiyam al-Layl
التهجد: Tahajjud
الوتر: Witr
النافلة: Nafl, Nafilah
التطوع: Tatawwu'
التيمم: Tayammum
الغسل: Ghusl
الجنابة: Janabah
الحيض: Hayd
النجاسة: Najasah
الصدقة: Sadaqah
زكاة الفطر: Zakat al-Fitr
النصاب: Nisab
رمضان: Ramadan, Ramadhan, Ramazan
السحور: Suhur
الإفطار: Iftar
الاعتكاف: I'tikaf
ليلة القدر: Laylat al-Qadr
العمرة: Umrah
الإحرام: Ihram
الطواف: Tawaf
السعي: Sa'i
عرفة: Arafah
المناسك: Manasik
الهدي: Hady
الأضحية: Udhiyah
العيد: Eid
التلبية: Talbiyah
المسجد الحرام: al-Masjid al-Haram
المسجد النبوي: al-Masjid an-Nabawi
المسجد الأقصى: al-Masjid al-Aqsa
النية: Niyyah
البسملة: Basmalah
التسبيح: Tasbih
التهليل: Tahlil
التحميد: Tahmid
الحمد: Hamd
الاستعاذة: Isti'adhah
الاستغفار: Istighfar
التلاوة: Tilawah
الصلاة على النبي: Salawat
الفرض: Fard
الواجب: Wajib
المستحب: Mustahabb
المندوب: Mandub
المكروه: Makruh
المباح: Mubah
الرخصة: Rukhsah
العزيمة: Azimah
الفقه: Fiqh
الفقيه: Faqih
المفتي: Mufti
الاجتهاد: Ijtihad
التقليد: Taqlid
المذهب: Madhhab
الإجماع: Ijma'
القياس: Qiyas
المصلحة: Maslahah
مقاصد الشريعة: Maqasid ash-Shari'ah
الحدود: Hudud
القصاص: Qisas
الدية: Diyah
الكفارة: Kaffarah
النذر: Nadhr
الذكاة: Dhakah
الإخلاص: Ikhlas
التوبة: Tawbah
الخشوع: Khushu'
الحياء: Haya'
الزهد: Zuhd
الورع: Wara'
الرياء: Riya'
الغيبة: Ghibah
النميمة: Namimah
الكبائر: Kaba'ir
الهداية: Hidayah
بر الوالدين: Birr al-Walidayn
صلة الرحم: Silat ar-Rahim
البر: Birr
النكاح: Nikah
الطلاق: Talaq
الخلع: Khul'
المهر: Mahr
العدة: Iddah
الولي!: Wali
الحجاب: Hijab
العورة: Awrah
الوصية: Wasiyyah
الوقف: Waqf
الربا: Riba
الزنا: Zina
الخمر: Khamr
الميسر: Maysir
الحسبة: Hisbah
الداعية: Da'iyah
المؤلفة قلوبهم: Mu'allafat al-Qulub
الوسطية: Wasatiyyah
الغلو: Ghuluw
التكفير: Takfir
الفتنة: Fitnah
التفسير: Tafsir
التأويل: Ta'wil
التجويد: Tajwid
الترتيل: Tartil
أسباب النزول: Asbab an-Nuzul
السورة: Surah
الآية: Ayah
المصحف: Mushaf
الإعجاز: I'jaz
الحديث القدسي: Hadith Qudsi
الحديث الصحيح!: Sahih
الحديث الحسن!: Hasan
الحديث الضعيف: Da'if
الحديث الموضوع: Mawdu'
المتواتر: Mutawatir
السند: Sanad
الإسناد: Isnad
المتن: Matn
السيرة: Sirah
الغزوة: Ghazwah
الأنصار: Ansar
المهاجرون: Muhajirun
أهل البيت: Ahl al-Bayt
الخلفاء الراشدون: Rashidun
"""

# Sub-phrases of the dictionary's OWN title that publishers use on their own:
# الجمهرة titles الصبر "Patience in adversity"; a translation that says
# "patience" is using the head of that title, not a different word. Each entry
# is checked at load time to be a literal sub-phrase of one of the term's
# dictionary titles — anything else is ignored, so this table cannot
# introduce a word the dictionary does not use.
SUBPHRASES = {
    "الصبر": ["Patience"],
    "الصحابة": ["companions"],
    "صلة الرحم": ["ties of kinship"],
}

# Renderings we recognise as referring to a term but which neither الجمهرة nor
# the package attests. Kept tiny on purpose: each line is a claim.
UNATTESTED = {
    "القرآن": ["Koran", "Alcoran"],
    "المسلم": ["Moslem", "Mohammedan"],
    "الإسلام": ["Mohammedanism", "Muhammadanism"],
    "الكعبة": ["Caaba"],
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Stand-ins while engine.normalize() runs (it folds ة -> ه and ى -> ي).
# Both are Arabic letters, so they survive normalize's \w filter untouched.
_TA_MARBUTA_HOLD = "\u06C3"
_ALEF_MAQSURA_HOLD = "\u06D0"


def fold_ar(text: str) -> str:
    """
    Arabic folding for term matching: MIZAN's frozen normaliser, minus two rules.

    1. engine.normalize() deletes parenthesised text, because in verse
       translations "(...)" marks a translator's insertion. In prose a term is
       often written exactly that way — «(التوحيد)» — so the brackets are
       turned into spaces first and the content survives.
    2. It folds ta marbuta into ha (ة -> ه). For matching scripture that is
       harmless; for TERMS it manufactures homographs: «عبادة» (worship)
       becomes «عباده» (His servants), «القبلة» becomes «قبله» (before him).
       Measured on the approved-translation calibration (docs/TERMS.md §6),
       those two collisions alone produced ~800 false MISSINGs. So ة is kept.
       The cost — informal text that writes «الصلاه» for «الصلاة» is not
       matched — is the safe direction for a tool whose output is a flag.

    Alef maqsura (ى) is also kept here, but only so that clitic analysis can
    tell a final ى from the pronoun suffix ي («وترى» "and you see" must not
    lose a "suffix" and become «وتر» "witr"). For COMPARISON it is folded to ي
    (see `_cmp`), because many writers use the two interchangeably at the end
    of a word («النبى» / «النبي»).
    """
    held = (re.sub(r"[()]", " ", text or "")
            .replace("ة", _TA_MARBUTA_HOLD).replace("ى", _ALEF_MAQSURA_HOLD))
    return (fold_arabic(held).replace(_TA_MARBUTA_HOLD, "ة")
            .replace(_ALEF_MAQSURA_HOLD, "ى"))


def _cmp(core: str) -> str:
    """Comparison form of a core: final ى and ي are the same letter here."""
    return core.replace("ى", "ي")


def _ar_core(word: str) -> str:
    """Drop the definite article: «الصلاه» -> «صلاه»."""
    return word[2:] if word.startswith("ال") and len(word) > 3 else word


_PROCLITICS_1 = ("", "و", "ف")
_PROCLITICS_2 = ("", "ب", "ك", "ل")
# Longest first. Pronoun suffixes, then sound plural/dual endings.
_SUFFIXES = ("هما", "كما", "هم", "هن", "كم", "كن", "نا", "ها", "ات", "ون",
             "ين", "ان", "ه", "ي", "ك")
_MIN_STEM = 3   # never strip down to fewer letters: «حجه» must not become «حج»


def ar_candidates(token: str) -> set[str]:
    """
    Every article-free core this folded token could be an inflection of.

    Handles the clitics the brief names (و ف ب ك ل ال, including «لل» where
    the article's alef is elided) and the common pronoun and plural suffixes,
    restoring ta marbuta before a suffix («صلاته» -> «صلاة»).

    Over-generation is safe here — a candidate only matters if it equals a
    glossary core — but two guards keep it from inventing matches: a single
    proclitic is only stripped when 3+ letters or an article remain («كبر» is
    not «ك» + «بر»), and a suffix is only stripped when 3+ letters remain.
    """
    stems = {token}
    for p1 in _PROCLITICS_1:
        for p2 in _PROCLITICS_2:
            pre = p1 + p2
            if not pre or not token.startswith(pre):
                continue
            rest = token[len(pre):]
            if p2 == "ل" and rest.startswith("ل") and len(rest) > 3:
                stems.add("ا" + rest)              # لل + noun = ل + ال + noun
            if rest.startswith("ال") or len(rest) >= _MIN_STEM:
                stems.add(rest)
    out: set[str] = set()
    for st in stems:
        for c in {st, _ar_core(st)}:
            out.add(c)
            for suf in _SUFFIXES:
                if c.endswith(suf) and len(c) - len(suf) >= _MIN_STEM:
                    b = c[:-len(suf)]
                    out.add(b)
                    if b.endswith("ت"):
                        out.add(b[:-1] + "ة")      # صلات|ه -> صلاة
                    if suf == "ات":
                        out.add(b + "ة")           # صدق|ات -> صدقة
    return {_cmp(c) for c in out}


# The Arabic article as transliterations write it, assimilated or not:
# al-Jannah, as-Salah, ash-Shirk, at-Tawhid, an-Nar, ad-Din, az-Zakat.
_ARTICLE_PREFIX = re.compile(
    r"^(?:al|el|ul|an|ar|as|ash|at|ath|ad|adh|az|ah)[-‐‑](?=\w)", re.I)
_APOSTROPHES = re.compile(r"[ʿʾʼʻꜤꜥ'’‘`\-‐‑]")


def latin_key(token: str) -> str:
    """
    Spelling-insensitive key for a Latin-script word.

    Transliterations of one Arabic word vary in exactly these ways, so each is
    folded away: diacritics (ā ḥ ṣ), ʿayn/hamza marks and apostrophes, the
    Arabic article ("as-Salah"), long vowels written double (Tawheed, Wudoo,
    Salaah), doubled consonants (Sunnah, Hajj), au/aw (Tauhid), and the ta
    marbuta ending (Salah = Salat = Sala, Zakah = Zakat). A light English
    plural/-ing/-ed strip lets "prayers" meet "Prayer" and "worshipping" meet
    "Worship". Applied identically to the glossary and to the text, so a
    systematic oddity on one side is the same oddity on the other.
    """
    t = unicodedata.normalize("NFKD", token)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = _ARTICLE_PREFIX.sub("", t)
    t = _APOSTROPHES.sub("", t)
    for a, b in (("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"),
                 ("ou", "u"), ("aa", "a"), ("au", "aw")):
        t = t.replace(a, b)
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    if len(t) > 5 and t.endswith("ing"):
        t = t[:-3]
    elif len(t) > 4 and t.endswith("ed"):
        t = t[:-2]
    t = re.sub(r"(.)\1+", r"\1", t)
    t = re.sub(r"(?<=\w\w)a[ht]$", "a", t)
    return t


_LATIN_TOKEN = re.compile(r"[^\W\d_]+(?:[-‐‑'’‘`ʼ][^\W\d_]+)*")
_LEADING_THE = re.compile(r"^(?:the|a|an)\s+", re.I)
# English articles carry no terminology and differ freely between the
# dictionary ("People of scripture") and publishers ("People of the
# Scripture"), so they are skipped on BOTH sides when matching phrases.
_ARTICLES = frozenset({"the", "a", "an"})


def _form_keys(form: str) -> tuple[str, ...]:
    """Key tuple for a glossary form ("The sharia" -> ("sharia",))."""
    return tuple(latin_key(w) for w in _LATIN_TOKEN.findall(form)
                 if w.lower() not in _ARTICLES and latin_key(w))


def _split_alternatives(text: str) -> list[str]:
    """'Tawhid / Oneness of God' -> ['Tawhid', 'Oneness of God']."""
    parts = re.split(r"\s*/\s*|\s*;\s*|\s*,\s*|\s+or\s+|\s+i\.e\.\s+|[()\[\]]",
                     text or "")
    return [p.strip(" .-") for p in parts if p and p.strip(" .-")]


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

@dataclass
class Term:
    """One shar'i term as the panel uses it: Arabic forms + English forms."""

    term_ar: str
    match_ar: list[tuple[str, ...]]          # folded, article-free word cores
    aliases: set[str]                        # folded keys naming this term
    approved_en: list[str]                   # display: dictionary + package
    translit: list[str] = field(default_factory=list)
    discouraged: list[str] = field(default_factory=list)
    unattested: list[str] = field(default_factory=list)
    rule_ar: str | None = None
    basis: str | None = None
    source_url: str | None = None
    source_url_ar: str | None = None
    word_id: int | None = None
    other_languages: list[str] = field(default_factory=list)
    definition_en: str | None = None
    ambiguous: bool = False
    scan_alone: bool = True
    package: bool = False
    has_dictionary_english: bool = True

    def lang_url(self, lang: str) -> str | None:
        """The dictionary's own page for this entry in `lang`, if it has one."""
        if self.word_id and lang in self.other_languages:
            return f"https://islamic-content.com/dictionary/word/{self.word_id}/{lang}"
        return None


@dataclass
class Glossary:
    terms: list[Term]
    source: str                 # "jamhara+package" | "package" | "fallback"
    _ar_index: dict = field(default_factory=dict, repr=False)
    _en_index: dict = field(default_factory=dict, repr=False)

    def build(self) -> "Glossary":
        ar: dict[str, list] = {}
        en: dict[str, list] = {}
        for t in self.terms:
            for cores in t.match_ar:
                ar.setdefault(cores[0], []).append((t, cores))
            forms = ([(f, "approved") for a in t.approved_en
                      for f in _split_alternatives(a)]
                     + [(f, "translit") for f in t.translit]
                     + [(f, "discouraged") for f in t.discouraged]
                     + [(f, "unattested") for f in t.unattested])
            seen = set()
            for form, kind in forms:
                keys = _form_keys(form)
                if not keys or (keys, kind) in seen:
                    continue
                seen.add((keys, kind))
                en.setdefault(keys[0], []).append((t, keys, kind, form))
        for lst in ar.values():
            lst.sort(key=lambda x: -len(x[1]))
        for lst in en.values():
            lst.sort(key=lambda x: -len(x[1]))
        self._ar_index, self._en_index = ar, en
        return self


def _alias(arabic: str) -> str:
    """Identity key for an Arabic term name: folded, article-free, no spaces."""
    return "".join(_cmp(_ar_core(w)) for w in fold_ar(arabic).split())


def _parse_translits(text: str) -> dict[str, tuple[list[str], bool]]:
    out: dict[str, tuple[list[str], bool]] = {}
    for line in text.strip().splitlines():
        if ":" not in line:
            continue
        ar, forms = line.split(":", 1)
        ar = ar.strip()
        scan_alone = not ar.endswith("!")
        out[_alias(ar.rstrip("!"))] = (
            [f.strip() for f in forms.split(",") if f.strip()], scan_alone)
    return out


def _match_tuple(arabic: str) -> tuple[str, ...]:
    return tuple(_cmp(_ar_core(w)) for w in fold_ar(arabic).split())


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        k = x.lower() if isinstance(x, str) else x
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _signature(glossary_dir: str) -> tuple:
    """(file, mtime) for each data file — None when absent."""
    sig = []
    for path in (os.path.join(glossary_dir, PACKAGE_FILE), _jamhara_path(glossary_dir)):
        sig.append((os.path.basename(path),
                    os.path.getmtime(path) if os.path.exists(path) else None))
    return tuple(sig)


def load_glossary(glossary_dir: str = GLOSSARY_DIR) -> Glossary:
    """
    The glossary in `glossary_dir`, rebuilt only when its files change.

    Keyed on the files' modification times rather than cached for the life of
    the process: a server started before scripts/fetch_jamhara.py has written
    data/glossary/ picks the collected glossary up on its next request, with
    no restart, instead of serving the package-ten fallback forever.
    """
    return _load_glossary(glossary_dir, _signature(glossary_dir))


load_glossary.cache_clear = lambda: _load_glossary.cache_clear()   # type: ignore[attr-defined]


@lru_cache(maxsize=8)
def _load_glossary(glossary_dir: str, _sig: tuple) -> Glossary:
    """
    Build the glossary from disk. `_sig` only participates in the cache key.

    Records from jamhara.jsonl that share a match form (several dictionary
    senses of «السنة», «الذكر», ...) merge into ONE term whose approved list is
    the union of their English equivalents, so any sense's equivalent counts
    as approved. The package's ten terms merge into the matching dictionary
    term and contribute their own English column, usage rule and the narrow
    discouraged list.
    """
    pkg_path = os.path.join(glossary_dir, PACKAGE_FILE)
    jam_path = _jamhara_path(glossary_dir)

    package: list[dict] = []
    if os.path.exists(pkg_path):
        with open(pkg_path, encoding="utf-8") as f:
            for p in json.load(f)["terms"]:
                package.append({
                    "term_ar": p["term_ar"],
                    "approved_en": p.get("approved_en") or _split_alternatives(
                        p["approved_en_raw"]),
                    "rule_ar": p.get("usage_rule_ar"),
                    "basis": p.get("basis") or p.get("usage_rule_ar"),
                    "discouraged": p.get("discouraged_en", []),
                })
    else:
        for ar, en, basis, disc in _FALLBACK_PACKAGE:
            package.append({"term_ar": ar, "approved_en": _split_alternatives(en),
                            "rule_ar": basis, "basis": basis, "discouraged": disc})

    terms: dict[tuple, Term] = {}
    source = "fallback" if not os.path.exists(pkg_path) else "package"
    if os.path.exists(jam_path):
        source = "jamhara+package"
        with open(jam_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        records.sort(key=lambda r: (r.get("sense_rank", 0)))
        for r in records:
            review = (r.get("review") or {}).get("flag")
            if review == "wrong_sense":
                continue          # a different word: its English is not ours
            forms = r.get("match_ar") or [r["term_ar"]]
            key = _match_tuple(forms[0])
            t = terms.get(key)
            en = r.get("approved_en") or []
            if review == "suspect_title" and en:
                # Keep the dictionary's transliteration, drop the title the
                # entry's own definition contradicts. Never substitute one.
                en = en[1:]
            if t is None:
                t = Term(
                    term_ar=r["term_ar"] if len(forms) == 1 and fold_ar(
                        forms[0]) == fold_ar(r["term_ar"]) else forms[0],
                    match_ar=[_match_tuple(m) for m in forms],
                    aliases={_alias(x) for x in
                             forms + [r["term_ar"], r.get("seed_ar", "")] if x},
                    approved_en=list(en),
                    translit=list(en if review == "suspect_title" else en[1:]),
                    source_url=r.get("source_url") if en else None,
                    source_url_ar=r.get("source_url_ar"),
                    word_id=r.get("word_id"),
                    other_languages=list(r.get("other_languages", [])),
                    definition_en=r.get("definition_en"),
                    ambiguous=bool(r.get("ambiguous_ar")),
                    has_dictionary_english=bool(en),
                )
                terms[key] = t
            else:
                t.approved_en = _dedupe(t.approved_en + list(en))
                t.translit = _dedupe(t.translit + list(
                    en if review == "suspect_title" else en[1:]))
                if en and not t.has_dictionary_english:
                    t.has_dictionary_english = True
                    t.source_url = r.get("source_url")
                    t.word_id = r.get("word_id")
                    t.other_languages = list(r.get("other_languages", []))
                    t.definition_en = t.definition_en or r.get("definition_en")

    for p in package:
        key = _match_tuple(p["term_ar"])
        t = terms.get(key)
        if t is None:
            t = Term(term_ar=p["term_ar"], match_ar=[key],
                     aliases={_alias(p["term_ar"])},
                     approved_en=[], has_dictionary_english=False,
                     ambiguous=fold_ar(p["term_ar"]) in {
                         fold_ar(x) for x in _FALLBACK_AMBIGUOUS})
            terms[key] = t
        t.package = True
        t.approved_en = _dedupe(t.approved_en + list(p["approved_en"]))
        t.rule_ar, t.basis = p["rule_ar"], p["basis"]
        t.discouraged = _dedupe(t.discouraged + list(p["discouraged"]))

    translits = _parse_translits(TRANSLITERATIONS)
    unattested = {_alias(k): v for k, v in UNATTESTED.items()}
    subphrases = {_alias(k): v for k, v in SUBPHRASES.items()}
    for t in terms.values():
        for alias in t.aliases & subphrases.keys():
            titles = [_form_keys(x) for x in t.approved_en]
            for sub in subphrases[alias]:
                k = _form_keys(sub)
                if any(k == ti[i:i + len(k)] for ti in titles
                       for i in range(len(ti) - len(k) + 1)):
                    t.approved_en = _dedupe(t.approved_en + [sub])
        for alias in t.aliases:
            if alias in translits:
                forms, scan_alone = translits[alias]
                t.translit = _dedupe(t.translit + forms)
                t.scan_alone = t.scan_alone and scan_alone
            if alias in unattested:
                t.unattested = _dedupe(t.unattested + unattested[alias])

    return Glossary(terms=list(terms.values()), source=source).build()


def clarity_entries(glossary: Glossary | None = None) -> list[dict]:
    """
    The surface forms Panel 3 (clarity.py) must freeze, with gloss material.

    Only TRANSLITERATIONS are returned — the curated spellings, the ones
    الجمهرة names in its own English text, and dictionary titles that are
    themselves transliterations ("Zakat", "The qiblah"). Plain English
    equivalents are deliberately left out: the dictionary renders النار as
    "Fire" and الإيمان as "Faith", and freezing or glossing "the fire spread
    through the market" as a shar'i term would be wrong. Terms whose
    transliteration doubles as an ordinary word or name ("!" in
    TRANSLITERATIONS: Muslim, Hasan, Wali) are left out for the same reason.
    """
    g = glossary or load_glossary()
    out = []
    for t in g.terms:
        if not t.scan_alone:
            continue
        tl_keys = {_form_keys(x) for x in t.translit}
        surfaces = list(t.translit)
        for a in t.approved_en:
            for alt in _split_alternatives(a):
                alt = _LEADING_THE.sub("", alt)
                if _form_keys(alt) in tl_keys:
                    surfaces.append(alt)
        surfaces = [x for x in _dedupe(surfaces) if len(x) >= 3]
        if surfaces:
            out.append({"arabic": t.term_ar, "surfaces": surfaces,
                        "approved": list(t.approved_en), "package": t.package,
                        "rule_ar": t.rule_ar, "definition_en": t.definition_en,
                        "source_url": t.source_url or t.source_url_ar})
    return out


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

_QURAN_BRACKETS = re.compile(r"﴿[^﴾]*﴾", re.S)

# Honorific formulas are not terminology. «عليه الصلاة والسلام» after a
# prophet's name, «والصلاة والسلام على رسول الله» in an opening — the word
# «الصلاة» there is a blessing formula that publishers render "peace be upon
# him", and treating it as the term «الصلاة» flagged almost every Arabic text
# that names a prophet. Matched on the FOLDED text.
_HONORIFICS = re.compile(
    r"(?:\bو?الصلاة\s+و\s*السلام\b|\bصلاة\s+الله\s+وسلامه\b|"
    r"\bصلوات\s+الله\s+وسلامه\b)")


def find_arabic_terms(arabic: str, glossary: Glossary) -> list[tuple[Term, int]]:
    """
    Glossary terms present in the Arabic, in order of first appearance, with
    their occurrence counts. Longest match wins, so «المسجد الحرام» is one term
    and does not also report «الحرام» ("forbidden").
    """
    text = _QURAN_BRACKETS.sub(" ", arabic or "")
    tokens = _HONORIFICS.sub(" ", fold_ar(text)).split()
    cands = [ar_candidates(tok) for tok in tokens]
    found: dict[int, list] = {}
    order: list[Term] = []
    i = 0
    while i < len(tokens):
        best = None
        # Sorted, not set order: Python randomises str hashing per process, and
        # a tie between two terms at one position must not depend on it.
        # Longest (least-stripped) candidate first, so a whole-word reading
        # beats one that needed a clitic or suffix removed.
        for c in sorted(cands[i], key=lambda x: (-len(x), x)):
            for term, cores in glossary._ar_index.get(c, ()):
                n = len(cores)
                if best and n <= best[1]:
                    continue
                if i + n <= len(tokens) and all(
                        cores[k] in cands[i + k] for k in range(1, n)):
                    best = (term, n)
        if best:
            term, n = best
            if id(term) not in found:
                found[id(term)] = [term, 0]
                order.append(term)
            found[id(term)][1] += 1
            i += n
        else:
            i += 1
    return [(t, found[id(t)][1]) for t in order]


@dataclass
class _Hit:
    term: Term
    kind: str          # approved | translit | discouraged | unattested
    surface: str       # exactly as written in the translation
    keys: tuple
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.surface)


def _spelling(surface: str) -> str:
    """
    Identity of a SPELLING for consistency checks: case and apostrophe style
    ignored ("Qur'an" vs "Qur’an" is typography, not inconsistency), English
    possessive and the Arabic article ("al-", "as-") dropped. Diacritics are kept: "Qur’ān" vs "Quran" is a real
    difference of convention within one text.
    """
    s = re.sub(r"['’‘`ʼ]s$", "", surface.strip())
    s = _ARTICLE_PREFIX.sub("", s)          # "al-fitrah" is "fitrah" + article
    return re.sub(r"['’‘`ʼ]", "'", s).lower()


def scan_translation(translation: str, glossary: Glossary) -> list[_Hit]:
    """
    Every glossary form present in the translation, per term, per position.

    Hits of DIFFERENT terms may overlap: "congregational prayer" is the
    approved rendering of «صلاة الجماعة» and also contains "prayer", the
    approved rendering of «الصلاة». When both terms are in the Arabic, both
    renderings are there. (Consistency mode, which has no Arabic to say which
    term was meant, drops contained hits — see `_outermost`.) Within one term
    and kind, only the longest form at a position is kept.
    """
    toks = [(m.group(0), latin_key(m.group(0)), m.start(), m.end())
            for m in _LATIN_TOKEN.finditer(translation or "")
            if m.group(0).lower() not in _ARTICLES]
    hits: list[_Hit] = []
    for i in range(len(toks)):
        best: dict[tuple[int, str], tuple] = {}
        for term, keys, kind, _ in glossary._en_index.get(toks[i][1], ()):
            n = len(keys)
            if i + n > len(toks):
                continue
            if all(toks[i + k][1] == keys[k] for k in range(n)):
                slot = (id(term), kind)
                if slot not in best or n > len(best[slot][2]):
                    best[slot] = (term, kind, keys)
        for term, kind, keys in best.values():
            n = len(keys)
            surface = translation[toks[i][2]:toks[i + n - 1][3]]
            hits.append(_Hit(term, kind, surface, keys, toks[i][2]))
    return hits


def _outermost(hits: list[_Hit]) -> list[_Hit]:
    """Drop hits strictly contained in a longer hit ("Haram" in
    "al-Masjid al-Haram"; "penal law" in "Islamic penal law")."""
    spans = {(h.start, h.end) for h in hits}
    return [h for h in hits
            if not any(a <= h.start and h.end <= b and (b - a) > (h.end - h.start)
                       for a, b in spans)]


# A transliteration-shaped word: carries a macron/dot-below letter or an
# ʿayn/hamza mark, or an apostrophe BETWEEN letters that is not an English
# contraction or possessive ("Qur'an", "Mas’ud" yes; "won't", "God's" no).
_TRANSLIT_MARKS = re.compile(r"[āīūḥṣḍṭẓʿʾĀĪŪḤṢḌṬẒ]")
_INNER_APOS = re.compile(r"[^\W\d_]['’‘`ʼ]([^\W\d_]+)$")
_CONTRACTION_TAILS = {"s", "t", "re", "ve", "ll", "d", "m"}


def _looks_transliterated(word: str) -> bool:
    if _TRANSLIT_MARKS.search(word):
        return True
    m = _INNER_APOS.search(word)
    return bool(m) and m.group(1).lower() not in _CONTRACTION_TAILS


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """
    One term-level verdict. as_dict() is the frozen output contract:

        term_ar      str          the Arabic term of a glossary entry. A
                                  finding always names a glossary term:
                                  words outside the glossary are never
                                  reported (only counted, in the report's
                                  `unchecked_transliterations`)
        approved_en  list[str] | None
                                  approved English renderings, dictionary
                                  first, then package, then accepted
                                  transliterations; None when the dictionary
                                  offers no English for the entry
        status       APPROVED | VARIANT | MISSING | NOT_IN_GLOSSARY
        found        str | None   the matched glossary form quoted VERBATIM
                                  from the translation — only the words of
                                  the matched term, never surrounding prose;
                                  None for MISSING
        suggestion   str          one human-readable sentence for the editor
                                  (English, Arabic quoted in «»). It is a
                                  NOTE, not a replacement string: the terms
                                  to use are in `approved_en`, and nothing is
                                  ever substituted into the text
        source_url   str | None   the dictionary page the approved rendering
                                  comes from (the /en page; the /<lang> page
                                  for other languages when it exists)
    """

    term_ar: str | None
    approved_en: list[str] | None
    status: str
    found: str | None
    suggestion: str
    source_url: str | None

    def as_dict(self) -> dict:
        return {"term_ar": self.term_ar, "approved_en": self.approved_en,
                "status": self.status, "found": self.found,
                "suggestion": self.suggestion, "source_url": self.source_url}


@dataclass
class TermsReport:
    findings: list[Finding]
    mode: str
    lang: str
    glossary_source: str
    glossary_terms: int
    skipped_ambiguous: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Transliteration-shaped words outside the glossary (consistency mode).
    # A count only: the words themselves are usually personal names.
    unchecked_transliterations: int = 0

    @property
    def counts(self) -> dict:
        """All four statuses, zeros included, plus "total" (= len(findings))."""
        c = {s: 0 for s in STATUSES}
        for f in self.findings:
            c[f.status] += 1
        c["total"] = len(self.findings)
        return c

    def as_dict(self) -> dict:
        """
        {"findings": [...], "counts": {...}} is the contract other panels code
        against; the remaining keys are additive context for the UI.
        """
        return {
            "findings": [f.as_dict() for f in self.findings],
            "counts": self.counts,
            "mode": self.mode,
            "lang": self.lang,
            "glossary": {"source": self.glossary_source,
                         "terms": self.glossary_terms,
                         "reference": f"{DICTIONARY_NAME} — islamic-content.com/dictionary"},
            "skipped_ambiguous": self.skipped_ambiguous,
            "notes": self.notes,
            "unchecked_transliterations": self.unchecked_transliterations,
        }


def _approved_display(t: Term, limit: int = 6) -> list[str] | None:
    shown = _dedupe(t.approved_en + t.translit[:2])[:limit]
    return shown or None


def _rule_text(t: Term) -> str:
    if not t.package or not t.basis:
        return ""
    return f" Package rule (ضابط الاستخدام): «{t.basis}»."


def _fmt(forms: list[str] | None) -> str:
    return ", ".join(f"“{x}”" for x in (forms or [])) or "—"


def _reduction(t: Term, bad: _Hit, approved: list[str] | None,
               also_good: _Hit | None, may: bool = False) -> Finding:
    """VARIANT for a reductive rendering the package's usage rule rules out."""
    verb = "may reduce" if may else "reduces"
    also = ""
    if also_good is not None:
        also = (f" The text also uses the approved “{also_good.surface}”; if "
                f"“{bad.surface}” is there to deny the reduction (as in “not "
                f"merely {bad.surface}”), no change is needed.")
    return Finding(t.term_ar, approved, VARIANT, bad.surface,
                   f"“{bad.surface}” {verb} «{t.term_ar}».{_rule_text(t)} "
                   f"Approved: {_fmt(approved)}.{also} Suggestion only — the "
                   f"wording is the editor's decision.",
                   t.source_url or t.source_url_ar)


def _bilingual_en(t: Term, hits: list[_Hit], present: list[Term]) -> Finding | None:
    mine = [h for h in hits if h.term is t]
    good = [h for h in mine if h.kind in ("approved", "translit")]
    approved = _approved_display(t)

    # Discouraged (reductive) renderings are checked FIRST and win. The
    # package's usage rule is about how the term is explained, so an approved
    # word in the same text does not cancel a reduction: "Tawhid here simply
    # means numerical oneness" uses the approved term AND reduces it. (The
    # first version returned APPROVED as soon as any approved form was
    # present, so exactly that sentence passed.) When both are present the
    # suggestion says so, because "not merely penal law" also contains the
    # discouraged words; the editor decides.
    #
    # A discouraged phrase that is itself the approved rendering of ANOTHER
    # term in the same Arabic («المناسك» -> "rites") is that term's
    # translation, not a reduction of this one.
    present_ids = {id(p) for p in present}
    others = {h.keys for h in hits
              if h.term is not t and id(h.term) in present_ids
              and h.kind in ("approved", "translit")}
    bad = [h for h in mine if h.kind == "discouraged" and h.keys not in others]
    if bad:
        return _reduction(t, bad[0], approved, good[0] if good else None)

    if good:
        spellings = _dedupe([h.surface for h in good if h.kind == "translit"])
        note = ""
        if len({_spelling(s) for s in spellings}) > 1:
            note = f" Spelled {len(spellings)} ways here ({_fmt(spellings)}); keep one."
        return Finding(t.term_ar, approved, APPROVED, good[0].surface,
                       f"Uses “{good[0].surface}”, an approved rendering of "
                       f"«{t.term_ar}».{note}", t.source_url or t.source_url_ar)
    odd = [h for h in mine if h.kind == "unattested"]
    if odd:
        return Finding(t.term_ar, approved, VARIANT, odd[0].surface,
                       f"“{odd[0].surface}” is not a rendering {DICTIONARY_NAME} "
                       f"or the package attests for «{t.term_ar}». Approved: "
                       f"{_fmt(approved)}.", t.source_url or t.source_url_ar)
    if t.ambiguous:
        return None
    if not t.approved_en:
        # Neither the dictionary (after review) nor the package gives an
        # English equivalent. A curated transliteration would be ACCEPTED if
        # used, but it is a spelling, not an approved equivalent, so it is
        # never presented as one.
        accepted = (f" A transliteration such as {_fmt(t.translit[:2])} is "
                    f"accepted if used." if t.translit else "")
        return Finding(t.term_ar, None, NOT_IN_GLOSSARY, None,
                       f"{DICTIONARY_NAME} offers no English equivalent for "
                       f"«{t.term_ar}» in this sense; MIZAN does not supply one."
                       f"{accepted}", t.source_url_ar)
    return Finding(t.term_ar, approved, MISSING, None,
                   f"«{t.term_ar}» is in the Arabic, but no approved equivalent "
                   f"was found in the translation. Approved: {_fmt(approved)}."
                   f"{_rule_text(t)} Flag for the editor; nothing was changed.",
                   t.source_url or t.source_url_ar)


def _bilingual_other(t: Term, hits: list[_Hit], lang: str,
                     latin: bool) -> Finding | None:
    mine = [h for h in hits if h.term is t and h.kind == "translit"]
    approved = _approved_display(t)
    url = t.lang_url(lang)
    where = (f" {DICTIONARY_NAME} has a {lang} entry: {url}" if url else
             f" {DICTIONARY_NAME} lists no {lang} entry for it.")
    if latin and mine:
        return Finding(t.term_ar, approved, APPROVED, mine[0].surface,
                       f"Keeps the transliteration “{mine[0].surface}” for "
                       f"«{t.term_ar}». MIZAN holds no approved {lang} equivalent "
                       f"beyond that.{where}", url or t.source_url)
    if t.ambiguous:
        return None
    return Finding(t.term_ar, approved, NOT_IN_GLOSSARY, None,
                   f"MIZAN holds approved equivalents in English only and cannot "
                   f"check {lang} wording for «{t.term_ar}».{where}",
                   url or t.source_url or t.source_url_ar)


_SENTENCE_END = re.compile(r"(?<=[.!?؟])\s+")


def _sentence_index(text: str):
    """pos -> index of the sentence it falls in (a light split, for locality)."""
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(text)]

    def which(pos: int) -> int:
        lo = 0
        for i, s in enumerate(starts):
            if s <= pos:
                lo = i
            else:
                break
        return lo
    return which


def _translation_only(translation: str, hits: list[_Hit]) -> tuple[list[Finding], int]:
    """
    No Arabic: the only honest check is internal consistency. One term spelled
    several ways in one text, or a rendering the dictionary does not attest,
    is a VARIANT; a single consistent transliteration is APPROVED. Plain
    English words ("prayer") are not reported — without the source there is
    no telling which Arabic term they render.

    A discouraged rendering IS reported when it sits in the same sentence as
    the term's own transliteration ("Tawhid here simply means numerical
    oneness"): the text names the term and explains it with a reduction the
    package's usage rule rules out. It wins over the other checks.

    Returns (findings, n) where n counts transliteration-shaped words outside
    the glossary. They are counted, never listed: they are usually personal
    names, and the report is stored.
    """
    by_term: dict[int, list[_Hit]] = {}
    order: list[Term] = []
    hits = _outermost(hits)
    for h in hits:
        if h.kind not in ("translit", "unattested") or not h.term.scan_alone:
            continue
        if id(h.term) not in by_term:
            by_term[id(h.term)] = []
            order.append(h.term)
        by_term[id(h.term)].append(h)
    covered = [(h.start, h.end) for h in hits]
    sentence_of = _sentence_index(translation or "")

    out: list[Finding] = []
    for t in order:
        hs = by_term[id(t)]
        approved = _approved_display(t)
        named_in = {sentence_of(h.start): h for h in hs if h.kind == "translit"}
        reduced = [h for h in hits if h.term is t and h.kind == "discouraged"
                   and sentence_of(h.start) in named_in]
        if reduced:
            out.append(_reduction(t, reduced[0], approved,
                                  named_in[sentence_of(reduced[0].start)],
                                  may=True))
            continue
        odd = [h for h in hs if h.kind == "unattested"]
        counts: dict[str, int] = {}
        shown: dict[str, str] = {}
        for h in hs:
            if h.kind == "translit":
                k = _spelling(h.surface)
                shown.setdefault(k, re.sub(r"['’‘`ʼ]s$", "", h.surface))
                counts[k] = counts.get(k, 0) + 1
        distinct = list(shown)
        if odd:
            out.append(Finding(t.term_ar, approved, VARIANT, odd[0].surface,
                               f"“{odd[0].surface}” is not a rendering "
                               f"{DICTIONARY_NAME} or the package attests. "
                               f"Approved: {_fmt(approved)}.",
                               t.source_url or t.source_url_ar))
        elif len(distinct) > 1:
            listing = ", ".join(f"“{shown[k]}” ×{counts[k]}" for k in distinct)
            out.append(Finding(t.term_ar, approved, VARIANT,
                               " / ".join(shown[k] for k in distinct),
                               f"One term, {len(distinct)} spellings in one text: "
                               f"{listing}. Pick one. Approved: {_fmt(approved)}.",
                               t.source_url or t.source_url_ar))
        else:
            out.append(Finding(t.term_ar, approved, APPROVED, hs[0].surface,
                               f"“{hs[0].surface}” is used consistently. Without "
                               f"the Arabic, only consistency can be checked.",
                               t.source_url or t.source_url_ar))

    # Transliteration-shaped words outside the glossary are COUNTED, never
    # listed. The first version reported each one as a NOT_IN_GLOSSARY
    # finding with the word in `found` — mostly personal names ("Mas’ud",
    # "Sa’id"), which app.py then stored with the check. A finding carries a
    # glossary term or nothing.
    seen: set[str] = set()
    for m in _LATIN_TOKEN.finditer(translation or ""):
        w = m.group(0)
        if (any(a <= m.start() < b for a, b in covered)
                or not _looks_transliterated(w)):
            continue
        seen.add(w.lower())
    return out, len(seen)


def check_terms(arabic: str | None, translation: str, lang: str, *,
                glossary: Glossary | None = None) -> TermsReport:
    """
    Panel 2 entry point. Never modifies either text.

    Parameters
    ----------
    arabic:
        The Arabic source, or None/empty when only the translation is
        available (consistency mode).
    translation:
        The translation exactly as published.
    lang:
        ISO 639-1 code of the translation ("en", "ur", "tl", ...).
    glossary:
        Keyword-only, for tests and tools: a `load_glossary(path)` result to
        use instead of data/glossary/.

    Returns
    -------
    TermsReport — `as_dict()` gives {"findings": [...], "counts": {...}, ...}.
    """
    g = glossary or load_glossary()
    lang = (lang or "").lower().split("-")[0].split("_")[0] or "en"
    hits = scan_translation(translation or "", g)
    notes: list[str] = []
    if g.source == "fallback":
        notes.append("data/glossary/ not found: checking the package's ten terms "
                     "only.")

    if arabic and arabic.strip():
        mode = MODE_BILINGUAL
        present = find_arabic_terms(arabic, g)
        present_terms = [t for t, _ in present]
        latin = lang in LATIN_SCRIPT_LANGS
        findings, skipped = [], []
        for t, _n in present:
            if lang == "en":
                f = _bilingual_en(t, hits, present_terms)
            else:
                f = _bilingual_other(t, hits, lang, latin)
            if f is None:
                skipped.append(t.term_ar)
            else:
                findings.append(f)
        if lang != "en":
            notes.append(
                f"lang={lang}: MIZAN holds {DICTIONARY_NAME}'s English equivalents "
                + ("and checks only whether the transliteration is kept."
                   if latin else "and cannot check this script; terms are listed "
                   "as NOT_IN_GLOSSARY with the dictionary's own page where it "
                   "exists."))
        if skipped:
            notes.append("Ambiguous unvocalised forms (e.g. «السنة» = Sunnah or "
                         "'the year') are never reported MISSING.")
    else:
        mode = MODE_TRANSLATION_ONLY
        findings, unchecked = _translation_only(translation or "", hits)
        skipped = []
        notes.append("No Arabic source: only transliteration consistency was "
                     "checked.")
        if unchecked:
            notes.append(
                f"{unchecked} transliterated word{'s' if unchecked != 1 else ''} "
                f"outside the glossary {'were' if unchecked != 1 else 'was'} "
                f"not checked. They are not listed: most are personal names.")

    return TermsReport(findings=findings, mode=mode, lang=lang,
                       glossary_source=g.source, glossary_terms=len(g.terms),
                       skipped_ambiguous=skipped, notes=notes,
                       unchecked_transliterations=(
                           unchecked if mode == MODE_TRANSLATION_ONLY else 0))


# ---------------------------------------------------------------------------
# Measurement — describes the TOOL's behaviour on real text. It is not, and
# must never be quoted as, a measure of how often any publisher misuses terms.
# ---------------------------------------------------------------------------

def _tally(reports, label: str) -> dict:
    status = {s: 0 for s in STATUSES}
    by_term: dict[str, dict[str, int]] = {}
    units_with = 0
    for rep in reports:
        if rep.findings:
            units_with += 1
        for f in rep.findings:
            status[f.status] += 1
            key = f.term_ar or "(transliteration outside glossary)"
            by_term.setdefault(key, {s: 0 for s in STATUSES})[f.status] += 1
    total = sum(status.values())
    return {
        "unit": label, "units": len(reports), "units_with_findings": units_with,
        "findings": total, "status": status,
        "share": {s: round(n / total, 4) if total else 0.0
                  for s, n in status.items()},
        "by_term": dict(sorted(by_term.items(),
                               key=lambda kv: -sum(kv[1].values()))),
    }


def measure_real_corpus(path: str, lang: str = "en") -> dict:
    """
    data/real/corpus.jsonl has no Arabic side, so this is translation-only
    (consistency) mode. Two units: each quoted passage with its surrounding
    context, and each whole article (its distinct context pieces joined),
    because consistency is a property of a document, not of a sentence.
    """
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rows = [r for r in rows if r.get("lang") == lang]
    per_quote, articles = [], {}
    for r in rows:
        pieces = [r.get("context_before", ""), r.get("published_text", ""),
                  r.get("context_after", "")]
        per_quote.append(check_terms(None, " ".join(p for p in pieces if p), lang))
        art = articles.setdefault(r["source_url"], [])
        for p in pieces:
            if p and p not in art:
                art.append(p)
    per_article = [check_terms(None, " ".join(v), lang) for v in articles.values()]
    return {"corpus": os.path.relpath(path, ROOT), "lang": lang,
            "records": len(rows),
            "per_quote": _tally(per_quote, "quoted passage + context"),
            "per_article": _tally(per_article, "article (distinct contexts)")}


def measure_approved_translations(db: str | None = None, lang: str = "en") -> dict:
    """
    Calibration: every verse's Arabic against every APPROVED translation of it
    in MIZAN's index. Approved text is correct by definition, so whatever
    MISSING rate appears here is the false-alarm floor of MISSING on
    scripture: Quran translators choose equivalents for each verse, not from
    a dictionary. This is why check_terms skips text inside ﴿ ﴾. No edition
    is named in the output, by design.
    """
    import sqlite3
    from .engine import DB
    con = sqlite3.connect(db or DB)
    arabic = {(s, a): t for s, a, t in con.execute(
        "SELECT surah, ayah, text_simple FROM arabic_ayat")}
    books = [b for (b,) in con.execute(
        "SELECT book_id FROM translations WHERE language=?", (lang,))]
    g = load_glossary()
    reports = []
    for bid in books:
        for s, a, text in con.execute(
                "SELECT surah, ayah, text FROM ayat WHERE book_id=?", (bid,)):
            ar = arabic.get((s, a))
            if ar and text:
                reports.append(check_terms(ar, text, lang, glossary=g))
    out = _tally(reports, "verse x approved translation")
    out["editions"] = len(books)
    out["by_term"] = dict(list(out["by_term"].items())[:40])
    return out


def measure_parallel_definitions(path: str | None = None) -> dict | None:
    """
    Calibration on approved PROSE: الجمهرة's own Arabic and English
    definitions of the same entry (same section, e.g. «المعنى الاصطلاحي»),
    written by the dictionary's own editors. A MISSING here is a place where
    the dictionary's English prose renders a term in its Arabic prose with a
    word other than that term's own headword — i.e. the floor of MISSING on
    careful, approved, non-scripture text.
    """
    path = path or os.path.join(GLOSSARY_DIR, "parallel_definitions.jsonl")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        pairs = [json.loads(line) for line in f if line.strip()]
    g = load_glossary()
    reports = [check_terms(p["ar"], p["en"], "en", glossary=g) for p in pairs]
    out = _tally(reports, "dictionary entry: Arabic definition x English definition")
    out["by_term"] = dict(list(out["by_term"].items())[:40])
    return out


if __name__ == "__main__":   # pragma: no cover - CLI
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Panel 2 — terminology lock")
    ap.add_argument("--measure", action="store_true",
                    help="measure on data/real/corpus.jsonl and the approved "
                         "translations; write data/glossary/measurement.json")
    ap.add_argument("--arabic", default=None)
    ap.add_argument("--translation", default=None)
    ap.add_argument("--lang", default="en")
    a = ap.parse_args()
    if a.measure:
        g = load_glossary()
        result = {
            "glossary": {"source": g.source, "terms": len(g.terms)},
            "real_corpus": measure_real_corpus(
                os.path.join(ROOT, "data", "real", "corpus.jsonl"), a.lang),
            "dictionary_parallel_definitions_calibration":
                measure_parallel_definitions(),
            "approved_translations_calibration":
                measure_approved_translations(lang=a.lang),
        }
        out = os.path.join(GLOSSARY_DIR, "measurement.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        def _brief(v):
            if not isinstance(v, dict):
                return v
            return {k: _brief(x) for k, x in v.items() if k != "by_term"}
        brief = _brief(result)
        json.dump(brief, sys.stdout, ensure_ascii=False, indent=2)
        print(f"\nfull result -> {os.path.relpath(out, ROOT)}")
    else:
        rep = check_terms(
            a.arabic if a.arabic is not None else
            "التوحيد أساس الدين، والشريعة شاملة، والعبادة لله وحده.",
            a.translation if a.translation is not None else
            "Tawheed is the basis of the religion; the Sharia is "
            "comprehensive, and rituals are for God alone.", a.lang)
        json.dump(rep.as_dict(), sys.stdout, ensure_ascii=False, indent=2)
        print()
