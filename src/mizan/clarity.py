#!/usr/bin/env python3
"""
MIZAN — Panel 3: clarity and audience fit, WITHOUT changing substance.

Why this module exists
----------------------
Track 2's success criterion has two clauses joined by "and at the same time":

    «هل حافظ الحل على المعنى والدلالة الشرعية ودقة المصطلحات،
      وفي الوقت نفسه حسّن وضوح المحتوى وملاءمته للجمهور المستهدف
      دون تغيير جوهره؟»

Clauses one and three (preserve meaning / do not change substance) are what
engine.py + detect.py + report.py already enforce: a quotation is attributed to
a named approved translation by deterministic string comparison, and no model
is ever allowed to author scripture. Clause two — "improved the clarity of the
content and its fit for the target audience" — is this file. Without it Mizan
is a Track 4 provenance tool filed under Track 2.

The governing inversion
-----------------------
Every other "AI content improver" is a BLOCK-LIST: rewrite everything, then try
to catch the cases you should not have touched. In religious text that is the
wrong default — the failure is silent and it is theological, not cosmetic. So
Panel 3 is an ALLOW-LIST: a sentence is passed through UNCHANGED unless it
positively proves it is ordinary narrative or explanatory prose with no
normative force and no scripture inside it.

The consequence is a high refusal rate, and that is the headline, not the
footnote. "Mizan declined to adapt 38% of these sentences, and here is each
reason" is a stronger claim to the scientific package's «الأولوية للامتناع»
(abstention takes priority) than any rewrite quality score could be.

The three hard lines
--------------------
1. SCRIPTURE IS UNTOUCHABLE. Any sentence overlapping a detected quotation, or
   sitting inside quote marks (in that sentence or across several), or
   carrying a verse/hadith reference, or reading like the wording of an
   approved translation in the index (the scripture guard — an undetected
   verse), is QUOTED or refused. It is never segmented for adaptation. With
   no index the guard fails safe and refuses.
2. NO RULINGS, EVER. Mizan does not author, soften, strengthen or rephrase
   normative language. Package level (د) — فتوى أو حالة شخصية — is out of
   scope entirely, and (ب)/(ج) are out of scope for ADAPTATION even though
   they may appear in a document we check. A sentence a condition governs
   ("If..., X, and Y") is refused too: a split would move Y out of the "if".
3. FROZEN TOKENS ARE BYTE-EXACT. Before adaptation every quotation span and
   every approved shar'i term is swapped for an opaque placeholder. After
   adaptation they are restored and the restoration is verified byte-for-byte.
   A single mismatch discards the whole adapted sentence and returns the
   original. That assertion, not the quality of the rewrite, is the product.

What this layer does NOT do
---------------------------
No model. Nothing here calls out, nothing here samples, nothing here is
non-deterministic. The transforms are splitting at safe conjunctions and
FLAGGING (passive voice, long noun chains, glossary-eligible terms, long
sentences) — suggestions addressed to a human editor, not silent rewrites.
`model_adapt` is the single documented seam where the Oct 5 model layer plugs
in, mirroring `detect.semantic_candidates`; it raises NotImplementedError and
the deterministic path is the documented fallback mode.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass, field

# ======================================================================
# Sentence classification
# ======================================================================

QUOTED = "QUOTED"        # overlaps a detected quotation, or is inside quote marks
PROSE = "PROSE"          # the author's own words

# Disposition of a PROSE sentence after the allow-list gate.
ELIGIBLE = "ELIGIBLE"    # passed the gate; clarity work may be suggested
REFUSED = "REFUSED"      # failed the gate; passed through untouched

# ======================================================================
# Refusal reasons — stable machine codes, each with a human sentence.
#
# These codes are part of the output contract: a UI groups by them and the
# evaluation report counts them. Renaming one is a breaking change.
# ======================================================================

R_SCRIPTURE = "SCRIPTURE"
R_REFERENCE = "REFERENCE"
R_NORMATIVE = "NORMATIVE"
R_IMPLICIT_OBLIGATION = "IMPLICIT_OBLIGATION"
R_PREFERENCE = "PREFERENCE"
R_ATTRIBUTION = "ATTRIBUTION"
R_CREED = "CREED"
R_TOO_SHORT = "TOO_SHORT"
R_ROLLBACK = "ROLLBACK"
R_CONDITIONAL = "CONDITIONAL"
R_SCRIPTURE_LIKE = "SCRIPTURE_LIKE"
R_SCRIPTURE_UNCHECKED = "SCRIPTURE_UNCHECKED"
R_LANGUAGE_UNSCREENED = "LANGUAGE_UNSCREENED"

# Languages whose ruling, creed and condition language the lexical gate can
# actually read. The patterns are English (plus a thin Arabic layer that is
# not a translation language here). In any other language the gate cannot
# tell a ruling from narrative, so — allow-list — nothing is adapted and
# nothing is suggested there: a "shorten this sentence" note on a sentence
# we cannot screen is advice MIZAN cannot stand behind.
SCREENED_LANGS = frozenset({"en"})

LANGUAGE_NOTE_EN = (
    "Adaptation is available for English only. For this language MIZAN "
    "cannot screen sentences for rulings, creed or conditions, so it adapts "
    "nothing and raises no suggestions; it reports readability only.")
LANGUAGE_NOTE_AR = (
    "التكييف متاح للإنجليزية فقط. في هذه اللغة لا يستطيع مِيزان فحص الجمل "
    "بحثًا عن صيغ الأحكام والعقائد والشروط، لذلك لا يكيّف أي جملة ولا يقدّم "
    "اقتراحات، ويكتفي بعرض مقاييس الوضوح.")

REFUSAL_MESSAGE = {
    R_SCRIPTURE: (
        "Contains or overlaps scripture. Mizan never rewrites revealed text."
    ),
    R_REFERENCE: (
        "Carries a verse or hadith reference. The sentence frames a citation, "
        "so its wording is part of the attribution."
    ),
    R_NORMATIVE: (
        "Uses normative or ruling language. Mizan does not author, soften or "
        "restate obligations — that is level (د) and out of scope."
    ),
    R_IMPLICIT_OBLIGATION: (
        "States an obligation without a modal ('A Muslim prays five times a "
        "day'). Re-phrasing it would silently shift a ruling into a habit, or "
        "the reverse."
    ),
    R_PREFERENCE: (
        "Makes a preference or superiority claim. Ranking acts of worship is "
        "a juristic judgement, not an editorial one."
    ),
    R_ATTRIBUTION: (
        "Names a scholar, school or source as holding a position. The exact "
        "wording is the attribution and cannot be loosened."
    ),
    R_CREED: (
        "States or touches a point of creed or theology — God, prophethood, "
        "revelation, the unseen, the hereafter, or another faith's doctrine. "
        "Level (ب)/(ج) content is checked, not adapted."
    ),
    R_CONDITIONAL: (
        "A condition (if, when, unless, whoever, provided that, except...) "
        "governs this sentence. Splitting or reordering it could move a clause "
        "out of the condition and change a ruling."
    ),
    R_SCRIPTURE_LIKE: (
        "Shares wording with an approved Quran translation in Mizan's index "
        "(a run of consecutive words, or in English most of an 8-word "
        "window's distinctive vocabulary, or archaic scriptural register) "
        "although it is neither quoted nor cited. It may be an undetected "
        "verse, so it is left exactly as written."
    ),
    R_SCRIPTURE_UNCHECKED: (
        "The scripture index is not available, so Mizan cannot rule out an "
        "unquoted verse in this sentence. It is left untouched (fail safe)."
    ),
    R_LANGUAGE_UNSCREENED: (
        "Mizan screens sentences for rulings, creed and conditions in English "
        "only. In this language (or script) it cannot tell a ruling from "
        "narrative, so the sentence is left untouched and nothing is suggested."
    ),
    R_TOO_SHORT: (
        "Too short to carry a measurable clarity problem. Adapting it would "
        "add risk for no readability gain."
    ),
    R_ROLLBACK: (
        "Adaptation was discarded: the frozen-token check did not restore "
        "byte-for-byte. The original sentence is returned unchanged."
    ),
}

# ======================================================================
# Audience levels — the package's four content tiers (§8 of TECHNICAL_SPEC,
# «مستويات المحتوى» in the official package), read as READER profiles.
#
# A level changes HOW MUCH is flagged and how aggressively long sentences are
# split. It NEVER changes what is claimed, never changes which sentences are
# eligible, and never relaxes a refusal. The allow-list is level-independent by
# construction: `refusal_reasons()` does not take a level argument, so there is
# no code path where picking "curious" makes Mizan willing to touch a ruling.
# ======================================================================

CURIOUS = "curious"            # non-Muslim enquirer; shortest sentences, most flags
NEW_MUSLIM = "new_muslim"      # recent convert; terms flagged for gloss on first use
PRACTISING = "practising"      # knows the vocabulary; only structural problems
DAEE = "daee"                  # the da'i writing the material; lightest touch

LEVELS = (CURIOUS, NEW_MUSLIM, PRACTISING, DAEE)


@dataclass(frozen=True)
class LevelProfile:
    """
    Thresholds for one audience level.

    Deliberately plain numbers in one place: on Oct 5 somebody will want to
    tune these under time pressure, and they should not have to read the
    transforms to do it.
    """

    name: str
    max_words: int            # sentences longer than this are split candidates
    min_split_part: int       # never produce a fragment shorter than this
    flag_passive: bool        # surface passive constructions to the editor
    max_noun_chain: int       # consecutive long content words before flagging
    gloss_terms: bool         # flag shar'i terms that have an approved gloss
    description: str


PROFILES: dict[str, LevelProfile] = {
    CURIOUS: LevelProfile(
        CURIOUS, max_words=18, min_split_part=5, flag_passive=True,
        max_noun_chain=3, gloss_terms=True,
        description="Reader with no prior exposure: shortest sentences, every "
                    "shar'i term flagged for a gloss on first use."),
    NEW_MUSLIM: LevelProfile(
        NEW_MUSLIM, max_words=22, min_split_part=5, flag_passive=True,
        max_noun_chain=3, gloss_terms=True,
        description="Recent convert: knows the shape of the religion, not yet "
                    "the vocabulary. Terms flagged, structure simplified."),
    PRACTISING: LevelProfile(
        PRACTISING, max_words=28, min_split_part=6, flag_passive=True,
        max_noun_chain=4, gloss_terms=False,
        description="Reads this material regularly: vocabulary is assumed, "
                    "only structural clarity problems are raised."),
    DAEE: LevelProfile(
        DAEE, max_words=34, min_split_part=6, flag_passive=False,
        max_noun_chain=5, gloss_terms=False,
        description="The da'i drafting the text: lightest touch, long "
                    "sentences only when they are genuinely run-on."),
}


def profile(level: str) -> LevelProfile:
    """Resolve a level name, failing loudly on a typo rather than defaulting."""
    try:
        return PROFILES[level]
    except KeyError:
        raise ValueError(
            f"unknown audience level {level!r}; expected one of {LEVELS}"
        ) from None


# ======================================================================
# The allow-list gate
#
# Patterns are written against the NORMALISED-FOR-MATCHING form (lowercased,
# whitespace-collapsed) but applied to the original string via re.IGNORECASE so
# that offsets stay usable. Arabic is matched directly; it has no case.
# ======================================================================

# Normative / ruling language. English modals, the vocabulary of the five
# rulings and of sin, plus the Arabic ruling verbs. Deliberately broad: a
# false refusal costs a pass-through, a missed ruling costs the product.
#
# History: the first version listed fixed phrases ("one should", "is
# required") and missed bare "compulsory", "mandatory", "unlawful", "lawful",
# "sin", "may not" and "should" after any subject other than four pronouns.
# "Zakat is compulsory for every adult..." passed the gate and was split.
_NORMATIVE = re.compile(
    r"\b("
    # modals and quasi-modals of obligation and prohibition
    r"must|shall|should|ought|has\s+to|have\s+to|needs?\s+to|"
    r"(?:is|are)\s+to\s+be|may\s+not|may\s+only|"
    # the five rulings and their neighbours, in English
    r"oblig\w*|compulsor\w*|mandat\w*|requir\w*|duty|duties|"
    r"incumbent|binding|enjoin\w*|ordain\w*|prescri(?:be|bed|bes|ption)|"
    r"forbid\w*|forbade|prohibit\w*|impermissib\w*|permissib\w*|"
    r"permit(?:s|ted)?|allow(?:s|ed|able)?|disallow\w*|"
    r"lawful\w*|unlawful\w*|illicit|licit|illegal|"
    r"(?:is|are)\s+(?:strongly\s+)?(?:encouraged|discouraged)|"
    r"disliked|reprehensible|blameworthy|praiseworthy|condemn\w*|"
    r"exempt\w*|"
    # sin and its consequences
    r"sin|sins|sinful\w*|sinned|sinning|sinners?|punish\w*|"
    # transliterated ruling names, with common spellings
    r"haram|haraam|halal|halaal|makruh|makrooh|mustahabb?|mandub|mubah|"
    r"wajib|fard|fardh|farz|sunnah\s+mu'?akkadah|it\s+is\s+(?:a\s+)?sunnah|"
    # validity
    r"it\s+is\s+(?:not\s+)?valid|invalid\w*|void|nullif\w*|annul\w*|"
    r"breaks?\s+(?:the|one's|his|her|their)\s+(?:fast|ablution|wudu|prayer)|"
    # moral verdicts
    r"it\s+is\s+(?:not\s+)?(?:right|wrong|correct|incorrect|acceptable|"
    r"unacceptable|appropriate|inappropriate|proper|improper)\s+(?:to|for)|"
    r"ruling|rulings"
    r")\b"
    # a sentence-initial prohibition: "Do not...", "Never..."
    r"|^\s*(?:do\s+not|don't|never)\b",
    re.IGNORECASE,
)

_NORMATIVE_AR = re.compile(
    r"(يجب|لا\s*يجوز|يجوز|حرام|محرم|واجب|فرض|مكروه|مستحب|مباح|"
    r"ينبغي|لا\s*ينبغي|يحرم|يلزم|على\s*المسلم(?:ة)?\s*أن|إثم|ذنب|معصية)"
)

# Preference / superiority claims. "it is better to", "the best of", "preferable".
_PREFERENCE = re.compile(
    r"\b("
    r"it\s+is\s+better|better\s+to\b|preferable|preferred|recommended|"
    r"the\s+best\b|best\s+of\b|superior\s+to|greater\s+(?:than|reward)|"
    r"more\s+beloved|most\s+beloved|more\s+virtuous|most\s+virtuous|"
    r"takes\s+precedence"
    r")\b",
    re.IGNORECASE,
)

_PREFERENCE_AR = re.compile(r"(أفضل|الأفضل|خير\s*من|أحب\s*إلى|الأولى\s*أن|يُستحسن)")

# A verse or hadith reference printed in the sentence. Reuses the shapes
# detect.printed_references understands, plus hadith collection names, because
# a sentence that FRAMES a citation has wording that is part of the attribution.
_REFERENCE = re.compile(
    r"("
    r"\b(?:qur'?an|quran|surah|surat|sura|chapter)\s*[\s:,]?\s*\d{1,3}\s*[:.]\s*\d{1,3}|"
    r"\[\s*\d{1,3}\s*[:.]\s*\d{1,3}\s*\]|"
    r"\(\s*\d{1,3}\s*[:.]\s*\d{1,3}\s*\)|"
    r"\bq\.?\s*\d{1,3}\s*[:.]\s*\d{1,3}|"
    r"\b(?:narrated|reported)\s+by\b|"
    r"\b(?:sahih\s+)?(?:al-?)?(?:bukhari|muslim|tirmidhi|abu\s+dawud|nasa'?i|"
    r"ibn\s+majah|ahmad|malik|muwatta)\b|"
    r"\bhadith\s+(?:no\.?|number)\s*\d+|"
    r"رواه|أخرجه|متفق\s*عليه|صحيح\s*(?:البخاري|مسلم)|"
    r"\[\s*[^\]]*?\s*:\s*\d{1,3}\s*\]|"
    # A citation framed in words, with no number: "as the Quran states",
    # "God says in the Quran", "the Prophet, may God bless him, said". The
    # words that follow are someone else's, and so is their wording.
    r"\b(?:qur'?an|qur’an|koran|bible|torah|gospels?|injil|scriptures?|"
    r"hadiths?|ahadith|prophet|messenger|allah|god|lord)\b"
    r"(?:\s+(?!(?:says|said|states|stated|tells|told)\b)[\w’']+){0,3}"
    r"(?:\s*\([^)]{0,60}\))?(?:\s*,[^,]{0,90},)?\s+"
    r"(?:also\s+|clearly\s+|himself\s+|itself\s+)?"
    r"(?:says|said|saying|states|stated|tells|told|teaches|taught|mentions|"
    r"mentioned|declares|declared|reports|reported|narrates|narrated|"
    r"records|recorded|describes|described|reminds|addresses|commands|"
    r"commanded|forbids|forbade|promises|promised|warns|warned)\b|"
    r"\b(?:verses?|ayahs?|ayat|hadiths?|ahadith)\b|"
    r"قال\s*(?:تعالى|الله|رسول|النبي)"
    r")",
    re.IGNORECASE,
)

# Naming a scholar or school as the holder of a position. We are conservative:
# the presence of the name plus a position verb, OR a madhhab name at all.
_ATTRIBUTION = re.compile(
    r"("
    r"\b(?:hanafi|maliki|shafi'?i|hanbali|zahiri)\b|"
    r"\bthe\s+(?:four\s+)?(?:schools?|madhhab|madhahib|jurists|scholars|"
    r"fuqaha|ulama|'?ulama'?)\b|"
    r"\b(?:imam|shaykh|sheikh|ibn|al-)\w*\s+\w+\s+(?:held|holds|said|states|"
    r"ruled|argued|is\s+of\s+the\s+(?:view|opinion))|"
    r"\baccording\s+to\s+(?:the\s+)?(?:majority|imam|shaykh|sheikh|scholars)|"
    r"\bscholars\s+(?:differ|differed|agree|agreed|hold|held)|"
    r"\bconsensus\b|\bijma'?\b|"
    r"المذهب|الحنفية|المالكية|الشافعية|الحنابلة|جمهور\s*العلماء|الإجماع"
    r")",
    re.IGNORECASE,
)

# Creed and theology. These are level (ب)/(ج): checkable, not adaptable.
#
# History: the first version held six fixed phrases ("the five pillars",
# "whoever denies"...). "There is no god worthy of worship except Allah
# alone, and Muhammad is His final messenger" and "Jesus was not crucified,
# and he was raised up to Allah" both passed. The allow-list reading of
# "no creed statement" is a TOPIC test, not a phrase test: a sentence that
# speaks about God, prophethood, revelation, the unseen, the hereafter, or
# another faith's doctrine is not ordinary narrative prose, whatever its
# grammar. Broad on purpose; the refusal rate this costs is reported.
_CREED = re.compile(
    r"\b("
    r"the\s+six\s+(?:articles|pillars)\s+of\s+(?:faith|iman)|"
    r"(?:the\s+five\s+)?pillars?\s+of\s+(?:islam|faith|iman)|the\s+five\s+pillars|"
    r"articles?\s+of\s+(?:islamic\s+)?(?:faith|belief)|"
    r"whoever|whosoever|"
    r"is\s+(?:an?\s+)?(?:disbeliever|kafir|kufr|shirk|bid'?ah|innovation)|"
    r"(?:leaves?|left)\s+the\s+fold|out\s+of\s+(?:the\s+fold\s+of\s+)?islam|"
    r"takes\s+one\s+out\s+of\s+islam|"
    # God, His names and His oneness
    r"allah|god|gods|god's|god’s|lord|lords|creator|almighty|divin\w*|"
    r"deity|deities|oneness|monothe\w*|polythe\w*|idol\w*|"
    r"worthy\s+of\s+worship|partners?\s+(?:with|to|unto|in|besides)|"
    r"associat\w*\s+(?:partners|others|anything|anyone|any\s+\w+)\s+with|"
    r"associates?\s+in\s+worship|partners?\s+or\s+associates?|"
    r"no\s+(?:partners?|associates?|equals?|rivals?|consort)|"
    r"creat\w*\s+(?:the\s+)?(?:universe|heavens?|earth|world|mankind|man|"
    r"humans?|human\s+beings|everything|all\s+things)|"
    r"shirk|kufr|kafir\w*|disbelie\w*|unbelie\w*|infidel\w*|"
    r"apostas\w*|apostate\w*|blasphem\w*|testimony\s+of\s+faith|shahadah?|"
    # prophethood and revelation
    r"prophethood|(?:final|last|seal\s+of\s+the)\s+(?:prophet|messenger)s?|"
    r"(?:prophet|messenger|apostle)s?\s+of\s+(?:allah|god|islam)|"
    r"prophets?|messengers?|apostles?|gabriel|jibr[ae]el|jibril|"
    r"revelation\w*|reveal\w*|scriptures?|sent\s+down|"
    r"word\s+of\s+(?:god|allah)|miracle\w*|"
    # the unseen and the hereafter
    r"paradise|heaven\w*|hell|hells|hellfire|jannah|jahannam|hereafter|"
    r"afterlife|after\s+death|resurrect\w*|"
    r"day\s+of\s+(?:judg\w*|resurrection|reckoning|recompense|rising)|"
    r"judg(?:e)?ment\s+day|angel\w*|archangel\w*|jinn|satan\w*|devil\w*|"
    r"shaytan|iblis|unseen|soul|souls|decree\w*|predestin\w*|destiny|qadar|"
    r"reward\w*|recompense|"
    # other faiths' doctrine
    r"crucif\w*|son\s+of\s+god|trinit\w*|triune|begotten|incarnat\w*|"
    r"atonement|original\s+sin|messiah|raised\s+(?:him\s+|them\s+)?up|"
    # "Islam teaches...", "in Islam": a claim about what the religion holds
    r"islam\s+(?:teaches|holds|affirms|maintains|says|requires|forbids|"
    r"permits|considers|regards|specifies|views|sees|lays|stresses)|"
    r"in\s+islam|according\s+to\s+islam|"
    r"islamic\s+(?:doctrine|belief|creed|faith|law|teaching|teachings)"
    r")\b",
    re.IGNORECASE,
)

# Honorific blessing formulas after a prophet's or companion's name are a
# fixed courtesy, not a claim about God. They are blanked out before the
# creed TOPIC test (and only for it), so "Muhammad, may the mercy and
# blessings of God be upon him, is known for his forgiveness" is judged on
# what it says about him. Every other check still sees the full sentence.
_HONORIFIC = re.compile(
    r"\(?\s*(?:may\s+)?(?:the\s+)?(?:peace|mercy|blessings?|salutations?)"
    r"(?:\s*,?\s*(?:and\s+)?(?:the\s+)?(?:peace|mercy|blessings?|salutations?))*"
    r"\s+of\s+(?:god|allah)\s+be\s+(?:up)?on\s+(?:him|her|them|you)\s*\)?|"
    r"\(?\s*(?:may\s+)?(?:god|allah)\s+(?:bless\s+him\s+and\s+grant\s+him\s+"
    r"peace|be\s+pleased\s+with\s+(?:him|her|them))\s*\)?|"
    r"\(?\s*peace\s+be\s+upon\s+(?:him|her|them)\s*\)?",
    re.IGNORECASE,
)

# Capitalised divine pronouns ("depends on Him", "His exclusive right") and
# capitalised names of the unseen ("leads to the Fire", "the Hour", "the
# Garden"). Case matters here, so this one is NOT IGNORECASE: "the fire spread
# through the market" is ordinary prose. A sentence-initial capital is
# ambiguous (a narrator's "He", "Fire broke out") and is not counted.
_DIVINE_PRONOUN = re.compile(
    r"\b(He|Him|His|Himself|Fire|Garden|Gardens|Hour|Hereafter|Throne|"
    r"Unseen)\b")

_CREED_AR = re.compile(
    r"(كفر|شرك|بدعة|من\s*أنكر|من\s*جحد|يخرج\s*من\s*الملة|"
    r"الله|إله|الرب|ربك|ربه|ربهم|الجنة|النار|الآخرة|القيامة|الملائكة|"
    r"الوحي|نبي|رسول|الغيب|القدر)"
)

# Archaic scriptural register. Approved English translations of the Quran
# (Pickthall, Yusuf Ali and others) and of earlier scriptures are written in
# it; modern publisher prose is not. A cheap, index-free scripture cue.
_ARCHAIC = re.compile(
    r"\b(thee|thou|thy|thine|ye|hath|doth|unto|verily|saith|shalt|hast|"
    r"dost|wouldst|couldst|shouldst|art\s+thou|lo)\b",
    re.IGNORECASE,
)

# Governing conditions. A clause inside "If a woman is menstruating, X, and Y"
# belongs to the condition; splitting at ", and" moves Y out of it and turns
# a conditional ruling into an unconditional one. (Measured: the first version
# split exactly that sentence.)
_COND_ALTS = (
    r"if|when|whenever|unless|until|whoever|whosoever|whomever|"
    r"except|provided|providing|as\s+long\s+as|so\s+long\s+as|"
    r"on\s+condition|in\s+case|only\s+if|even\s+if|otherwise|lest"
)
_COND_WORDS = r"(?:" + _COND_ALTS + r")"
_COND_ANY = re.compile(r"\b" + _COND_WORDS + r"\b", re.IGNORECASE)
_COND_INITIAL = re.compile(
    r"^\s*[\"“‘'(\[]*(?:(?:and|but|so|yet|or)\s+)?(?:" + _COND_ALTS
    + r"|once)\b", re.IGNORECASE)
_COND_AFTER_COMMA = re.compile(
    r"[,;:]\s*(?:(?:and|but|or)\s+)?" + _COND_WORDS + r"\b", re.IGNORECASE)
_COND_AR = re.compile(r"(إذا|إلا|ما\s*دام|بشرط|شريطة|كلما|لولا)")
# The only place the deterministic transform ever splits.
_COORDINATION = re.compile(r",\s+(?:and|but|so|yet)\b", re.IGNORECASE)

# Quote marks of every flavour a publisher actually uses, including the Quranic
# ornate brackets ﴿ ﴾ which the Arabic side treats as a quotation marker.
_QUOTE_PAIRS = [
    ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    ("«", "»"), ("﴾", "﴿"), ("‹", "›"),
]
_OPEN_QUOTES = "".join(p[0] for p in _QUOTE_PAIRS)
_CLOSE_QUOTES = "".join(p[1] for p in _QUOTE_PAIRS)

# Implicit obligation: a religious subject + a present-tense habitual verb, with
# no modal in sight. "A Muslim prays five times a day" reads as description but
# functions as a ruling, and rephrasing it ("Muslims often pray...") silently
# downgrades the ruling. This pattern is intentionally broad; a false refusal
# costs nothing but a pass-through, a false acceptance costs the product.
_IMPLICIT_SUBJECT = re.compile(
    r"\b(?:(?:a|an|every|each|the|all|any)\s+)?"
    r"(?:muslim|muslims|believer|believers|worshipper|worshippers|"
    r"muslim\s+woman|muslim\s+man|muslim\s+women|muslim\s+men|one|"
    # Rulings are as often addressed to roles in a family or a rite as to
    # "a Muslim": "Women cover...", "A husband provides...", "Parents teach..."
    r"woman|women|man|men|girl|girls|boy|boys|husband|husbands|wife|wives|"
    r"parent|parents|father|fathers|mother|mothers|child|children|"
    r"son|sons|daughter|daughters|person|people|everyone|"
    r"pilgrim|pilgrims|travell?er|travell?ers|imam|"
    r"adult|adults|guardian|guardians|heir|heirs)\b",
    re.IGNORECASE,
)

_IMPLICIT_VERB = re.compile(
    r"\b("
    # "-es" verbs are spelled out: "washes?" matches "washe"/"washes" but
    # not "wash" (a bug in the first version: "Muslims wash..." passed).
    r"prays?|fasts?|gives?|pays?|performs?|makes?|observes?|recites?|"
    r"faces?|wash|washes|abstains?|refrains?|wears?|covers?|avoids?|"
    r"undertakes?|completes?|repeats?|begins?|ends?|"
    r"lowers?|keeps?|obeys?|provides?|stays?|remains?|marry|marries|"
    r"divorces?|inherits?|offers?|bows?|prostrates?|says?|reads?|"
    r"eats?|drinks?|goes|go|visits?|teach|teaches|learns?|seeks?|greets?|"
    r"follows?|spends?|distributes?|slaughters?|sacrifices?|shaves?|"
    r"trims?|grows?|returns?|leaves?|stands?|sits?|uses?|takes?|"
    r"supports?|maintains?|serves?|honou?rs?|respects?|protects?"
    r")\b",
    re.IGNORECASE,
)


# An apostrophe BETWEEN two letters is part of a word, not a quotation mark:
# "Qur'an", "Da'wah", "Mas‘ud", "God’s". Transliterated shar'i terms are full
# of them, and two in one sentence used to read as an opening and a closing
# quote, refusing ordinary prose as SCRIPTURE.
_INWORD_MARK = re.compile(r"(?<=[^\W\d_])['’‘`ʼ](?=[^\W\d_])")


_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r\f\v]*\n")
_OPEN_CLOSE = {"“": "”", "«": "»", "﴾": "﴿", "﴿": "﴾", '"': '"'}


def quotation_regions(text: str) -> list[tuple[int, int]]:
    """
    Character ranges of the document that sit inside quotation marks, across
    sentence boundaries.

    The per-sentence check (`_has_quote_marks`) needs both marks in one
    sentence, so the middle of a quotation that runs over several sentences
    — `He said: "First sentence. Second sentence. Third."` — had no mark in
    it at all and was treated as the author's prose. A region opens at an
    opening mark and closes at its partner; an unclosed region ends at the
    paragraph break, so one stray mark cannot swallow the rest of a document.
    Single quotes are not used here: they double as apostrophes.
    """
    regions: list[tuple[int, int]] = []
    breaks = [m.start() for m in _PARAGRAPH_BREAK.finditer(text)] + [len(text)]
    i = 0
    while i < len(text):
        ch = text[i]
        close = _OPEN_CLOSE.get(ch)
        if close is None:
            i += 1
            continue
        stop = next(b for b in breaks if b > i)
        j = text.find(close, i + 1, stop)
        end = j + 1 if j != -1 else stop
        regions.append((i, end))
        i = end
    return regions


def _has_quote_marks(sentence: str) -> bool:
    """True if the sentence contains a balanced-looking quoted stretch."""
    sentence = _INWORD_MARK.sub("", sentence)
    for op, cl in _QUOTE_PAIRS:
        if op == cl:
            if sentence.count(op) >= 2:
                return True
        elif op in sentence and cl in sentence:
            return True
    return False


def refusal_reasons(sentence: str, lang: str = "en") -> list[str]:
    """
    Every reason this sentence may not be adapted. Empty list == eligible.

    Two layers, both level-free:
      * the LEXICAL gate (`lexical_reasons`): quote marks, citations, ruling,
        preference, attribution, creed and condition language, implicit
        obligations, archaic scriptural register, length;
      * the SCRIPTURE GUARD (`scripture_reasons`): does the sentence read like
        the wording of an approved translation in MIZAN's index although it
        is neither quoted nor cited? An undetected verse must never reach
        the adapter. With no index the guard fails safe and refuses.

    Deliberately returns ALL reasons rather than short-circuiting on the first.
    The refusal report is a selling point, and "refused for three independent
    reasons" is more convincing evidence of conservatism than "refused".

    Takes no audience level. That is the structural guarantee that no level can
    unlock a sentence the gate rejects. `lang` only chooses which language's
    approved translations the scripture guard compares against.
    """
    reasons = lexical_reasons(sentence)
    for r in language_reasons(sentence, lang) + scripture_reasons(sentence, lang):
        if r not in reasons:
            reasons.append(r)
    return reasons


def _latin_share(text: str) -> float:
    """Share of the letters in `text` that are Latin script (1.0 if none)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    latin = sum(1 for c in letters
                if c < "ɐ" or "Ḁ" <= c <= "ỿ")
    return latin / len(letters)


