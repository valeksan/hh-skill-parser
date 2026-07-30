PYTHON ?= python3
PIP := $(PYTHON) -m pip

.PHONY: help \
	install \
	collect resume retry coverage areas-sync areas-validate \
	extract-relevance extract-features extract-skills \
	export-vacancies export-skills export-marts stats \
	labeling-export labeling-import pilot-create pilot-report discover-skills import-skill-candidates \
	auth-login auth-refresh db-check db-checkpoint db-backup db-restore test smoke \
	clean

help: ## Show available commands
	@printf "\nSetup\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  install/p;/^  help/p'
	@printf "\nCollection / DB\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  collect/p;/^  resume/p;/^  areas-sync/p;/^  db-/p'
	@printf "\nOffline processing / export\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  extract-/p;/^  export-/p;/^  stats/p;/^  labeling-/p;/^  pilot-/p;/^  discover-/p;/^  import-/p;/^  retry/p;/^  coverage/p;/^  areas-validate/p;/^  auth-/p'
	@printf "\nTest / live smoke\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  test/p;/^  smoke/p'
	@printf "\nMaintenance\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  clean/p'

install: ## Install base project dependencies
	$(PIP) install -e .

collect: ## Start DB-backed collection (pass AREAS='--area 1 --area 2')
	$(PYTHON) -m hh_parser.cli collect $(AREAS)

resume: ## Resume DB-backed collection (pass RUN_ID=123)
	$(PYTHON) -m hh_parser.cli resume --run-id $(RUN_ID)

retry: ## Retry unresolved units in a run (pass RUN_ID=123)
	$(PYTHON) -m hh_parser.cli retry --run-id $(RUN_ID)

coverage: ## Report persisted run coverage without network (pass RUN_ID=123)
	$(PYTHON) -m hh_parser.cli coverage --run-id $(RUN_ID)

areas-sync: ## Fetch versioned HH area catalog
	$(PYTHON) -m hh_parser.cli areas sync

areas-validate: ## Validate AREAS against stored catalog (pass AREAS='--area 1')
	$(PYTHON) -m hh_parser.cli areas validate $(AREAS)

extract-relevance: ## Rebuild relevance labels from SQLite
	$(PYTHON) -m hh_parser.cli extract relevance

extract-features: ## Rebuild derived features from SQLite
	$(PYTHON) -m hh_parser.cli extract features

extract-skills: ## Rebuild skills from SQLite (pass SKILLS_FILE=path)
	$(PYTHON) -m hh_parser.cli extract --skills-file $(or $(SKILLS_FILE),skills_whitelist.txt) skills

export-vacancies: ## Export latest vacancy CSV (pass OUTPUT=vacancies.csv)
	$(PYTHON) -m hh_parser.cli export vacancies --output $(OUTPUT)

export-skills: ## Export normalized skill CSV (pass OUTPUT=vacancy_skills.csv)
	$(PYTHON) -m hh_parser.cli export skills --output $(OUTPUT)

export-marts: ## Export DA marts + manifest (pass OUTPUT_DIR=marts)
	$(PYTHON) -m hh_parser.cli export marts --output-dir $(OUTPUT_DIR)

stats: ## Print offline vacancy statistics
	$(PYTHON) -m hh_parser.cli stats

labeling-export: ## Export relevance labeling CSV (pass OUTPUT=labels.csv)
	$(PYTHON) -m hh_parser.cli export labeling --output $(OUTPUT)

labeling-import: ## Import reviewed labeling CSV (pass INPUT=labels.csv)
	$(PYTHON) -m hh_parser.cli import labeling $(INPUT)

pilot-create: ## Create fixed labeling pilot (pass BATCH_ID=id OUTPUT=pilot.csv)
	$(PYTHON) -m hh_parser.cli pilot create --batch-id $(BATCH_ID) --output $(OUTPUT)

pilot-report: ## Write offline pilot metrics (pass BATCH_ID=id OUTPUT=report.json)
	$(PYTHON) -m hh_parser.cli pilot report --batch-id $(BATCH_ID) --output $(OUTPUT)

discover-skills: ## Export skill candidates (pass OUTPUT=candidates.csv)
	$(PYTHON) -m hh_parser.cli discover skills --output $(OUTPUT)

import-skill-candidates: ## Apply review to new dictionary (pass INPUT=... SKILLS_FILE=... OUTPUT=...)
	$(PYTHON) -m hh_parser.cli import skill-candidates $(INPUT) --skills-file $(SKILLS_FILE) --output $(OUTPUT)

auth-login: ## Run local HH OAuth login (pass CLIENT_ID=id TOKEN_FILE=.hh_oauth_token.json)
	$(PYTHON) -m hh_parser.cli auth login --client-id $(CLIENT_ID) --token-file $(or $(TOKEN_FILE),.hh_oauth_token.json)

auth-refresh: ## Refresh HH OAuth token file (pass CLIENT_ID=id TOKEN_FILE=.hh_oauth_token.json)
	$(PYTHON) -m hh_parser.cli auth refresh --client-id $(CLIENT_ID) --token-file $(or $(TOKEN_FILE),.hh_oauth_token.json)

db-check: ## Run SQLite integrity check (pass DATABASE=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) check

db-checkpoint: ## Checkpoint SQLite WAL (pass DATABASE=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) checkpoint

db-backup: ## Create verified backup (pass DATABASE=path BACKUP=path)
	$(PYTHON) -m hh_parser.cli db --database $(or $(DATABASE),hh_mobilization.sqlite3) backup --output $(BACKUP)

db-restore: ## Restore backup separately (pass BACKUP=path RESTORE=path)
	$(PYTHON) -m hh_parser.cli db restore --input $(BACKUP) --output $(RESTORE)

test: ## Run deterministic local test discovery
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

smoke: ## Run opt-in live HH smoke (pass SMOKE_ARGS='--confirm-live --area 1')
	$(PYTHON) -m hh_parser.cli smoke live $(SMOKE_ARGS)

clean: ## Remove generated artifacts
	rm -rf __pycache__ build dist
	rm -f *.spec
