PYTHON ?= python3
PIP := $(PYTHON) -m pip
RUN := $(PYTHON) parse_skills.py

.PHONY: help \
	install install-full install-chart install-cli \
	run run-html run-lite run-key-skills \
	clean

help: ## Show available commands
	@printf "\nSetup\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  install/p;/^  help/p'
	@printf "\nRun\n"
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sed -n '/^  run/p'
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

run: ## Run parser with default settings
	$(RUN)

run-html: ## Run parser through HTML source in description mode
	$(RUN) --source html --mode description

run-lite: ## Run parser without chart rendering
	$(RUN) --no-chart

run-key-skills: ## Run parser with auto HTML description fallback for key-skills
	$(RUN) --source auto --mode key-skills --html-description-fallback

clean: ## Remove generated artifacts
	rm -f progress.json top_skills_all_data.csv hh_skills_bar_chart.png
	rm -rf __pycache__