def language_reasons(sentence: str, lang: str = "en") -> list[str]:
    """
    [R_LANGUAGE_UNSCREENED] when the lexical gate cannot read this sentence:
    the document's language is not in SCREENED_LANGS, or the sentence itself
    is mostly in another script (an Urdu or Arabic sentence inside an English
    document). Otherwise [].
    """
    if not sentence.strip():
        return []
    if _norm_lang(lang) not in SCREENED_LANGS or _latin_share(sentence) < 0.5:
        return [R_LANGUAGE_UNSCREENED]
    return []


def _norm_lang(lang: str | None) -> str:
    return (lang or "en").lower().replace("_", "-").split("-")[0] or "en"


def lexical_reasons(sentence: str) -> list[str]:
    """The pattern-based half of the gate: pure, cheap, no index."""
    reasons: list[str] = []
    stripped = sentence.strip()

    if _has_quote_marks(stripped):
        reasons.append(R_SCRIPTURE)
    if _REFERENCE.search(stripped):
        reasons.append(R_REFERENCE)
    if _NORMATIVE.search(stripped) or _NORMATIVE_AR.search(stripped):
        reasons.append(R_NORMATIVE)
    if _PREFERENCE.search(stripped) or _PREFERENCE_AR.search(stripped):
        reasons.append(R_PREFERENCE)
    if _ATTRIBUTION.search(stripped):
        reasons.append(R_ATTRIBUTION)
    topic = _HONORIFIC.sub(" ", stripped)
    if (_CREED.search(topic) or _CREED_AR.search(stripped)
            or _has_divine_pronoun(topic)):
        reasons.append(R_CREED)
    if _is_implicit_obligation(stripped):
        reasons.append(R_IMPLICIT_OBLIGATION)
    if _is_conditional(stripped):
        reasons.append(R_CONDITIONAL)
    if _ARCHAIC.search(stripped):
        reasons.append(R_SCRIPTURE_LIKE)
    if len(stripped.split()) < 4:
        reasons.append(R_TOO_SHORT)

    return reasons


