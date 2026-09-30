# aijalon.trade — developer / operator entry points. Run from the repo root.
# Cloud targets act on PROJECT_ID (default aijalon-trade-prod) and need the owner's gcloud login.
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PROJECT_ID ?= aijalon-trade-prod
REGION     ?= asia-southeast1
PY_IMAGE   ?= python:3.12-slim
export PROJECT_ID REGION

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- supply chain
lock:  ## hash-lock backend deps (requirements*.lock) inside python:3.12-slim, then commit them
	docker run --rm -v "$(CURDIR)/backend:/w" -w /w $(PY_IMAGE) sh -c '\
	  pip install --quiet pip-tools==7.6.1 && \
	  pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --resolver=backtracking \
	    -o requirements.lock requirements.txt && \
	  pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --resolver=backtracking \
	    -o requirements-dev.lock requirements-dev.txt'
	@echo "review: git diff backend/requirements*.lock  (remove the '# verify' notes in requirements*.txt once resolved)"

pin:  ## pin GitHub Actions to commit SHAs and container images to digests
	python3 infra/pin.py all
	python3 infra/pin.py check --strict

pin-check:  ## verify pinning (what CI runs)
	python3 infra/pin.py check

# ---------------------------------------------------------------- local quality gates (mirror CI)
venv:  ## create backend/.venv with runtime + dev deps (hash-locked if lock files exist)
	python3.12 -m venv backend/.venv
	if [ -f backend/requirements.lock ]; then \
	  backend/.venv/bin/pip install --require-hashes -r backend/requirements.lock -r backend/requirements-dev.lock; \
	else backend/.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt; fi

lint:  ## ruff + bandit + mypy (mypy informational)
	cd backend && .venv/bin/ruff check . && .venv/bin/bandit -q -c pyproject.toml -r app -ll -ii
	-cd backend && .venv/bin/mypy

test:  ## backend tests (stdlib domain tests first, then pytest)
	cd backend && python3 -I -m unittest discover -s tests -t . -p 'test_domain_*.py'
	cd backend && .venv/bin/python -m pytest

audit:  ## pip-audit against the lock
	cd backend && .venv/bin/pip-audit --strict --require-hashes --disable-pip -r requirements.lock

web:  ## build the SPA (needs a global tsc)
	node web/build.mjs

csp-check:  ## infra/csp.txt == firebase.json (== web/dist/csp.txt when built)
	python3 infra/csp_sync.py check

csp-sync:  ## rebuild web with the PRODUCTION app-config, then copy its CSP into infra/csp.txt + firebase.json
	python3 -c "import json;c=json.load(open('web/public/app-config.json'));c['firebase'].update(projectId='$(PROJECT_ID)',authDomain='aijalon.trade');json.dump(c,open('/tmp/app-config.csp.json','w'))"
	APP_CONFIG=/tmp/app-config.csp.json node web/build.mjs
	python3 infra/csp_sync.py write

docker:  ## build both images locally
	docker build -f backend/Dockerfile -t aijalon-backend:local backend
	DOCKER_BUILDKIT=1 docker build -f sandbox/Dockerfile -t aijalon-sandbox:local .

validate:  ## syntax-check every shell script, YAML and JSON this repo's infra uses
	for f in $$(git ls-files '*.sh') infra/gcp/*.sh infra/cloudflare/*.sh; do bash -n "$$f"; done
	python3 -c "import glob,yaml,json;[yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')+glob.glob('infra/**/*.yaml',recursive=True)];[json.load(open(f)) for f in ('firebase.json','.firebaserc')];print('yaml/json ok')"
	python3 infra/csp_sync.py check
	python3 infra/pin.py check

# ---------------------------------------------------------------- cloud (owner's machine)
bootstrap:  ## create/verify all Google Cloud resources (BILLING_ACCOUNT=... required the first time)
	./infra/gcp/bootstrap.sh

auth-config:  ## Identity Platform upgrade + TOTP MFA + provider/domain lockdown
	./infra/gcp/firebase_auth.sh

db-bootstrap:  ## database, users, migrations, grants, privilege assertions (temporary proxy-only public IP)
	./infra/gcp/db_bootstrap.sh

dns:  ## Cloudflare DNS/TLS/WAF (needs CLOUDFLARE_API_TOKEN, FIREBASE_A_RECORDS, FIREBASE_TXT, API_LB_* inputs)
	./infra/cloudflare/dns.sh

deploy:  ## trigger the GitHub deploy workflow (production approval required)
	gh workflow run deploy.yml -R joztern755/quantscriptmarket --ref main

go-live:  ## resume the Cloud Scheduler jobs (trading starts). Only after every gate in docs/DEPLOY.md §12
	@read -r -p "Resume ALL scheduler jobs in $(PROJECT_ID)? type GO-LIVE: " a; [ "$$a" = "GO-LIVE" ]
	for j in tick settle-daily ingest-signals reconcile deposits-scan referral-tiers; do \
	  gcloud scheduler jobs resume $$j --location=$(REGION) --project=$(PROJECT_ID); done

pause-all:  ## emergency: pause every scheduler job (stops ticks; the app kill switch is the finer control)
	for j in tick settle-daily ingest-signals reconcile deposits-scan referral-tiers; do \
	  gcloud scheduler jobs pause $$j --location=$(REGION) --project=$(PROJECT_ID); done

.PHONY: help lock pin pin-check venv lint test audit web csp-check csp-sync docker validate bootstrap auth-config db-bootstrap dns deploy go-live pause-all
