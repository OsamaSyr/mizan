# syntax=docker/dockerfile:1
# =============================================================================
# MIZAN — container image.
#
#   docker build -t mizan .                         # deterministic core (~0.3 GB)
#   docker run --rm -p 8000:8000 mizan              # http://localhost:8000
#   docker run --rm -e PORT=9000 -p 9000:9000 mizan
#
#   docker build --build-arg WITH_ML=1 -t mizan:ml .   # + semantic tier (bge-m3)
#
# The index is NOT copied in: `scripts/setup.sh` runs during the build and
# downloads, verifies (size vs the publisher's manifest, SHA-256 vs the pins)
# and builds data/corpus.sqlite exactly as on a laptop, then checks the result
# against the reference fingerprint. A build that cannot reproduce the
# measured index fails.
#
# The third-party test corpus (data/real/*.jsonl) is never put in the image.
# =============================================================================
ARG WITH_ML=0

# -----------------------------------------------------------------------------
FROM python:3.11-slim AS core
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app
# Unprivileged runtime user, created first so files can be owned by it in the
# same layer that creates them (a later `chown -R` would copy the 124 MB index
# into a second layer).
RUN useradd --create-home --uid 10001 mizan

# Only what setup needs, so the download/build layer stays cached while the
# rest of the code changes. After the build the raw translation files are
# dropped (the index holds them); the Quranpedia LICENSE.md and manifest, and
# the Tanzil text files carrying Tanzil's copyright block, are kept beside the
# index they were built into.
COPY scripts/setup.sh scripts/build_index.py scripts/fetch_arabic.py scripts/
COPY src/mizan/engine.py src/mizan/eval.py src/mizan/
# The redistributable glossary (no الجمهرة definitions — see .dockerignore),
# so setup's glossary step checks what the image will actually serve.
COPY data/glossary/ data/glossary/
RUN bash scripts/setup.sh --no-changes \
 && rm -f data/raw/translations-all.zip data/raw/quranpedia/*.json \
 && rm -rf /root/.cache \
 && chown -R mizan:mizan /app/data

COPY --chown=mizan:mizan . .

# -----------------------------------------------------------------------------
# Optional semantic tier. Installs the ML extra (CPU build of torch, so an
# amd64 image does not pull CUDA), fetches the pinned model revision at build
# time (nothing is downloaded at request time) and builds data/embeddings/
# unless it is already in the build context. On CPU the embedding build is
# slow (expect an hour or more for 137k renderings); it runs once, here.
FROM core AS ml-0

FROM core AS ml-1
ENV HF_HOME=/app/.cache/huggingface
RUN TORCH="$(sed -n 's/^torch==\([^ ]*\).*/\1/p' requirements-ml.txt)" \
 && pip install --no-cache-dir "torch==${TORCH}" \
        --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements-ml.txt \
 && python scripts/build_embeddings.py --download \
 && { [ -f data/embeddings/index.json ] || python scripts/build_embeddings.py; } \
 && mkdir -p /app/.cache && chown -R mizan:mizan /app/data /app/.cache

# -----------------------------------------------------------------------------
FROM ml-${WITH_ML} AS final
ENV MIZAN_HOST=0.0.0.0 \
    PORT=8000
USER mizan
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8000'), timeout=4)"
# app.py reads MIZAN_PORT; platforms (Cloud Run, Fly, Render, ...) set PORT.
CMD ["sh", "-c", "MIZAN_PORT=\"${PORT:-8000}\" exec python app.py"]