def _has_divine_pronoun(sentence: str) -> bool:
    """A capitalised divine pronoun or name of the unseen, not the first word."""
    lead = re.match(r"^[\s\"“‘'(\[]*", sentence).end()
    for m in _DIVINE_PRONOUN.finditer(sentence):
        if m.start() == lead and m.group(1) != "Him" and m.group(1) != "Himself":
            continue
        return True
    return False


def _is_conditional(sentence: str) -> bool:
    """
    True when a condition governs clauses that a split or a reorder could
    separate from it. Three shapes, all level-free:

      * the sentence opens with a condition ("If...", "When...", "Whoever...");
      * a condition is introduced after a comma (", if...", ", unless...") —
        a trailing condition after a comma may govern every clause before it;
      * a condition word appears anywhere before a ", and/but/so/yet" — the
        only place the deterministic transform splits.

    A condition that sits inside the LAST clause, with no comma before it
    ("..., and it matters which edition a publisher reaches for when
    preparing material"), governs only that clause and is left alone.
    """
    if _COND_AR.search(sentence):
        return True
    if _COND_INITIAL.search(sentence) or _COND_AFTER_COMMA.search(sentence):
        return True
    coords = [m.start() for m in _COORDINATION.finditer(sentence)]
    if not coords:
        return False
    first_cond = _COND_ANY.search(sentence)
    return bool(first_cond) and first_cond.start() < coords[-1]


def _is_implicit_obligation(sentence: str) -> bool:
    """
    Detect an obligation dressed as a description.

    The signature is: a religious-role subject, followed closely by a habitual
    present-tense act-of-worship verb, with no modal anywhere (a modal would
    already have been caught as R_NORMATIVE, and would make it explicit rather
    than implicit).

    Checked on a WINDOW rather than the whole sentence so that "Muslims in
    Spain built mosques, and travellers pray on the road" does not match on a
    subject from clause one and a verb from clause three. Every subject in the
    sentence is tried, not only the first.
    """
    for subject in _IMPLICIT_SUBJECT.finditer(sentence):
        # Look only in the ~8 words after the subject: an obligation sits next
        # to its subject. Beyond that the pair is almost certainly coincidental.
        tail = sentence[subject.end():]
        window = " ".join(tail.split()[:8])
        if not _IMPLICIT_VERB.search(window):
            continue
        # Past tense narration ("Muslims prayed behind him that day") is
        # history, not a ruling. The verb pattern is present-tense only, but
        # the sentence may still be narrative; require no past-tense auxiliary
        # in the window.
        if re.search(r"\b(?:was|were|had|did|used\s+to)\b", window,
                     re.IGNORECASE):
            continue
        return True
    return False


