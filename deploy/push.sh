#!/usr/bin/env bash
# =============================================================================
# MIZAN - copy the project from this laptop to the server (run on the laptop).
#
#   deploy/push.sh root@SERVER_IP              code + data/embeddings (73 MB)
#   deploy/push.sh root@SERVER_IP --corpus     also the verified data/corpus.sqlite
#                                              (124 MB; only if the server cannot
#                                              download the Quranpedia dump)
#   deploy/push.sh root@SERVER_IP --restart    restart mizan afterwards (prewarm
#                                              included) and show its log
#
# data/embeddings/ is committed, but the server is not a git checkout and the
# CPU build takes hours, so this is how the measured index reaches the server. Third-party test text (data/real/),
# local reviewer state and caches are never uploaded. Server-side files that
# are excluded here (data/corpus.sqlite built by setup, data/raw) are kept:
# rsync --delete does not touch excluded paths.
# =============================================================================
set -euo pipefail

TARGET="${1:?usage: deploy/push.sh user@server [--corpus] [--restart]}"
shift
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORPUS=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --corpus) CORPUS=1 ;;
    --restart) RESTART=1 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

if [ ! -f "$ROOT/data/embeddings/index.json" ]; then
  echo "data/embeddings/index.json is missing: the AI tier would be off on the server."
  echo "Build it first (.venv/bin/python scripts/build_embeddings.py) or pass the existing one."
  exit 1
fi

# Local-only third-party files never leave the laptop: the held-out
# translations (only their manifest is published), the full الجمهرة file with
# definitions, and evaluation caches. The served app does not need any of them.
EXCLUDES=(
  --include '/data/heldout/manifest.json'
  --exclude '/data/heldout/*.json' --exclude 'cache/'
  --exclude '/data/glossary/jamhara.jsonl' --exclude '/data/glossary/parallel_definitions.jsonl'
  --exclude '.git' --exclude '.venv' --exclude 'venv' --exclude '__pycache__'
  --exclude '*.pyc' --exclude '.pytest_cache' --exclude '.DS_Store' --exclude '*.part'
  --exclude '/data/raw' --exclude '/data/real/raw' --exclude '/data/real/corpus.jsonl'
  --exclude '/data/real/negatives.jsonl' --exclude '/data/real/rebuild_report.json'
  --exclude '/tests/results_real.json' --exclude '/data/review.sqlite*'
  --exclude '/data/glossary/raw' --exclude '/results/cache' --exclude '/results/quick'
  --exclude '/deploy/experiments' --exclude '/data/corpus.sqlite-*'
)
if [ "$CORPUS" = 0 ]; then
  EXCLUDES+=(--exclude '/data/corpus.sqlite')
fi

ssh "$TARGET" 'mkdir -p /opt/mizan/app'
rsync -az --delete "${EXCLUDES[@]}" "$ROOT/" "$TARGET:/opt/mizan/app/"
ssh "$TARGET" 'if id mizan >/dev/null 2>&1; then chown -R mizan:mizan /opt/mizan/app/data; fi'
echo "pushed $ROOT -> $TARGET:/opt/mizan/app"

if [ "$RESTART" = 1 ]; then
  echo "restarting mizan (returns once the model is loaded)..."
  ssh "$TARGET" 'systemctl restart mizan && journalctl -u mizan -n 12 --no-pager && cat /var/lib/mizan-status/ai.json; echo'
fi
