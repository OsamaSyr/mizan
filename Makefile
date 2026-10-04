# MIZAN — every routine task is one command.
#
#   make setup        download + verify sources, build data/corpus.sqlite
#   make run          serve the app on http://127.0.0.1:$(PORT)
#   make test         unit tests (pytest if installed, plain runners otherwise)
#   make eval         re-derive every published number: 3 seeded runs
#   make real-corpus  rebuild data/real/ (third-party test text) from the publishers' pages
#
# Python 3.11+, standard library only. Override the interpreter with
# `make PYTHON=/path/to/python3.11 ...`.

PYTHON ?= python3
PORT   ?= 8000
RUNS   ?= 3
SUITE  ?= all
EVAL_ARGS ?=

export PYTHONPATH := $(CURDIR)/src

.DEFAULT_GOAL := help
.PHONY: help setup index verify real-corpus glossary-public test eval eval-quick run docker docker-ml clean distclean

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

setup:  ## one command from a clean clone: fetch, verify, build the index
	@bash scripts/setup.sh

index:  ## rebuild data/corpus.sqlite from the cached, verified sources (offline)
	@bash scripts/setup.sh --offline --force --no-changes

verify:  ## check the index against the reference fingerprint, no network
	@$(PYTHON) -m mizan.eval --check-index

real-corpus:  ## rebuild data/real/*.jsonl from the publishers' pages (~10 min, polite crawl)
	@$(PYTHON) scripts/rebuild_real_corpus.py

glossary-public:  ## write data/glossary/jamhara.public.jsonl (no definitions) from the local jamhara.jsonl
	@$(PYTHON) scripts/export_public_glossary.py

test:  ## unit tests
	@if $(PYTHON) -c "import pytest" 2>/dev/null; then \
		$(PYTHON) -m pytest -q tests; \
	else \
		set -e; for t in tests/test_*.py; do echo "== $$t"; $(PYTHON) $$t; done; \
	fi

eval:  ## python3 -m mizan.eval --suite all --runs 3  ->  results/summary.json, results/SUMMARY.md
	@$(PYTHON) -m mizan.eval --suite $(SUITE) --runs $(RUNS) --update-readme $(EVAL_ARGS)

eval-quick:  ## smoke test of the evaluation on small samples -> results/quick/
	@$(PYTHON) -m mizan.eval --suite $(SUITE) --runs 1 --quick

run:  ## serve the app (MIZAN_PORT, default 8000)
	@MIZAN_PORT=$(PORT) $(PYTHON) app.py

docker:  ## build the container image (runs setup inside the build)
	docker build -t mizan .

docker-ml:  ## build the image with the optional semantic tier (bge-m3, large)
	docker build --build-arg WITH_ML=1 -t mizan:ml .

clean:  ## remove caches; keeps data/ and results/summary.json, SUMMARY.md
	rm -rf results/cache results/quick .pytest_cache
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +

distclean: clean  ## also delete everything setup builds or downloads (data/raw, the index)
	rm -rf data/raw data/corpus.sqlite data/corpus.sqlite-wal data/corpus.sqlite-shm