# ======================================================================
# The scripture guard — an undetected verse must never reach the adapter.
#
# Frozen spans come from Panels 1-2. A verse the detector did not locate (no
# citation, no quote marks, a 20-30 word run lifted from a long verse) is not
# frozen, so without this guard the panel treated it as prose and could insert
# ". And" inside approved wording. (Measured: 11 of 40 such runs embedded in a
# sentence were split even WITH the real detector's findings; 35 of 40 when
# clarity ran on its own.)
#
# The guard asks the detector's own deterministic index up to two questions,
# and refuses if either says yes:
#
#   1. VERBATIM RUN — do K or more consecutive tokens of the sentence (after
#      engine.normalize) appear, in order, in one PLAIN approved rendering of
#      a candidate verse? "Plain" mirrors detect's containment rule: footnotes
#      cut, tafsir-length editions left out, so commentary cannot make prose
#      look like a verse. Candidates are retrieved per window of the sentence.
#   2. LIKENESS (English only) — does any 8-word window read like ONE
#      approved rendering? (`SpanDetector.scripture_likeness`: the IDF-weighted
#      share of the window's vocabulary found in a single rendering of one
#      candidate verse.) It catches short fragments the run test misses.
#
# Calibrated PER LANGUAGE (docs/CLARITY.md §11), on each language's real
# publisher prose in data/real/ and on 200 verse runs per language read from
# the index at run time. Likeness is useless outside English: normalize()
# fragments Devanagari and Bengali at vowel signs into one-letter tokens that
# occur in every verse, and it labelled 50-80% of neutral ur/hi/bn prose as
# scripture (a Hindi sentence about Medina's markets scored 1.0). The run test
# needs a longer K where tokens are fragments. At the values below every run
# hit on real prose, in all five languages, was verse text the publisher had
# printed.
#
# Lexical, stdlib, ~6 ms per sentence after a ~1 s one-time index build per
# language that the server has already paid for.
#
# Fail safe: if the index is not there, every prose sentence is refused with
# R_SCRIPTURE_UNCHECKED. A missing index must never mean "nothing looks like
# scripture".
# ======================================================================

SCRIPTURE_LIKE_WINDOW = 8         # words per likeness window
SCRIPTURE_LIKE_STRIDE = 3         # words between window starts


@dataclass(frozen=True)
class GuardParams:
    """Scripture-guard thresholds for one language."""

    likeness_min: float | None    # None: the likeness test is not used
    run_min: int                  # consecutive normalised tokens
    window: int                   # tokens per candidate-retrieval window
    candidates: int               # candidate verses per retrieval window


GUARD_PARAMS: dict[str, GuardParams] = {
    "en": GuardParams(likeness_min=0.95, run_min=6, window=16, candidates=80),
    "ur": GuardParams(likeness_min=None, run_min=8, window=8, candidates=40),
    "tl": GuardParams(likeness_min=None, run_min=10, window=8, candidates=40),
    "hi": GuardParams(likeness_min=None, run_min=12, window=16, candidates=80),
    "bn": GuardParams(likeness_min=None, run_min=15, window=16, candidates=80),
}
# A language calibrated for nobody: the run test only, with the strictest
# token count measured for a fragmenting script.
_GUARD_DEFAULT = GuardParams(likeness_min=None, run_min=10, window=16,
                             candidates=80)

# English values, kept under their first names for callers and docs.
SCRIPTURE_LIKE_MIN = GUARD_PARAMS["en"].likeness_min
SCRIPTURE_RUN_MIN = GUARD_PARAMS["en"].run_min


def guard_params(lang: str) -> GuardParams:
    return GUARD_PARAMS.get(_norm_lang(lang), _GUARD_DEFAULT)


_GUARD_LOCK = threading.Lock()
_GUARD: dict[str, object] = {}            # lang -> SpanDetector
_GUARD_WHY: dict[str, str] = {}           # lang -> why it is unavailable
_LIKENESS_CACHE: dict[tuple[str, str], tuple[float, int]] = {}
_LIKENESS_CACHE_MAX = 20000
_PLAIN_CACHE: dict[tuple[str, int, int], list] = {}
_PLAIN_CACHE_MAX = 20000
_FOOTNOTE_SPLIT = re.compile(r"_{4,}")


def _guard_detector(lang: str):
    """
    The deterministic SpanDetector for `lang`, or None when it cannot be built.

    Built lazily, once per process and language. A missing index file is
    re-checked on every call (so a server that started before `make setup`
    finished picks the index up), and never creates an empty database file.
    """
    lang = _norm_lang(lang)
    det = _GUARD.get(lang)
    if det is not None:
        return det
    with _GUARD_LOCK:
        det = _GUARD.get(lang)
        if det is not None:
            return det
        if lang in _GUARD_WHY and not _GUARD_WHY[lang].startswith("missing"):
            return None
        try:
            from .engine import DB, Mizan
            if not os.path.exists(DB):
                _GUARD_WHY[lang] = f"missing index: {DB}"
                return None
            from .detect import SpanDetector
            det = SpanDetector(Mizan(DB), lang, semantic=False)
        except Exception as e:                          # noqa: BLE001
            _GUARD_WHY[lang] = f"{type(e).__name__}: {e}"
            return None
        _GUARD[lang] = det
        _GUARD_WHY.pop(lang, None)
        return det


def scripture_guard_status(lang: str = "en") -> dict:
    """For /api/health-style reporting: is the guard live for `lang`?"""
    det = _guard_detector(lang)
    gp = guard_params(lang)
    return {"available": det is not None, "lang": _norm_lang(lang),
            "reason": None if det is not None else _GUARD_WHY.get(_norm_lang(lang)),
            "likeness_window": SCRIPTURE_LIKE_WINDOW,
            "likeness_min": gp.likeness_min,
            "verbatim_run_min": gp.run_min}


def _windows(items: list, size: int, stride: int) -> list[list]:
    """Overlapping windows that always include the last `size` items."""
    if len(items) <= size:
        return [items]
    starts = list(range(0, len(items) - size + 1, stride))
    if starts[-1] != len(items) - size:
        starts.append(len(items) - size)
    return [items[i:i + size] for i in starts]


def _plain_token_lists(det, lang: str, surah: int, ayah: int) -> list[tuple]:
    """
    (tokens, Counter) for each of a verse's PLAIN approved renderings,
    normalised: footnotes
    after "____" cut, and renderings longer than detect.PLAIN_LENGTH_FACTOR x
    the verse's median length (tafsir editions) left out — the same rule
    detect uses for containment, so commentary never makes prose look like
    a verse.
    """
    key = (lang, surah, ayah)
    hit = _PLAIN_CACHE.get(key)
    if hit is not None:
        return hit
    from .detect import PLAIN_LENGTH_FACTOR
    from .engine import normalize
    from collections import Counter
    rows = []
    for _book, text in det.by_ref.get((surah, ayah), ()):
        toks = normalize(_FOOTNOTE_SPLIT.split(text or "", maxsplit=1)[0]).split()
        if toks:
            rows.append(toks)
    lens = sorted(len(r) for r in rows) or [0]
    cap = PLAIN_LENGTH_FACTOR * lens[len(lens) // 2]
    out = [(r, Counter(r)) for r in rows if len(r) <= cap]
    if len(_PLAIN_CACHE) >= _PLAIN_CACHE_MAX:
        _PLAIN_CACHE.clear()
    _PLAIN_CACHE[key] = out
    return out


def _verbatim_run(det, lang: str, sentence: str) -> int:
    """
    Longest run of consecutive normalised words the sentence shares, in
    order, with one plain approved rendering of a candidate verse. Candidates
    are retrieved per window, so a verse fragment in a long sentence of prose
    still finds its verse.
    """
    from collections import Counter
    from difflib import SequenceMatcher
    from .engine import normalize
    gp = guard_params(lang)
    toks = normalize(sentence).split()
    if len(toks) < gp.run_min:
        return 0
    cands: set[tuple[int, int]] = set()
    for win in _windows(toks, gp.window, gp.window // 2):
        for s, a, _ in det.lexical_candidates(" ".join(win),
                                              top_k=gp.candidates):
            cands.add((s, a))
    tcount = Counter(toks)
    best = 0
    for s, a in sorted(cands):
        for rend, rcount in _plain_token_lists(det, lang, s, a):
            # A shared run can be no longer than the multiset intersection.
            bound = sum(min(n, rcount[t]) for t, n in tcount.items()
                        if t in rcount)
            if bound <= best:
                continue
            m = SequenceMatcher(None, toks, rend, autojunk=False
                                ).find_longest_match(0, len(toks), 0, len(rend))
            if m.size > best:
                best = m.size
    return best


def scripture_likeness(sentence: str, lang: str = "en"
                       ) -> tuple[float, int] | None:
    """
    (highest window likeness 0.0-1.0, longest verbatim run in normalised
    tokens) for the sentence, or None when the index is unavailable.
    Likeness is 0.0 for languages that do not use it. Deterministic; cached
    per sentence.
    """
    lang = _norm_lang(lang)
    det = _guard_detector(lang)
    if det is None:
        return None
    key = (lang, sentence)
    v = _LIKENESS_CACHE.get(key)
    if v is not None:
        return v
    like = 0.0
    if guard_params(lang).likeness_min is not None:
        words = sentence.split()
        like = max((det.scripture_likeness(" ".join(w))
                    for w in _windows(words, SCRIPTURE_LIKE_WINDOW,
                                      SCRIPTURE_LIKE_STRIDE) if w), default=0.0)
    run = _verbatim_run(det, lang, sentence)
    v = (like, run)
    if len(_LIKENESS_CACHE) >= _LIKENESS_CACHE_MAX:
        _LIKENESS_CACHE.clear()
    _LIKENESS_CACHE[key] = v
    return v


def scripture_reasons(sentence: str, lang: str = "en") -> list[str]:
    """[R_SCRIPTURE_LIKE], [R_SCRIPTURE_UNCHECKED] (no index), or []."""
    if not sentence.strip():
        return []
    v = scripture_likeness(sentence, lang)
    if v is None:
        return [R_SCRIPTURE_UNCHECKED]
    like, run = v
    gp = guard_params(lang)
    if run >= gp.run_min or (gp.likeness_min is not None
                             and like >= gp.likeness_min):
        return [R_SCRIPTURE_LIKE]
    return []


# ======================================================================
# Approved glossary — TERMS WE DO NOT TRANSLATE AWAY.
#
# Two layers:
#   1. The official scientific package's own ten terms (§"نماذج لقاموس
#      المصطلحات الأساسية"), hardcoded below. Each carries the package's usage
#      constraint (ضابط الاستخدام) because the constraint is the reason the
#      term is frozen. "Sharia" is in the list not because it is hard to read
#      but because the package explicitly says it must not be reduced to
#      criminal law. Freezing it and flagging it for a gloss is the correct
#      handling; silently replacing it with "Islamic law" is not.
#   2. When data/glossary/ is present (scripts/fetch_jamhara.py), every
#      approved TRANSLITERATION collected from الجمهرة — the dictionary the
#      package puts ahead of automatic translation — via terms.clarity_entries.
#      Plain-English equivalents ("Fire", "Faith") are not frozen: they have
#      everyday senses. If the data is absent or unreadable, layer 1 alone is
#      used and GLOSSARY_SOURCE says so.
# ======================================================================

@dataclass(frozen=True)
class GlossaryTerm:
    """One approved shar'i term and why its wording is fixed."""

    term: str                  # the form that appears in English text
    arabic: str
    approved_english: str      # the package's approved rendering
    constraint: str            # ضابط الاستخدام — why it may not be loosened
    gloss: str                 # a short plain-language note for new readers


_PACKAGE_GLOSSARY: tuple[GlossaryTerm, ...] = (
    GlossaryTerm(
        "Tawhid", "التوحيد", "Tawhid / Oneness of God",
        "Keep the term and explain it. Do not reduce it to numerical oneness.",
        "God's exclusive right to be worshipped, and to be described as He "
        "described Himself."),
    GlossaryTerm(
        "Sharia", "الشريعة", "Sharia / Islamic law and guidance",
        "Explain by context. Do not reduce it to penal law.",
        "The revealed guidance covering worship, dealings and ethics."),
    GlossaryTerm(
        "Sunnah", "السنة", "Sunnah",
        "The Prophet's way and guidance; the intended sense is fixed by the "
        "scholarly context.",
        "The Prophet's example — what he said, did and approved."),
    GlossaryTerm(
        "Hadith", "الحديث", "Hadith",
        "A report from the Prophet. State its grade when it is used as proof.",
        "A recorded report of the Prophet's words, acts or approvals."),
    GlossaryTerm(
        "Fatwa", "الفتوى", "Fatwa",
        "A ruling issued by a qualified person on a specific case. Not the "
        "same as general information.",
        "A qualified scholar's answer about one specific situation."),
    GlossaryTerm(
        "Dawah", "الدعوة", "Da'wah / Invitation to Islam",
        "Choose the rendering by context and audience.",
        "Inviting people to Islam with wisdom and good character."),
    GlossaryTerm(
        "Islam", "الإسلام", "Islam",
        "Explain by context; do not reduce it to a general cultural meaning.",
        "Submission to God alone, in belief and in practice."),
    GlossaryTerm(
        "worship", "العبادة", "Worship",
        "Covers acts of the heart, speech and limbs — not only rites.",
        "Everything done to draw near to God, inwardly and outwardly."),
    GlossaryTerm(
        "prophethood", "النبوة", "Prophethood",
        "God's selection of prophets by revelation; distinct from human "
        "religious leadership.",
        "God choosing particular people to receive and convey revelation."),
    GlossaryTerm(
        "revelation", "الوحي", "Revelation",
        "What God revealed to His prophets. Avoid loose uses suggesting "
        "personal inspiration.",
        "What God sent down to His prophets."),
)

_JAMHARA_CONSTRAINT = (
    "Approved equivalent from الجمهرة (islamic-content.com/dictionary), which "
    "the package places ahead of automatic translation for sensitive shar'i "
    "terms. Keep the term; add a gloss rather than replacing it.")


def _load_glossary() -> tuple[tuple[GlossaryTerm, ...], str]:
    """
    The package's ten terms, extended with الجمهرة transliterations if present.

    Package entries always come first and always win: a surface form such as
    "Tawheed" that الجمهرة attests for التوحيد is attached to the PACKAGE entry
    for التوحيد, so it is frozen with the package's own constraint and gloss.
    Any failure to read the data degrades to the package ten — never to an
    empty glossary, which would silently stop freezing shar'i terms.
    """
    try:
        from .terms import clarity_entries, fold_ar, load_glossary
        source = load_glossary().source
        entries = clarity_entries()
    except Exception:                                   # noqa: BLE001
        return _PACKAGE_GLOSSARY, "package (hardcoded)"
    if not entries:
        return _PACKAGE_GLOSSARY, "package (hardcoded)"
    label = ("package + الجمهرة (data/glossary)" if source == "jamhara+package"
             else "package + accepted transliterations (data/glossary absent)")

    by_arabic = {fold_ar(t.arabic): t for t in _PACKAGE_GLOSSARY}
    seen = {t.term.lower() for t in _PACKAGE_GLOSSARY}
    extra: list[GlossaryTerm] = []
    for e in entries:
        pkg = by_arabic.get(fold_ar(e["arabic"]))
        for surface in e["surfaces"]:
            if surface.lower() in seen:
                continue
            seen.add(surface.lower())
            if pkg is not None:
                extra.append(GlossaryTerm(surface, pkg.arabic,
                                          pkg.approved_english, pkg.constraint,
                                          pkg.gloss))
                continue
            approved = " / ".join(e["approved"][:3]) or surface
            if e["definition_en"]:
                gloss = e["definition_en"]
            elif e["source_url"]:
                gloss = f"See الجمهرة: {e['source_url']}"
            else:
                gloss = "A shar'i term; see الجمهرة (islamic-content.com)."
            extra.append(GlossaryTerm(surface, e["arabic"], approved,
                                      _JAMHARA_CONSTRAINT, gloss))
    return _PACKAGE_GLOSSARY + tuple(extra), label


GLOSSARY, GLOSSARY_SOURCE = _load_glossary()

# Built once: term -> entry, matched case-insensitively. Longest surface first
# so "Tawhid al-Uluhiyyah" wins over "Tawhid". Boundaries are lookarounds, not
# \b, because transliterations end in marks \b does not see ("Du‘ā’").
_GLOSSARY_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t.term) for t in
                           sorted(GLOSSARY, key=lambda t: -len(t.term)))
    + r")(?!\w)",
    re.IGNORECASE,
)
_GLOSSARY_BY_KEY: dict[str, GlossaryTerm] = {}
_GLOSSARY_BY_FOLD: dict[str, GlossaryTerm] = {}
for _t in GLOSSARY:
    _GLOSSARY_BY_KEY.setdefault(_t.term.lower(), _t)
    _GLOSSARY_BY_FOLD.setdefault(
        unicodedata.normalize("NFKC", _t.term).casefold(), _t)


