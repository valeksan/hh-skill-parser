PYTHON ?= python3
PIP := $(PYTHON) -m pip
RUN := $(PYTHON) parse_skills.py run
PYINSTALLER ?= pyinstaller
BINARY_NAME ?= hh-skill-parser

.PHONY: help \
	install install-full install-chart install-cli install-bundle \
	run run-html run-lite run-key-skills \
	collect resume areas-sync db-check db-checkpoint db-backup db-restore \
	smoke \
	bundle \
	clean

help: ## Show available commands
	@printf "\nSetup\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  install/p;/^  help/p'
	@printf "\nRun\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  run/p'
	@printf "\nCollection / DB\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  collect/p;/^  resume/p;/^  areas-sync/p;/^  db-/p'
	@printf "\nTest\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  smoke/p'
	@printf "\nBuild\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  bundle/p'
	@printf "\nMaintenance\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  clean/p'

install: ## Install base project dependencies
	$(PIP) install -e .

install-full: ## Install project with optional chart and CLI extras
	$(PIP) install -e ".[full]"

install-chart: ## Install project with charting support
	$(PIP) install -e ".[chart]"

install-cli: ## Install project with console animation support
	$(PIP) install -e ".[cli]"

install-bundle: ## Install project with PyInstaller for binary builds
	$(PIP) install -e ".[bundle]"

run: ## Run parser with default settings
	$(RUN)

run-html: ## Run parser through HTML source in description mode
	$(RUN) --source html --mode description

run-lite: ## Run parser without chart rendering
	$(RUN) --no-chart

run-key-skills: ## Run parser with auto HTML description fallback for key-skills
	$(RUN) --source auto --mode key-skills --html-description-fallback

collect: ## Start DB-backed collection (pass AREAS='--area 1 --area 2')
	$(PYTHON) -m hh_parser.cli collect $(AREAS)

resume: ## Resume DB-backed collection (pass RUN_ID=123)
	$(PYTHON) -m hh_parser.cli resume --run-id $(RUN_ID)

areas-sync: ## Fetch versioned HH area catalog
	$(PYTHON) -m hh_parser.cli areas sync

db-check: ## Run SQLite integrity check (pass DATABASE=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) check

db-checkpoint: ## Checkpoint SQLite WAL (pass DATABASE=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) checkpoint

db-backup: ## Create verified backup (pass DATABASE=path BACKUP=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) backup --output $(BACKUP)

db-restore: ## Restore backup separately (pass BACKUP=path RESTORE=path)
	$(PYTHON) -m hh_parser.cli db restore --input $(BACKUP) --output $(RESTORE)

smoke: ## Run local smoke tests
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

bundle: ## Build a one-file binary into dist/
	@if ! command -v $(PYINSTALLER) >/dev/null 2>&1; then \
		printf "PyInstaller is not installed.\n"; \
		printf "Install it with one of these commands:\n"; \
		printf "  make install-bundle\n"; \
		printf "  $(PIP) install -e \".[bundle]\"\n"; \
		exit 1; \
	fi
	MPLBACKEND=Agg $(PYINSTALLER) --clean --onefile --name $(BINARY_NAME) parse_skills.py

clean: ## Remove generated artifacts
	rm -f progress.json top_skills_all_data.csv hh_skills_bar_chart.png
	rm -rf __pycache__ build dist
	rm -f *.spec
