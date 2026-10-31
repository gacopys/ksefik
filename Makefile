# ksefik - najczęstsze komendy. Samo `make` pokazuje listę.

.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
# Środowisko KSeF dla `run` i `logout`: prod | demo | test (puste = to z profilu).
# Inny profil (np. druga firma): make run PROFILE=~/firmy/druga.toml
ENV ?=
PROFILE ?=
ENV_FLAG := $(if $(ENV),--env $(ENV)) $(if $(PROFILE),--profile $(PROFILE))
ENV_ARG := $(if $(ENV),'$(ENV)',None)
PROFILE_ARG := $(if $(PROFILE),Path('$(PROFILE)').expanduser(),default_profile_path())

# Piaskownica środowiska testowego KSeF: fikcyjne dane, osobna konfiguracja i baza.
TEST_HOME ?= $(HOME)/.ksef-test
TEST_XDG := XDG_CONFIG_HOME="$(TEST_HOME)/config" XDG_DATA_HOME="$(TEST_HOME)/data"
TEST_PROFILE := $(TEST_HOME)/config/ksefik/profile.toml

CODE := src scripts tests
# Zrzuty ekranów: snap Firefox (konwersja SVG -> PNG) czyta tylko pliki spod ~/snap/firefox/common.
SCREENS_DIR ?= $(HOME)/snap/firefox/common/ksefik-screens
# Zrzuty do README (docs/screenshots/) - nazwy scenariuszy z scripts/screenshot.py --list.
DOCS_SCREENS := lista faktura podsumowanie kreator-nip konto uprawnienia-przeglad
SCREENS ?=
# Logi testów integracyjnych (każdy przebieg w osobnym podkatalogu).
IT_LOGS ?= $(CURDIR)/integration-logs

.PHONY: help install update run profile paths logout \
	test test-ui screens docs-screens coverage run-integration-test lint format check build install-tool uninstall-tool clean \
	test-env run-test-env test-env-clean

help: ## Pokaż dostępne komendy
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo

##@ Używanie

install: ## Zainstaluj zależności
	$(UV) sync

update: ## Zaktualizuj zależności do najnowszych wersji
	$(UV) lock --upgrade
	$(UV) sync

run: ## Uruchom aplikację (np. make run ENV=test)
	$(UV) run ksefik $(ENV_FLAG)

profile: ## Otwórz profil firmy (profile.toml) w edytorze - EDITOR, domyślnie nano
	@path=$$($(UV) run python -c "from pathlib import Path; from ksefik.config import default_profile_path; \
		print($(PROFILE_ARG))") && if [ ! -f "$$path" ]; then \
		echo "Brak profilu $$path - uruchom aplikację (make run), kreator go utworzy."; exit 1; fi \
		&& $${EDITOR:-nano} "$$path"

paths: ## Pokaż, gdzie leżą profil, baza i wysłane faktury
	@$(UV) run python -c "from pathlib import Path; from ksefik.config import DATA_ROOT, default_profile_path; \
		print(f'Profil:           {$(PROFILE_ARG)}'); \
		print(f'Przykład profilu: $(CURDIR)/profile.example.toml'); \
		print(f'Baza faktur:      {DATA_ROOT}/<środowisko>/<NIP>/ksef.db'); \
		print(f'Wysłane XML/UPO:  {DATA_ROOT}/<środowisko>/<NIP>/wystawione/')"

logout: ## Usuń zapisany token KSeF (np. make logout ENV=test)
	@$(UV) run python -c "from pathlib import Path; from ksefik.config import default_profile_path; \
		from ksefik.profile import load_profile; from ksefik.tokenstore import delete_token; \
		p = load_profile($(PROFILE_ARG), $(ENV_ARG)); delete_token(p.env, p.context_id); \
		print(f'Usunięto token: {p.env_label}, {p.context_id}')"

##@ Rozwój

test: ## Uruchom testy jednostkowe (bez sieci)
	$(UV) run pytest -q -n auto --ignore=tests/integration

test-ui: ## Testy interfejsu (Textual Pilot, bez sieci): przepływy, klawiatura, widoczność w 80×24
	$(UV) run pytest -q -n auto tests/ui

