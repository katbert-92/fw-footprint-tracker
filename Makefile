# Two commands to a running tracker:
#
#   git clone https://github.com/katbert-92/fw-footprint-tracker
#   cd fw-footprint-tracker && make up

SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: up update down logs ps env token restart

## Start everything, generating secrets on first run
up: env
	$(COMPOSE) up -d --build
	@echo
	@$(MAKE) --no-print-directory token

## Pull a new version and restart. .env is gitignored, so it survives.
update:
	git pull
	@$(MAKE) --no-print-directory up

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
