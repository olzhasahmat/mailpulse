ENV_FILE := $(if $(wildcard .env),--env-file .env,)
RUN := uv run $(ENV_FILE)

.DEFAULT_GOAL := help
.PHONY: help install up down logs db migrate revision test lint fmt api worker ingest mcp bot account-add inject-email import-archive miniapp graph eval-retrieval eval-classify secret-key set-keys check-keys check-gmail check-microsoft

help: ## список команд
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## установить зависимости
	uv sync

up: ## поднять всё в Docker
	docker compose up -d --build

down: ## остановить Docker
	docker compose down

logs: ## логи всех сервисов
	docker compose logs -f --tail=100

db: ## поднять только Postgres для локальной разработки
	docker compose up -d --wait postgres

migrate: ## применить миграции и создать таблицы чекпоинтера
	$(RUN) mailpulse-migrate

revision: ## новая миграция: make revision m="add something"
	$(RUN) alembic revision --autogenerate -m "$(m)"

test: ## тесты (с БД, если Postgres запущен)
	$(RUN) pytest

lint: ## проверить стиль
	uv run ruff check . && uv run ruff format --check .

fmt: ## отформатировать код
	uv run ruff check --fix . && uv run ruff format .

api: ## API локально
	$(RUN) mailpulse-api

worker: ## воркер локально
	$(RUN) mailpulse-worker

ingest: ## приём почты локально
	$(RUN) mailpulse-ingest

account-add: ## подключить ящик: make account-add email=you@gmail.com
	$(RUN) mailpulse-accounts add $(email)

mcp: ## MCP-сервер локально (streamable HTTP на :8001)
	$(RUN) mailpulse-mcp --transport streamable-http

bot: ## Telegram-бот локально
	$(RUN) mailpulse-bot

inject-email: ## подложить тестовое письмо: make inject-email account=yandex template=urgent
	$(RUN) mailpulse-inject --account $(or $(account),both) --template $(template)

import-archive: ## импорт архива в RAG: make import-archive [account=yandex] [days=30]
	$(RUN) mailpulse-import --account $(or $(account),all) --days $(or $(days),90)

miniapp: ## собрать фронтенд Mini App (miniapp/dist)
	cd miniapp && npm install && npm run build

miniapp-dev: ## запустить фронтенд в режиме разработки (нужен запущенный api)
	cd miniapp && npm run dev

graph: ## mermaid-диаграмма графа в docs/graph.mmd
	$(RUN) python -m mailpulse.agent.graph > docs/graph.mmd

eval-retrieval: ## оценка поиска: recall@5, MRR, порог релевантности (EMBEDDER=hashing — без модели)
	$(RUN) python evals/retrieval/run.py $(if $(EMBEDDER),--embedder $(EMBEDDER),)

eval-classify: ## оценка классификации и A/B (тратит деньги на API): make eval-classify [SPLIT=dev] [CONFIGS=prod]
	$(RUN) python evals/classification/run.py $(if $(SPLIT),--split $(SPLIT),) $(if $(CONFIGS),--configs $(CONFIGS),)

secret-key: ## сгенерировать SECRETS_KEY
	@uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

set-keys: ## вписать ключи в .env со скрытым вводом и проверить их
	uv run python scripts/set_keys.py
	uv run --env-file .env python scripts/check_keys.py

check-keys: ## проверить ключи из .env, не выводя их значения
	$(RUN) python scripts/check_keys.py

check-gmail: ## проверить Gmail по IMAP: make check-gmail email=you@gmail.com
	uv run python scripts/check_gmail_imap.py $(email)

check-microsoft: ## проверить Microsoft 365: make check-microsoft email=you@company.com
	$(RUN) python scripts/check_microsoft_imap.py $(email)
