# Two commands to a running tracker:
#
#   git clone https://github.com/katbert-92/fw-footprint-tracker
#   cd fw-footprint-tracker && make up

SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: up update down logs ps env token restart release

## Start everything, generating secrets on first run
up: env
	$(COMPOSE) up -d --build
	@echo
	@$(MAKE) --no-print-directory token

## Pull a new version and restart. .env is gitignored, so it survives.
## Follows whatever ref is checked out: a branch moves, a tag does not.
update:
	git pull
	@$(MAKE) --no-print-directory up

## Cut a release: make release VERSION=0.1.1
## Tags live on main, so that a tag never points at work in progress.
release:
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=0.1.1"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "Working tree is not clean"; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = "main" || \
		{ echo "Releases are tagged on main: git checkout main && git merge dev"; exit 1; }
	@sed -i.bak -E 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml && rm -f pyproject.toml.bak
	@grep -q '^version = "$(VERSION)"$$' pyproject.toml || { echo "Could not set the version"; exit 1; }
	@git diff --quiet -- pyproject.toml || git commit -qm "Release v$(VERSION)" pyproject.toml
	git tag -a v$(VERSION) -m "v$(VERSION)"
	@echo
	@echo "Publish it:  git push origin main --follow-tags"

## Create .env, or add whatever keys a newer version needs
env:
	@python3 deploy/make-env.py

## Print what a project needs to start reporting
token:
	@source .env; \
	echo "Grafana   http://localhost:$${GRAFANA_PORT}  (admin / $${GRAFANA_ADMIN_PASSWORD})"; \
	echo "Ingest    http://localhost:$${INGEST_PORT}"; \
	echo "Token     $${FWTRACK_INGEST_TOKEN}"; \
	echo; \
	echo "In the project being measured:"; \
	echo "  FWTRACK_ENABLE=1"; \
	echo "  FWTRACK_URL=http://<this-host>:$${INGEST_PORT}"; \
	echo "  FWTRACK_INGEST_TOKEN=$${FWTRACK_INGEST_TOKEN}"

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps
