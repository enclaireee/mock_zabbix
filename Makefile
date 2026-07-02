# OT Observability Lab — orchestration. `make help` for the menu.
.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create venv + install Python deps
	@test -d .venv || python3 -m venv .venv
	@$(PIP) install -q -r requirements.txt
	@echo "venv ready"

up: ## Start the Zabbix stack (db, server, web, agent)
	docker compose up -d
	@echo "Frontend: http://localhost:$$(grep ZBX_WEB_PORT .env | cut -d= -f2)  (Admin / zabbix)"

down: ## Stop the stack (keeps data)
	docker compose down

clean: ## Stop the stack and DELETE the database volume
	docker compose down -v

logs: ## Tail server logs
	docker compose logs -f zabbix-server

provision: venv ## Create host groups, templates, hosts, items, triggers from catalog/
	$(PY) -m otobs provision

simulate: venv ## Stream Good/Underperform/Failed mock data into Zabbix (Ctrl+C to stop)
	$(PY) -m otobs simulate

backfill: venv ## Backfill historical data (override DAYS=/SPEED=; else catalog/sim_config.yml)
	$(PY) -m otobs backfill $(if $(DAYS),--days $(DAYS)) $(if $(SPEED),--speed $(SPEED))

list: venv ## Print the parsed catalog (sanity view)
	$(PY) -m otobs list

check: venv ## Run the self-check (catalog + generator), no Zabbix needed
	$(PY) -m otobs check

.PHONY: help venv up down clean logs provision simulate backfill list check