screens: ## Zrzuty ekranów PNG w 80×24, 120×40 i 160×50 (SCREENS="lista edycja", katalog SCREENS_DIR)
	$(UV) run python scripts/screenshot.py --png --out "$(SCREENS_DIR)" $(SCREENS)
	@echo "Zrzuty: $(SCREENS_DIR)"

docs-screens: ## Odśwież zrzuty ekranów w README (docs/screenshots/, 120×36, PNG)
	$(UV) run python scripts/screenshot.py --png --out "$(SCREENS_DIR)" --size 120x36 $(DOCS_SCREENS)
	@mkdir -p docs/screenshots && for n in $(DOCS_SCREENS); do cp "$(SCREENS_DIR)/$$n-120x36.png" "docs/screenshots/$$n.png"; done
	@echo "Zaktualizowano docs/screenshots/"

coverage: ## Testy jednostkowe z pokryciem linii i gałęzi (minimum 90%, raport HTML w htmlcov/)
	$(UV) run pytest -q -n auto --ignore=tests/integration --cov=ksefik --cov-branch \
		--cov-report=term-missing --cov-report=html --cov-report=json:coverage.json --cov-fail-under=90

run-integration-test: ## Testy end-to-end z testowym KSeF (sieć, fikcyjne firmy; ARGS="-k token"; logi w integration-logs/)
	@set -o pipefail; home=$$(mktemp -d -t ksef-it-XXXXXX); logs="$(IT_LOGS)/$$(date +%Y%m%d-%H%M%S)"; \
	mkdir -p "$$logs"; echo "Katalog testów: $$home"; echo "Logi: $$logs"; \
	KSEF_INTEGRATION=1 KSEF_IT_HOME="$$home" KSEF_IT_LOG_DIR="$$logs" \
	XDG_CONFIG_HOME="$$home/config" XDG_DATA_HOME="$$home/data" \
	PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
		$(UV) run pytest tests/integration -v -rA --durations=0 -p no:cacheprovider \
		--log-file="$$logs/przebieg.log" --log-file-level=DEBUG \
		--log-file-format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s" \
		--log-file-date-format="%H:%M:%S" $(ARGS) 2>&1 | tee "$$logs/pytest.txt"; status=$$?; \
	if [ $$status -eq 0 ]; then rm -rf "$$home"; else echo "Pliki testów zostawione do analizy: $$home"; fi; \
	echo "Logi: $$logs"; exit $$status

lint: ## Sprawdź kod (ruff: błędy i formatowanie)
	$(UV) run ruff check $(CODE)
	$(UV) run ruff format --check $(CODE)

format: ## Sformatuj kod i uporządkuj importy
	$(UV) run ruff check --fix --select I $(CODE)
	$(UV) run ruff format $(CODE)

check: lint coverage ## Lint + testy z kontrolą pokrycia (przed commitem)

build: ## Zbuduj paczkę do dist/
	$(UV) build

install-tool: ## Zainstaluj polecenie `ksefik` dostępne w całym systemie
	$(UV) tool install --force --reinstall .

uninstall-tool: ## Odinstaluj globalne polecenie `ksefik`
	$(UV) tool uninstall ksefik

clean: ## Usuń pliki tymczasowe (cache, dist)
	rm -rf dist .pytest_cache .ruff_cache
	find $(CODE) -name __pycache__ -type d -prune -exec rm -rf {} +

##@ Środowisko testowe KSeF (fikcyjne dane)

test-env: $(TEST_PROFILE) ## Przygotuj piaskownicę: fikcyjny NIP, token i profil
	@echo "Piaskownica: $(TEST_HOME)  (od nowa: make test-env-clean test-env)"

$(TEST_PROFILE):
	$(TEST_XDG) $(UV) run python scripts/test_env_token.py --setup

run-test-env: $(TEST_PROFILE) ## Uruchom aplikację w piaskownicy testowej
	$(TEST_XDG) $(UV) run ksefik --env test

test-env-clean: ## Usuń piaskownicę testową
	@if [ ! -f "$(TEST_PROFILE)" ]; then echo "Brak piaskownicy w $(TEST_HOME)."; exit 0; fi; \
	if ! grep -q '^srodowisko = "test"' "$(TEST_PROFILE)"; then \
		echo "$(TEST_HOME) nie wygląda na piaskownicę testową - nie usuwam."; exit 1; fi; \
	rm -rf "$(TEST_HOME)" && echo "Usunięto $(TEST_HOME)."