def _glossary_entry(matched: str) -> GlossaryTerm | None:
    """
    The entry for a regex match. `re.IGNORECASE` and str.lower() do not agree
    on every Unicode letter («İ», «ẞ»), and with hundreds of transliterations
    in the pattern a KeyError here would take down the whole panel.
    """
    return (_GLOSSARY_BY_KEY.get(matched.lower())
            or _GLOSSARY_BY_FOLD.get(
                unicodedata.normalize("NFKC", matched).casefold()))


# ======================================================================
# Frozen tokens — the safety guarantee
# ======================================================================

# Placeholder shape. Chosen so that:
#   * it survives word-splitting as ONE token (no spaces, no punctuation that
#     a splitter would break on),
#   * it cannot occur in natural text (the sentinel is non-linguistic),
#   * it is trivially greppable when something goes wrong at 3am on Oct 5.
_FROZEN_FMT = "␂MZN{idx}␃"
_FROZEN_RE = re.compile("␂MZN(\\d+)␃")


@dataclass
class FrozenSpan:
    """One stretch of text that adaptation may not touch."""

    token: str      # the placeholder that stands in for it
    text: str       # the exact original bytes
    kind: str       # "quotation" | "term"
    note: str = ""  # why it is frozen, for the editor-facing report


class FrozenTokenViolation(Exception):
    """
    Raised when restoration is not byte-exact.

    Never allowed to escape `adapt_sentence`: it is caught there and converted
    into a full rollback plus an R_ROLLBACK refusal. It is an exception rather
    than a boolean so that no future edit can accidentally ignore the return
    value.
    """


def freeze(sentence: str, *, quoted_spans: list[tuple[int, int]] | None = None,
           freeze_terms: bool = True) -> tuple[str, list[FrozenSpan]]:
    """
    Replace untouchable stretches with opaque placeholders.

    Parameters
    ----------
    sentence:
        The sentence, exactly as written.
    quoted_spans:
        Character ranges inside `sentence` known to be scripture. Supplied by
        the segmenter from report findings; frozen before terms so that a term
        inside a quotation is covered by the quotation, not double-wrapped.
    freeze_terms:
        Freeze approved glossary terms too. On by default — the package names
        the Jumhara dictionary as taking precedence over automatic translation
        in sensitive shar'i terms, so the term's exact surface form is as
        untouchable as a verse for our purposes.

    Returns
    -------
    (masked_sentence, spans) where `spans` is in the order the placeholders
    were created. Restoration reverses it.
    """
    spans: list[FrozenSpan] = []
    out = sentence

    # Quotations first, right-to-left, so earlier offsets stay valid.
    for start, end in sorted(quoted_spans or [], key=lambda p: -p[0]):
        if not (0 <= start < end <= len(out)):
            continue
        token = _FROZEN_FMT.format(idx=len(spans))
        spans.append(FrozenSpan(token=token, text=out[start:end],
                                kind="quotation",
                                note="detected quotation — never rewritten"))
        out = out[:start] + token + out[end:]

    if freeze_terms:
        # Collect then substitute right-to-left, same offset reasoning.
        matches = [m for m in _GLOSSARY_RE.finditer(out)]
        for m in reversed(matches):
            entry = _glossary_entry(m.group(1))
            token = _FROZEN_FMT.format(idx=len(spans))
            spans.append(FrozenSpan(
                token=token, text=m.group(0), kind="term",
                note=entry.constraint if entry else "approved shar'i term"))
            out = out[:m.start()] + token + out[m.end():]

    return out, spans


def thaw(masked: str, spans: list[FrozenSpan]) -> str:
    """
    Put every frozen stretch back.

    Raises FrozenTokenViolation if any placeholder went missing, was
    duplicated, or if an unknown placeholder appeared. All three are the same
    class of bug — adaptation touched the mask — and all three must abort.
    """
    by_token = {s.token: s for s in spans}

    seen: dict[str, int] = {}
    for m in _FROZEN_RE.finditer(masked):
        tok = m.group(0)
        if tok not in by_token:
            raise FrozenTokenViolation(f"unknown frozen token {tok!r} in output")
        seen[tok] = seen.get(tok, 0) + 1

    for s in spans:
        n = seen.get(s.token, 0)
        if n == 0:
            raise FrozenTokenViolation(
                f"frozen {s.kind} was dropped by adaptation: {s.text!r}")
        if n > 1:
            raise FrozenTokenViolation(
                f"frozen {s.kind} was duplicated {n}x by adaptation: {s.text!r}")

    out = masked
    for s in spans:
        out = out.replace(s.token, s.text)
    return out


def verify_restoration(original: str, restored: str,
                       spans: list[FrozenSpan]) -> bool:
    """
    Byte-exact check that every frozen stretch survived adaptation intact.

    This does NOT require original == restored — the whole point of adaptation
    is that the surrounding prose changed. It requires that each frozen string
    appears in the restored text exactly as many times as it appeared in the
    original, byte for byte, with no normalisation, no casefolding and no
    Unicode re-composition.

    Encoding to UTF-8 before counting is deliberate: a transform that silently
    NFC-normalised an Arabic string would compare equal as `str` under some
    comparisons but is a different byte sequence on disk, and the index Mizan
    compares against is byte-oriented.
    """
    ob, rb = original.encode("utf-8"), restored.encode("utf-8")
    for s in spans:
        sb = s.text.encode("utf-8")
        if ob.count(sb) != rb.count(sb):
            return False
        if sb not in rb:
            return False
    return True


# ======================================================================
# Readability metrics — deterministic, stdlib only
# ======================================================================

