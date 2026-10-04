#!/usr/bin/env python3
"""
Write the redistributable form of the Jamhara glossary.

Why: islamic-content.com/page/copyright grants «الاستفادة العلمية من محتويات
الموقع في الاستخدام الشخصي غير التجاري» — personal, non-commercial use. A public
GitHub repository is redistribution, and the challenge terms forbid including a
third party's protected material without a legal basis. So the public repo
carries only what Mizan needs at runtime and what is a short factual mapping:
the Arabic term, its approved English equivalent(s), the category, and a link
back to the source page — never the dictionary's prose definitions.

Input : data/glossary/jamhara.jsonl          (local, gitignored, has definitions)
Output: data/glossary/jamhara.public.jsonl   (committed, no definitions)
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GLOSSARY = os.path.join(os.path.dirname(HERE), "data", "glossary")
SRC = os.path.join(GLOSSARY, "jamhara.jsonl")
DST = os.path.join(GLOSSARY, "jamhara.public.jsonl")

# The dictionary's prose stays local: its Arabic and English definitions, and
# the review notes that quote them. Everything the matcher needs — the term,
# its approved English, match forms, sense ranking, review FLAG, source URLs —
# is a short factual mapping or provenance and is kept.
DROP = ("definition_ar", "definition_en")


def main() -> int:
    if not os.path.exists(SRC):
        print(f"missing {SRC}; run scripts/fetch_jamhara.py first", file=sys.stderr)
        return 1
    n = 0
    with open(SRC, encoding="utf-8") as src, open(DST, "w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out = {k: v for k, v in rec.items() if k not in DROP}
            if isinstance(out.get("review"), dict):
                out["review"] = {"flag": out["review"].get("flag")}
            dst.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    print(f"wrote {n} records to {os.path.relpath(DST)} (definitions removed; "
          f"source: islamic-content.com, attribution via source_url)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
