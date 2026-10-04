#!/usr/bin/env python3
"""
Build the two benchmark documents from the locally cached real corpus.

  long.txt  - one islamreligion.com article of ~1,000 words with several
              Qur'an quotations (third-party text: written OUTSIDE the repo,
              measurement only, never committed)
  short.txt - the UI's first reviewed sample (web/app.js, s1), ~40 words

Usage: python3 deploy/experiments/make_docs.py OUT_DIR
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from collect_corpus import extract_paragraphs  # noqa: E402

out_dir = sys.argv[1]
os.makedirs(out_dir, exist_ok=True)

best = None
for path in sorted(glob.glob(os.path.join(ROOT, "data/real/raw/www.islamreligion.com/*.html"))):
    h = open(path, encoding="utf-8", errors="replace").read()
    if "/articles/" not in h[:5000] and 'lang="en"' not in h[:2000]:
        pass
    paras = extract_paragraphs(h)
    text = "\n\n".join(t for _, t in paras)
    n = len(text.split())
    quotes = sum(1 for k, _ in paras if k == "quote")
    refs = len(re.findall(r"\(\s*Qur", text))
    if 900 <= n <= 1150 and not re.search(r"[؀-ۿ]{20,}", text):
        score = quotes + refs
        if best is None or score > best[0]:
            best = (score, n, quotes, refs, path, text)

score, n, quotes, refs, path, text = best
open(os.path.join(out_dir, "long.txt"), "w", encoding="utf-8").write(text)
short = ('Then He clarified that creation was not left purposeless, saying: '
         '"And I did not create the jinn and mankind except to serve Me." (Quran 51:56). '
         'This verse is a foundation for understanding the purpose of human existence.')
open(os.path.join(out_dir, "short.txt"), "w", encoding="utf-8").write(short)
print(json.dumps({"long_source": os.path.relpath(path, ROOT), "long_words": n,
                  "long_quote_paragraphs": quotes, "long_quran_refs": refs,
                  "short_words": len(short.split())}, indent=1))