_VOWEL_RUN = re.compile(r"[aeiouy]+", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_ARABIC_CHAR = re.compile(r"[؀-ۿ]")


def _syllables(word: str) -> int:
    """
    Crude English syllable count: vowel runs, minus a silent trailing 'e'.

    Crude is fine and honest. We only ever compare a sentence against ITSELF
    before and after a split, so a systematic bias cancels out. We never claim
    an absolute grade level against an external standard.
    """
    w = word.lower().strip("'")
    if not w:
        return 0
    n = len(_VOWEL_RUN.findall(w))
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee", "ye")):
        n -= 1
    return max(n, 1)


@dataclass
class Readability:
    """
    Measured, not estimated. Every field is reproducible from the text.

    `flesch` is the classic Flesch Reading Ease (higher = easier). It is only
    meaningful for Latin-script text; for Arabic it is reported as None and
    `words_per_sentence` carries the signal instead. Saying so explicitly is
    better than printing a number that means nothing.
    """

    n_sentences: int
    n_words: int
    words_per_sentence: float
    syllables_per_word: float
    longest_sentence_words: int
    flesch: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def readability(text: str, lang: str | None = None) -> Readability:
    """
    Compute readability over a stretch of text (one or many sentences).

    `flesch` is None unless the text is English: `lang` other than "en", or
    any Arabic letter, or mostly non-Latin letters (Devanagari, Bengali...).
    """
    sentences = [s for s in split_sentences(text) if s.strip()]
    words = text.split()
    n_sent = max(len(sentences), 1)
    n_words = len(words)

    if n_words == 0:
        return Readability(0, 0, 0.0, 0.0, 0, None)

    wps = n_words / n_sent
    longest = max((len(s.split()) for s in sentences), default=0)

    if (_ARABIC_CHAR.search(text) or _latin_share(text) < 0.9
            or (lang is not None and _norm_lang(lang) != "en")):
        # Flesch's syllable model is English-specific. Reporting it for Arabic,
        # Hindi or Tagalog would be a fabricated number, which is exactly what
        # this project exists to argue against. (The first version computed
        # it for any script without Arabic letters, Devanagari included.)
        return Readability(n_sent, n_words, round(wps, 2), 0.0, longest, None)

    words = _WORD_RE.findall(text)
    n_words = len(words)
    if n_words == 0:
        return Readability(n_sent, 0, 0.0, 0.0, longest, None)
    wps = n_words / n_sent
    longest = max((len(_WORD_RE.findall(s)) for s in sentences), default=0)

    syl = sum(_syllables(w) for w in words)
    spw = syl / n_words
    flesch = 206.835 - 1.015 * wps - 84.6 * spw
    return Readability(n_sent, n_words, round(wps, 2), round(spw, 2), longest,
                       round(flesch, 1))


# ======================================================================
# Segmentation
# ======================================================================

# Sentence terminators, including the Arabic question mark, the Urdu full
# stop and the Devanagari/Bengali danda (। ॥). Without the danda a Hindi or
# Bengali document was one "sentence".
_SENT_END = re.compile(
    r"(?<=[.!?؟۔。।॥])[\s ]+(?=[^\s])"
)

# Abbreviations after which a period is NOT a sentence end. Religious texts are
# full of these ("pbuh.", "Dr.", "Vol.", "Q."), and splitting on them produces
# garbage fragments that then get "adapted".
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "vol", "no", "pp", "p", "ed",
    "eds", "cf", "ie", "eg", "etc", "vs", "ibid", "trans", "pbuh", "saw",
    "swt", "ra", "rah", "q", "ch", "fig",
}


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """
    Sentence boundaries as (start, end) CHARACTER OFFSETS into `text`.

    Offsets, not strings, are the primitive here. An earlier version returned
    strings and the caller recovered positions with `text.index(sentence)` —
    which blew up on the first real document, because whitespace between
    sentences (a paragraph break, a non-breaking space) is not part of any
    sentence, so the re-joined string was not findable verbatim. Worse, when
    the same sentence appears twice in a document, `index` silently returns
    the wrong one and scripture gets frozen at the wrong offsets. Carrying
    offsets end-to-end makes both failures impossible rather than unlikely.

    Not a general NLP sentence splitter and not trying to be. It is
    conservative: when in doubt it does NOT split, because an over-split
    sentence becomes two short fragments that each look eligible, while an
    under-split one is simply flagged as long. The failure directions are
    asymmetric and we take the safe one.
    """
    if not text or not text.strip():
        return []

    # Candidate boundaries: the start of each piece after a terminator.
    bounds: list[int] = [0]
    for m in _SENT_END.finditer(text):
        bounds.append(m.end())
    bounds.append(len(text))

    spans: list[tuple[int, int]] = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        piece = text[start:end]
        if not piece.strip():
            # Pure whitespace (e.g. a trailing paragraph break): attach it to
            # the previous sentence rather than emitting an empty one.
            if spans:
                spans[-1] = (spans[-1][0], end)
            continue
        if spans and _is_false_boundary(text[spans[-1][0]:spans[-1][1]]):
            spans[-1] = (spans[-1][0], end)
            continue
        spans.append((start, end))

    # Trim trailing whitespace off each span so a sentence never carries the
    # gap that follows it. Leading whitespace was already consumed by _SENT_END.
    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        while end > start and text[end - 1].isspace():
            end -= 1
        while start < end and text[start].isspace():
            start += 1
        if start < end:
            trimmed.append((start, end))
    return trimmed


def _is_false_boundary(previous: str) -> bool:
    """
    True when the previous piece ended on something that only LOOKS like a
    sentence terminator: a known abbreviation ("Dr.", "vol.", "pbuh.") or a
    single initial ("A." in "Ibn A. Rushd"). Splitting there produces
    fragments that then look like eligible short sentences.
    """
    prev = previous.rstrip()
    if not prev.endswith("."):
        return False
    words = prev.split()
    if not words:
        return False
    last = words[-1]
    if last.rstrip(".").lower() in _ABBREV:
        return True
    return bool(re.fullmatch(r"[A-Z]\.", last))


def split_sentences(text: str) -> list[str]:
    """Sentence strings. Thin wrapper over `sentence_spans`."""
    return [text[s:e] for s, e in sentence_spans(text)]


@dataclass
class Sentence:
    """
    One segmented sentence and everything decided about it.

    Carries `original` untouched always. `adapted` is None unless adaptation
    both ran AND verified; there is no state in which `adapted` holds
    unverified text.
    """

    index: int
    original: str
    kind: str                                   # QUOTED | PROSE
    disposition: str                            # ELIGIBLE | REFUSED
    refusal_reasons: list[str] = field(default_factory=list)
    adapted: str | None = None
    suggestions: list["Suggestion"] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)   # the frozen originals
    readability_before: Readability | None = None
    readability_after: Readability | None = None

    @property
    def output(self) -> str:
        """What a publisher would print: the adapted form, or the original."""
        return self.adapted if self.adapted is not None else self.original

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "original": self.original,
            "kind": self.kind,
            "disposition": self.disposition,
            "refusal_reasons": [
                {"code": c, "message": REFUSAL_MESSAGE.get(c, c)}
                for c in self.refusal_reasons
            ],
            "adapted": self.adapted,
            "output": self.output,
            "frozen": self.frozen,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "readability_before": (self.readability_before.as_dict()
                                   if self.readability_before else None),
            "readability_after": (self.readability_after.as_dict()
                                  if self.readability_after else None),
        }


def _word_offsets(text: str) -> list[tuple[int, int]]:
    """Character (start, end) for each whitespace-delimited word."""
    out, pos = [], 0
    for w in text.split():
        start = text.index(w, pos)
        out.append((start, start + len(w)))
        pos = start + len(w)
    return out


