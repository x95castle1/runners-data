# Both sheet exports and Apple Health exports; the CLI sorts them into the right
# order so plan rows always exist before measurements try to merge into them.
CSV    ?= $(wildcard data/*.csv)
HEALTH ?= $(wildcard data/*.zip)
SOURCES = $(CSV) $(HEALTH)
DB  ?= data/runs.db
# Prefer the project venv when it exists, so `make ingest` works without activating it.
PY  ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
URL ?= http://localhost:8000
# macOS uses `open`, Linux `xdg-open`.
BROWSER ?= $(shell command -v open >/dev/null 2>&1 && echo open || echo xdg-open)

.PHONY: help venv ingest inspect start stop open exclude excluded restore logs rebuild clean test dev

help:
	@echo "make venv      - create .venv and install dependencies"
	@echo "make inspect   - show what columns were detected in data/*.csv (no writes)"
	@echo "make ingest    - load data/*.csv and data/*.zip into $(DB)"
	@echo "make start     - start the web app at $(URL)"
	@echo "make stop      - stop it"
	@echo "make open      - open the app in your browser"
	@echo "make exclude RUN=<id> - remove a run for good (survives re-imports)"
	@echo "make logs      - tail container logs"
	@echo "make rebuild   - rebuild the image from scratch"
	@echo "make test      - run the parser tests"
	@echo "make clean     - delete the database (CSVs are untouched)"

venv:
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt pytest
	@echo "venv ready"

inspect:
	@test -n "$(SOURCES)" || (echo "Nothing in data/. Put your sheet CSV or an Apple Health export.zip there first."; exit 1)
	$(PY) -m ingest.cli --inspect --db $(DB) $(ARGS) $(SOURCES)

ingest:
	@test -n "$(SOURCES)" || (echo "Nothing in data/. Put your sheet CSV or an Apple Health export.zip there first."; exit 1)
	$(PY) -m ingest.cli --db $(DB) $(ARGS) $(SOURCES)

# Same thing, but inside the running container (no local Python needed).
ingest-docker:
	docker compose exec web python -m ingest.cli --db /srv/data/runs.db $(ARGS) $(patsubst data/%,/srv/data/%,$(SOURCES))

start:
	docker compose up -d --build
	@echo "→ $(URL)"

stop:
	docker compose down

open:
	@$(PY) -c "import urllib.request; urllib.request.urlopen('$(URL)/healthz', timeout=2)" 2>/dev/null \
		|| (echo "Nothing is serving $(URL) — run \`make start\` first."; exit 1)
	$(BROWSER) $(URL)

exclude:
	@test -n "$(RUN)" || (echo "Which run? e.g. make exclude RUN=3 REASON=\"GPS outlier\""; exit 1)
	$(PY) -m ingest.exclude $(RUN) --db $(DB) $(if $(REASON),--reason "$(REASON)",)

excluded:
	$(PY) -m ingest.exclude --list --db $(DB)

restore:
	@test -n "$(KEY)" || (echo "Which run key? see \`make excluded\`"; exit 1)
	$(PY) -m ingest.exclude --restore "$(KEY)" --db $(DB)

logs:
	docker compose logs -f web

rebuild:
	docker compose build --no-cache

dev:
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	$(PY) -m pytest tests/ -q

clean:
	rm -f $(DB)