def quoted_char_spans(text: str, findings) -> list[tuple[int, int]]:
    """
    Convert report findings' word indices into character ranges in `text`.

    `report.Finding` gives `start_word` / `end_word` as indices into
    `text.split()` (see detect.Span). Findings with no position — e.g. a verse
    resolved in the Arabic but never located in the translation — contribute
    nothing here and are handled by the attribution panel, not this one.
    """
    offsets = _word_offsets(text)
    spans: list[tuple[int, int]] = []
    for f in findings or []:
        sw = getattr(f, "start_word", None)
        ew = getattr(f, "end_word", None)
        if sw is None or ew is None:
            continue
        if not (0 <= sw < ew <= len(offsets)):
            continue
        spans.append((offsets[sw][0], offsets[ew - 1][1]))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent character ranges."""
    if not spans:
        return []
    out = [list(s) for s in sorted(spans)]
    merged = [out[0]]
    for s, e in out[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def segment(text: str, quoted_spans: list[tuple[int, int]] | None = None
            ) -> list[tuple[str, str, list[tuple[int, int]]]]:
    """
    Split into sentences and classify each as QUOTED or PROSE.

    Returns (sentence_text, kind, local_quoted_spans) where the spans are
    re-based onto the sentence's own character indices, ready for `freeze`.

    A sentence is QUOTED if it OVERLAPS a detected quotation at all — not only
    if it is entirely inside one. Partial overlap is the dangerous case: "He
    said <half a verse> and then continued" would otherwise be adapted with
    scripture embedded in it.
    """
    quoted_spans = _merge_spans(quoted_spans or [])

    results: list[tuple[str, str, list[tuple[int, int]]]] = []
    for start, end in sentence_spans(text):
        s = text[start:end]

        local: list[tuple[int, int]] = []
        for qs, qe in quoted_spans:
            if qe <= start or qs >= end:
                continue
            local.append((max(qs, start) - start, min(qe, end) - start))

        kind = QUOTED if local else PROSE
        results.append((s, kind, local))
    return results


# ======================================================================
# Adaptation transforms — deterministic, suggest-don't-rewrite
# ======================================================================

SUG_SPLIT = "SPLIT"
SUG_PASSIVE = "PASSIVE"
SUG_NOUN_CHAIN = "NOUN_CHAIN"
SUG_GLOSS = "GLOSS"
SUG_LENGTH = "LENGTH"


@dataclass
class Suggestion:
    """
    One thing an editor might want to change, with the reason.

    A suggestion is addressed to a human. Mizan applies only SUG_SPLIT
    automatically, because a split provably cannot add, remove or reorder any
    content word — it only inserts a boundary. Everything else requires a
    judgement about meaning, which is exactly the judgement this system is
    built to refuse.
    """

    code: str
    detail: str
    applied: bool = False
    span: str = ""        # the offending fragment, for highlighting

    def as_dict(self) -> dict:
        return asdict(self)


# Coordinating conjunctions it is safe to split at. "and"/"but"/"so" start an
# independent clause often enough that the split reads naturally. Subordinating
# conjunctions ("because", "although", "which") are EXCLUDED: splitting there
# strands a dependent clause as a fragment and can invert the logical relation.
_SPLIT_CONJ = ("and", "but", "so", "yet")

_PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|get|gets|got)\s+"
    r"(?:\w+ly\s+)?(\w+(?:ed|en|wn|de|ne|ht))\b(?=\s+by\b|\s|$)",
    re.IGNORECASE,
)

# Participles whose -ed/-en form is almost always adjectival, not passive.
_NOT_PASSIVE = {
    "based", "located", "related", "limited", "known", "used", "called",
    "named", "intended", "concerned", "interested", "involved", "needed",
    "required", "allowed", "supposed", "aged", "advanced", "detailed",
    "united", "learned", "educated", "written", "given", "taken", "seen",
}


def _split_at_conjunctions(sentence: str, prof: LevelProfile) -> tuple[str, int]:
    """
    Split an over-long sentence at safe coordinating conjunctions.

    Returns (text, n_parts); n_parts == 1 means "not split" and text is the
    input unchanged.

    Rules, all of which exist to make the split provably content-preserving:
      * only before a listed coordinating conjunction preceded by a comma;
      * never produce a part shorter than `prof.min_split_part` words;
      * never split a sentence a condition governs (`_is_conditional`): the
        gate already refuses those, and this check keeps any direct caller
        from moving a clause out of an "if";
      * never split inside a frozen token (impossible by construction: the
        token contains no whitespace and no conjunction);
      * the conjunction is KEPT and starts the new sentence ("X, and Y" ->
        "X. And Y"). Dropping it would read marginally better but would mean
        the adapted text is no longer a strict content-superset of the
        original, and "no word was removed" is a claim we want to be able to
        make without an asterisk;
      * edits are made IN PLACE by character offset: the comma becomes a full
        stop and the conjunction's first letter is capitalised — the ONLY two
        character-level edits. Every other character, including line breaks
        and double spaces, is carried over exactly. (The first version
        re-joined words with single spaces.)
    """
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", sentence)]
    if len(words) <= prof.max_words or _is_conditional(sentence):
        return sentence, 1

    cuts: list[int] = []                 # word index of each new part's conjunction
    current = 0                          # words in the part being built
    for i, (s, e) in enumerate(words):
        bare = sentence[s:e].strip(",;:").lower()
        remaining = len(words) - i
        if (bare in _SPLIT_CONJ and i
                and current >= prof.min_split_part
                and remaining - 1 >= prof.min_split_part
                and sentence[words[i - 1][0]:words[i - 1][1]].endswith(",")):
            cuts.append(i)
            current = 1
            continue
        current += 1
    if not cuts:
        return sentence, 1

    out: list[str] = []
    pos = 0
    for i in cuts:
        prev_s, prev_e = words[i - 1]
        conj_s, conj_e = words[i]
        head = sentence[pos:prev_e]
        stripped = head.rstrip(",;")
        terminator = "" if stripped.endswith((".", "!", "?")) else "."
        out.append(stripped + terminator)
        out.append(sentence[prev_e:conj_s])            # original whitespace
        conj = sentence[conj_s:conj_e]
        out.append(conj[0].upper() + conj[1:] if conj[0].islower() else conj)
        pos = conj_e
    out.append(sentence[pos:])
    return "".join(out), len(cuts) + 1


def _passive_suggestions(sentence: str) -> list[Suggestion]:
    """Flag probable passive constructions. Flag only — never rewrite."""
    out = []
    for m in _PASSIVE.finditer(sentence):
        participle = m.group(1).lower()
        if participle in _NOT_PASSIVE:
            continue
        out.append(Suggestion(
            SUG_PASSIVE,
            "Passive construction. An active form usually names the actor, "
            "which matters in da'wah prose where the actor is often God or "
            "the Prophet.",
            span=m.group(0)))
    return out


def _noun_chain_suggestions(sentence: str, prof: LevelProfile) -> list[Suggestion]:
    """
    Flag runs of consecutive nouns/long words that stack into one phrase.

    Heuristic, and labelled as such: a run of `max_noun_chain`+ consecutive
    words of 6+ letters with no function word between them. "The Islamic
    content translation verification process" is the shape we are after.
    """
    words = sentence.split()
    function_words = {
        "the", "a", "an", "of", "in", "on", "to", "for", "with", "and", "or",
        "but", "is", "are", "was", "were", "that", "which", "who", "by",
        "from", "as", "at", "it", "its", "this", "these", "those",
    }
    out, run = [], []
    for w in words + [""]:
        bare = re.sub(r"[^\w-]", "", w)
        if bare and bare.lower() not in function_words and len(bare) >= 6:
            run.append(bare)
            continue
        if len(run) >= prof.max_noun_chain:
            out.append(Suggestion(
                SUG_NOUN_CHAIN,
                f"{len(run)} long words stacked with no connector. Break the "
                f"phrase or add 'of'/'that' so the reader can parse it.",
                span=" ".join(run)))
        run = []
    return out


def _gloss_suggestions(sentence: str, prof: LevelProfile,
                       already_glossed: set[str]) -> list[Suggestion]:
    """
    Flag approved shar'i terms that this audience may not know.

    Suggests a gloss on FIRST USE only — `already_glossed` is threaded through
    the document so a term is not flagged in every paragraph. Never proposes a
    replacement: the package puts the Jumhara dictionary ahead of automatic
    translation for sensitive terms, so the term stays and a note is added.
    """
    if not prof.gloss_terms:
        return []
    out = []
    for m in _GLOSSARY_RE.finditer(sentence):
        entry = _glossary_entry(m.group(1))
        if entry is None:
            continue
        # Keyed by the ARABIC term, not the surface: "Tawhid" then "Tawheed"
        # in one document is one term, glossed once.
        if entry.arabic in already_glossed:
            continue
        already_glossed.add(entry.arabic)
        out.append(Suggestion(
            SUG_GLOSS,
            f"First use of an approved term. Approved rendering: "
            f"{entry.approved_english} ({entry.arabic}). Suggested gloss: "
            f"{entry.gloss} Constraint: {entry.constraint}",
            span=m.group(0)))
    return out


# ======================================================================
# The Oct 5 model seam
# ======================================================================

def model_adapt(sentence: str, level: str) -> str | None:
    """
    The model-backed rewriting layer. NOT IMPLEMENTED — plugs in on Oct 5.

    CONTRACT — an implementation MUST satisfy all of these:

    Parameters
    ----------
    sentence : str
        ONE sentence that has already passed `refusal_reasons()` (empty list)
        AND already had every quotation and every approved shar'i term replaced
        by an opaque frozen token of the form \\u2402MZN<n>\\u2403. The
        implementation receives the MASKED sentence, never the scripture. This
        ordering is the structural guarantee: a model physically cannot rewrite
        a verse it was never shown.
    level : str
        One of clarity.LEVELS. Governs how far the rewrite may go, never what
        it may claim.

    Returns
    -------
    str | None
        The rewritten MASKED sentence, or None meaning "no improvement to
        offer" — which is always an acceptable answer and must be preferred to
        a speculative rewrite. The caller thaws, verifies byte-exactness and
        rolls the whole sentence back on any mismatch, so returning something
        slightly wrong is safe but wasteful; returning None is cheap.

    Guarantees the caller relies on
    -------------------------------
    1. TOKEN FIDELITY. Every \\u2402MZN<n>\\u2403 token present in the input
       appears EXACTLY ONCE in the output, unmodified, and no new token
       appears. The caller enforces this (`thaw` raises FrozenTokenViolation),
       but an implementation that violates it will see 100% rollback and is
       therefore useless rather than dangerous.
    2. NO NEW CLAIMS. It may reorder, split, de-passivise and simplify
       connectives. It may NOT add a fact, an attribution, a ruling, a number,
       a name, a date, a scriptural allusion or a qualifier that was not in the
       input. Content words that are not in the input should not be in the
       output except for closed-class function words.
    3. NO NORMATIVE LANGUAGE INTRODUCED. The output is re-run through
       `refusal_reasons()` by the caller; if the rewrite has introduced
       normative, preference, attribution or creed language, the sentence is
       rolled back. A model that "helpfully" turns "many Muslims pray at dawn"
       into "Muslims must pray at dawn" is caught here, not in production.
    4. DETERMINISTIC for a given model version. Temperature 0 or equivalent, so
       `python -m mizan.eval --runs 3` reproduces.
    5. OFFLINE-SAFE. If the provider is unreachable it must RAISE, not return
       degraded output. The caller catches and falls back to the deterministic
       transforms — the documented fallback mode, same as the semantic tier in
       detect.py.

    What it adds over the deterministic path
    ----------------------------------------
    The deterministic transforms can only split at a comma plus coordinating
    conjunction, and can only FLAG a passive or a noun stack. They cannot
    de-passivise, cannot reorder a fronted subordinate clause, and cannot
    simplify a Latinate word to a plain one. Those are the three edits that
    actually move a Flesch score for a reader at the `curious` level, and all
    three need a language model. This function is where they arrive — behind
    the mask, behind the allow-list, behind the byte-exact check.
    """
    raise NotImplementedError(
        "model adaptation not wired yet — see the docstring for the required "
        "contract. The deterministic transforms are the documented fallback."
    )


# ======================================================================
# Per-sentence adaptation
# ======================================================================

def adapt_sentence(sentence: str, level: str = PRACTISING, *,
                   index: int = 0,
                   quoted_spans: list[tuple[int, int]] | None = None,
                   kind: str = PROSE,
                   already_glossed: set[str] | None = None,
                   use_model: bool = False,
                   lang: str = "en",
                   in_quotation: bool = False) -> Sentence:
    """
    Run one sentence through the whole panel: gate, freeze, adapt, verify.

    The control flow is deliberately linear and boring, because every branch
    here is a place where scripture could leak. Read it top to bottom:

        QUOTED              -> return untouched, REFUSED / R_SCRIPTURE
        gate fails          -> return untouched, REFUSED / <reasons>
                               (lexical gate + scripture guard)
        freeze              -> mask quotations and approved terms
        adapt (det. or model)
        thaw + verify       -> byte-exact or FULL ROLLBACK
        re-gate the output  -> adaptation must not have introduced a ruling
    """
    prof = profile(level)
    already_glossed = already_glossed if already_glossed is not None else set()
    before = readability(sentence, lang)

    # --- 1. Scripture never reaches the adapter -----------------------
    if kind == QUOTED or quoted_spans:
        return Sentence(index=index, original=sentence, kind=QUOTED,
                        disposition=REFUSED, refusal_reasons=[R_SCRIPTURE],
                        readability_before=before,
                        frozen=[sentence[s:e] for s, e in (quoted_spans or [])])

    # --- 2. The allow-list gate, including the scripture guard --------
    reasons = refusal_reasons(sentence, lang)
    if in_quotation and R_SCRIPTURE not in reasons:
        # Inside quotation marks that open or close in another sentence.
        reasons.insert(0, R_SCRIPTURE)
    if reasons:
        return Sentence(index=index, original=sentence, kind=PROSE,
                        disposition=REFUSED, refusal_reasons=reasons,
                        readability_before=before)

    # --- 3. Freeze ----------------------------------------------------
    masked, spans = freeze(sentence, quoted_spans=None, freeze_terms=True)

    # --- 4. Adapt (on the masked form only) ---------------------------
    suggestions: list[Suggestion] = []
    n_words = len(masked.split())

    if n_words > prof.max_words:
        suggestions.append(Suggestion(
            SUG_LENGTH,
            f"{n_words} words; the {prof.name} profile targets "
            f"{prof.max_words} or fewer.",
            span=""))

    adapted_masked: str | None = None
    if use_model:
        try:
            adapted_masked = model_adapt(masked, level)
        except NotImplementedError:
            adapted_masked = None     # documented fallback: deterministic path

    if adapted_masked is None:
        split_text, n_parts = _split_at_conjunctions(masked, prof)
        if n_parts > 1:
            adapted_masked = split_text
            suggestions.append(Suggestion(
                SUG_SPLIT,
                f"Split into {n_parts} sentences at a coordinating "
                f"conjunction. No content word was added, removed or "
                f"reordered.",
                applied=True))

    if prof.flag_passive:
        suggestions.extend(_passive_suggestions(masked))
    suggestions.extend(_noun_chain_suggestions(masked, prof))
    suggestions.extend(_gloss_suggestions(sentence, prof, already_glossed))

    # --- 5. Thaw and verify, byte-exact -------------------------------
    adapted: str | None = None
    if adapted_masked is not None and adapted_masked != masked:
        try:
            restored = thaw(adapted_masked, spans)
            if not verify_restoration(sentence, restored, spans):
                raise FrozenTokenViolation(
                    "frozen spans did not survive byte-exact restoration")
            # --- 6. Re-gate: adaptation must not have created a ruling,
            # a condition or scripture-like wording.
            if refusal_reasons(restored, lang):
                raise FrozenTokenViolation(
                    "adaptation introduced normative language")
            adapted = restored
        except FrozenTokenViolation:
            # FULL ROLLBACK. Not a partial repair, not a best-effort merge.
            return Sentence(index=index, original=sentence, kind=PROSE,
                            disposition=REFUSED,
                            refusal_reasons=[R_ROLLBACK],
                            readability_before=before,
                            frozen=[s.text for s in spans])

    after = readability(adapted, lang) if adapted is not None else before

    return Sentence(index=index, original=sentence, kind=PROSE,
                    disposition=ELIGIBLE, adapted=adapted,
                    suggestions=suggestions,
                    frozen=[s.text for s in spans],
                    readability_before=before, readability_after=after)


# ======================================================================
# Document-level result
# ======================================================================

@dataclass
class ClarityReport:
    """
    The whole Panel 3 answer for one document. JSON-serialisable via as_dict().

    `refusal_rate` is the headline figure and is stated as a FEATURE: it is the
    measured share of prose Mizan declined to touch, with every reason
    enumerated in `refusal_counts`. A low number here would be the worrying
    result, not a good one.
    """

    level: str
    sentences: list[Sentence] = field(default_factory=list)
    n_quoted: int = 0
    n_prose: int = 0
    n_eligible: int = 0
    n_refused: int = 0
    n_adapted: int = 0
    refusal_counts: dict = field(default_factory=dict)
    readability_before: Readability | None = None
    readability_after: Readability | None = None
    frozen_tokens_verified: bool = True
    n_frozen: int = 0
    summary: str = ""
    # The whole output document, rebuilt from the ORIGINAL text by character
    # offset: everything between and around sentences (paragraph breaks,
    # double spaces, a line break inside a multi-sentence quotation) is
    # carried over exactly. `frozen_tokens_verified` is checked against this.
    text_out: str | None = None
    scripture_guard: dict | None = None
    lang: str = "en"

    @property
    def refusal_rate(self) -> float:
        """Share of PROSE sentences passed through untouched, 0.0-1.0."""
        if not self.n_prose:
            return 0.0
        return self.n_refused / self.n_prose

    @property
    def adapted_text(self) -> str:
        """The document as a publisher would print it after this panel."""
        if self.text_out is not None:
            return self.text_out
        return " ".join(s.output for s in self.sentences)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "level_description": profile(self.level).description,
            "counts": {
                "sentences": len(self.sentences),
                "quoted": self.n_quoted,
                "prose": self.n_prose,
                "eligible": self.n_eligible,
                "refused": self.n_refused,
                "adapted": self.n_adapted,
            },
            "refusal_rate": round(self.refusal_rate, 3),
            "refusal_counts": [
                {"code": c, "n": n, "message": REFUSAL_MESSAGE.get(c, c)}
                for c, n in sorted(self.refusal_counts.items(),
                                   key=lambda kv: -kv[1])
            ],
            "readability_before": (self.readability_before.as_dict()
                                   if self.readability_before else None),
            "readability_after": (self.readability_after.as_dict()
                                  if self.readability_after else None),
            "frozen": {
                "n": self.n_frozen,
                "verified_byte_exact": self.frozen_tokens_verified,
            },
            "scripture_guard": self.scripture_guard,
            "language_support": language_support(self.lang),
            "summary": self.summary,
            "adapted_text": self.adapted_text,
            "suggestions": [
                {"sentence": s.index, **sg.as_dict()}
                for s in self.sentences for sg in s.suggestions
            ],
            "sentences": [s.as_dict() for s in self.sentences],
        }


def language_support(lang: str) -> dict:
    """
    What this panel can do for `lang`, for the UI to state plainly.

    `ruling_screen` False means the gate cannot read rulings, creed or
    conditions in this language, so every prose sentence is refused
    (R_LANGUAGE_UNSCREENED), nothing is adapted and no suggestion is made;
    only readability is reported. `note` / `note_ar` say so in a sentence.
    """
    code = _norm_lang(lang)
    screened = code in SCREENED_LANGS
    return {"lang": code, "ruling_screen": screened, "adaptation": screened,
            "screened_languages": sorted(SCREENED_LANGS),
            "note": None if screened else LANGUAGE_NOTE_EN,
            "note_ar": None if screened else LANGUAGE_NOTE_AR}


def document_frozen_spans(text: str, quoted_spans: list[tuple[int, int]],
                          term_texts: list[str]) -> list[FrozenSpan]:
    """
    What the document-level byte check covers:
      * every quotation span we were told about, WHOLE — not its per-sentence
        pieces, which can each survive while the line break between them
        does not;
      * every frozen term of every prose sentence.
    """
    out = [FrozenSpan(token="", text=text[s:e], kind="quotation")
           for s, e in quoted_spans if 0 <= s < e <= len(text)]
    out += [FrozenSpan(token="", text=t, kind="term") for t in term_texts if t]
    return out


def verify_document(original: str, output: str,
                    frozen: list[FrozenSpan]) -> bool:
    """
    The byte-exact claim app.py displays, checked against the FINAL output
    text rather than trusted from the per-sentence checks: each frozen span
    occurs in the output exactly as often as in the input, byte for byte, and
    no frozen placeholder has leaked into the output.
    """
    if not verify_restoration(original, output, frozen):
        return False
    return output.count("␂") == original.count("␂")


def adapt_document(text: str, level: str = PRACTISING, *,
                   findings=None,
                   quoted_spans: list[tuple[int, int]] | None = None,
                   use_model: bool = False,
                   lang: str = "en") -> ClarityReport:
    """
    Run Panel 3 over a whole translated document.

    Parameters
    ----------
    text:
        The translation, exactly as the publisher wrote it.
    level:
        One of clarity.LEVELS.
    findings:
        `report.Finding` objects from `check_document`. Their start_word /
        end_word positions mark the scripture that must be frozen. Pass them
        whenever you have them: without positions this panel falls back to
        quote-mark detection alone, which is weaker.
    quoted_spans:
        Explicit character ranges, as an alternative to `findings`. Merged with
        whatever `findings` produced.
    use_model:
        Attempt `model_adapt`. Today that raises NotImplementedError and the
        deterministic path runs instead; the flag exists so the Oct 5 wiring is
        a one-line change at the call site rather than a code edit here.
    lang:
        Language of `text`. Chooses which approved translations the scripture
        guard compares against (default English). A language with no index
        fails safe: every prose sentence is refused.

    Returns
    -------
    ClarityReport — JSON-serialisable, ready for app.py to render.
    """
    prof = profile(level)
    spans = _merge_spans(list(quoted_spans or [])
                         + quoted_char_spans(text, findings))
    spans = [(s, e) for s, e in spans if 0 <= s < e <= len(text)]

    before = readability(text, lang)
    already_glossed: set[str] = set()

    sentences: list[Sentence] = []
    offsets = sentence_spans(text)
    marked = quotation_regions(text)
    for i, ((sent, kind, local), (start, end)) in enumerate(
            zip(segment(text, spans), offsets)):
        inside = any(qs < end and qe > start for qs, qe in marked)
        sentences.append(adapt_sentence(
            sent, level, index=i, quoted_spans=local or None, kind=kind,
            already_glossed=already_glossed, use_model=use_model, lang=lang,
            in_quotation=inside))

    n_quoted = sum(1 for s in sentences if s.kind == QUOTED)
    n_prose = len(sentences) - n_quoted
    n_eligible = sum(1 for s in sentences
                     if s.kind == PROSE and s.disposition == ELIGIBLE)
    n_refused = sum(1 for s in sentences
                    if s.kind == PROSE and s.disposition == REFUSED)
    n_adapted = sum(1 for s in sentences if s.adapted is not None)

    refusal_counts: dict[str, int] = {}
    for s in sentences:
        if s.kind == QUOTED:
            continue                       # counted separately, not a refusal
        for r in s.refusal_reasons:
            refusal_counts[r] = refusal_counts.get(r, 0) + 1

    # Rebuild the output from the ORIGINAL text by character offset: each
    # sentence's output replaces exactly its own span, and every character
    # between sentences is copied as-is. Joining sentences with " " (the
    # first version) turned a line break inside a multi-sentence quotation
    # into a space while still reporting it byte-exact.
    pieces: list[str] = []
    pos = 0
    for (start, end), s in zip(offsets, sentences):
        pieces.append(text[pos:start])
        pieces.append(s.output)
        pos = end
    pieces.append(text[pos:])
    out_text = "".join(pieces)
    after = readability(out_text, lang)

    all_frozen = document_frozen_spans(
        text, spans, [t for s in sentences if s.kind == PROSE for t in s.frozen])
    verified = verify_document(text, out_text, all_frozen)

    rate = (n_refused / n_prose) if n_prose else 0.0
    summary = (
        f"{len(sentences)} sentences: {n_quoted} quoted (never touched), "
        f"{n_prose} prose. Mizan declined to adapt {n_refused} of {n_prose} "
        f"prose sentences ({rate:.0%}); {n_adapted} "
        f"{'was' if n_adapted == 1 else 'were'} restructured and "
        f"{sum(len(s.suggestions) for s in sentences)} suggestions were raised "
        f"for the editor. Every frozen span verified byte-exact: {verified}."
    )
    if _norm_lang(lang) not in SCREENED_LANGS:
        summary += " " + LANGUAGE_NOTE_EN

    return ClarityReport(
        level=prof.name, sentences=sentences,
        n_quoted=n_quoted, n_prose=n_prose, n_eligible=n_eligible,
        n_refused=n_refused, n_adapted=n_adapted,
        refusal_counts=refusal_counts,
        readability_before=before, readability_after=after,
        frozen_tokens_verified=verified, n_frozen=len(all_frozen),
        summary=summary, text_out=out_text,
        scripture_guard=scripture_guard_status(lang) if n_prose else None,
        lang=_norm_lang(lang),
    )


def measure_guard(n_runs: int = 200, seed: int = 20261004,
                  langs: tuple[str, ...] = ("en", "ur", "tl", "hi", "bn")) -> dict:
    """
    Reproduce the scripture-guard calibration (docs/CLARITY.md §11.3).

    Per language:
      * real prose — every sentence (4+ words) of the publisher prose in
        data/real/: the context paragraphs around collected quotations and,
        where present, the no-quotation paragraphs (negatives.jsonl). The
        count of sentences the guard labels SCRIPTURE_LIKE. Contexts can
        contain verse text the publisher printed, so a label there is not
        necessarily wrong;
      * verse runs — `n_runs` runs of 20-30 words from long verses of that
        language's approved editions, read from the index, embedded between
        words of real prose of the same language. The count of sentences
        holding 10+ run words that the guard catches.
    Deterministic for a given seed.
    """
    import json
    import random
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    real_dir = os.path.join(root, "data", "real")

    def load(name):
        path = os.path.join(real_dir, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    rows, negs = load("corpus.jsonl"), load("negatives.jsonl")
    end = {"ur": "۔", "hi": "।", "bn": "।"}
    out: dict = {}
    for lang in langs:
        det = _guard_detector(lang)
        if det is None:
            out[lang] = {"skipped": _GUARD_WHY.get(lang)}
            continue
        pieces: list[str] = []
        for r in rows:
            if r.get("lang") == lang:
                for k in ("context_before", "context_after"):
                    c = (r.get(k) or "").strip()
                    if c and c not in pieces:
                        pieces.append(c)
        pieces += [n["text"] for n in negs if n.get("lang") == lang]
        real: list[str] = []
        for p in pieces:
            for s, e in sentence_spans(p):
                x = p[s:e]
                if len(x.split()) >= 4 and x not in real:
                    real.append(x)
        labelled = sum(1 for x in real
                       if scripture_reasons(x, lang) == [R_SCRIPTURE_LIKE])
        rng = random.Random(seed)
        ix = det._ix
        long_docs = [i for i, d in enumerate(ix.docs)
                     if len(d[3].split()) >= 32
                     and not any(ch in d[3] for ch in "_[]0123456789")]
        frames = [x.split() for x in real if len(x.split()) >= 12]
        pos: list[str] = []
        while len(pos) < n_runs and long_docs and frames:
            w = ix.docs[rng.choice(long_docs)][3].split()
            n = rng.randint(20, 30)
            st = rng.randint(0, len(w) - n)
            run = " ".join(w[st:st + n]).strip(" ,;:")
            a, b = rng.choice(frames), rng.choice(frames)
            head = " ".join(a[:8])
            doc = (head + " " + run + " " + " ".join(b[-6:]).rstrip(".।۔")
                   + end.get(lang, "."))
            rs, re_ = len(head) + 1, len(head) + 1 + len(run)
            for s, e in sentence_spans(doc):
                if (min(e, re_) > max(s, rs)
                        and len(doc[max(s, rs):min(e, re_)].split()) >= 10):
                    pos.append(doc[s:e])
        caught = sum(1 for x in pos
                     if scripture_reasons(x, lang) == [R_SCRIPTURE_LIKE])
        gp = guard_params(lang)
        out[lang] = {"params": asdict(gp),
                     "real_sentences": len(real), "real_labelled": labelled,
                     "verse_run_sentences": len(pos), "verse_runs_caught": caught}
    return out


if __name__ == "__main__":   # pragma: no cover - manual smoke check
    import json
    import sys

    if "--measure-guard" in sys.argv:
        res = measure_guard()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        for lang, r in res.items():
            if "skipped" in r:
                print(f"{lang}: skipped ({r['skipped']})")
                continue
            print(f"{lang}: real prose labelled {r['real_labelled']}/"
                  f"{r['real_sentences']} "
                  f"({100 * r['real_labelled'] / max(1, r['real_sentences']):.1f}%)"
                  f" · verse runs caught {r['verse_runs_caught']}/"
                  f"{r['verse_run_sentences']}")
        sys.exit(0)

    sample = (
        "Many people first encounter the Quran through a translation, and "
        "because the translator's choices shape what the reader understands, "
        "it matters a great deal which published edition a publisher reaches "
        "for when preparing material. A Muslim prays five times a day. "
        "It is better to recite slowly than quickly. "
        "Scholars differ on how much explanation a translator may add. "
        "The committee reviewed the manuscript over several months."
    )
    rep = adapt_document(sample, CURIOUS)
    print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2)[:4000])
    print()
    print(rep.summary)
