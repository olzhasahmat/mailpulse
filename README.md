# MailPulse

AI-агент для входящей почты. Читает несколько ящиков (Gmail, корпоративный Microsoft 365, любой IMAP), отбирает важные письма и присылает их в Telegram с готовыми действиями: черновик ответа, «не важно», дайджест.

Финальный проект курса nFactorial по LLM-инженерии.

## Статус

| Модуль | Состояние |
|---|---|
| Схема БД, миграции, очередь задач на Postgres | готово |
| Приём почты: IMAP IDLE, разбор писем, вложения, «Отправленные» | готово для Gmail и Яндекса по паролю приложения; Microsoft 365 (OAuth2) — по результату `make check-microsoft` |
| Треды, история переписки с отправителем, одна карточка на письмо в нескольких ящиках | готово |
| Граф LangGraph: ветвления, цикл правок, два human-in-the-loop | готово |
| Классификация (Claude Haiku 4.5) и карточка (Claude Sonnet 5) | готово |
| Telegram: карточки, кнопки «Прочитано»/«Не важно», карантин | готово |
| Черновики ответов (Claude), цикл правок, отправка после подтверждения | готово; кнопка «Ответить», правка текстом, SMTP-отправка одобренного черновика |
| Вложения: текст PDF/DOCX/HTML и vision/OCR для сканов и картинок | готово; текст вложений идёт в классификацию, поиск и guardrail |
| Guardrail | эвристики на prompt injection в тексте, скрытом тексте и вложениях |
| MCP-сервер: 6 tool'ов и ресурс правил на реальной почте | готово; scoped по пользователю, `send_draft` требует одобрения в Telegram |
| Skills `email-triage` и `reply-drafting` | готово, `email-triage` используется в классификации |
| RAG: индексация писем, гибридный поиск (pgvector + полнотекст), контекст треда и похожих писем в классификации | готово: индексация, гибридный поиск, отсечение нерелевантного контекста, импорт архива; recall@5 = 1.00 на синтетическом наборе (`make eval-retrieval`) |
| Mini App (React + Vite): подключение ящиков, правила, дашборд; вход через Telegram initData | готово |
| Evals классификации (golden 40, 5+ метрик) и A/B по модели/температуре; оценка поиска recall@5 | готово (`make eval-classify`, `make eval-retrieval`) |

План по дням и обоснования решений — в [DECISIONS.md](DECISIONS.md), устройство системы — в [ARCHITECTURE.md](ARCHITECTURE.md).

## Быстрый старт

Нужны Docker, [uv](https://docs.astral.sh/uv/) и make.

```bash
cp .env.example .env
make install
make up
make test
```

`make up` поднимает Postgres, применяет миграции и запускает `ingest`, `worker`, `bot`, `api` и `mcp`. Для разбора писем в `.env` нужны `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` и `SECRETS_KEY` (`make secret-key`); для трейсов — `LANGSMITH_API_KEY` и `LANGSMITH_TRACING=true`.

## Подключение ящика

1. Отправь своему боту `/start`.
2. Создай пароль приложения: для Gmail — https://myaccount.google.com/apppasswords (нужна двухэтапная аутентификация).
3. Подключи ящик — пароль вводится в терминале и хранится зашифрованным:

```bash
make account-add email=you@gmail.com
```

4. Пришли себе письмо. Через 10–30 секунд придёт карточка, а трейс появится в LangSmith. Уже лежащие в ящике письма не разбираются — только новые.

Список ящиков и отключение: `uv run mailpulse-accounts list`, `uv run mailpulse-accounts disable you@gmail.com`.

## Тестовые письма

Чтобы прогнать письмо через весь конвейер без отправки по почте:

```bash
make inject-email account=yandex template=urgent
make inject-email account=both template=invoice   # копия в оба ящика — проверка дедупликации
```

Шаблоны: `urgent`, `invoice`, `fyi`, `spam`, `phishing` (prompt injection), `reply` (ответ в тред), `scan` (счёт картинкой — проверка vision/OCR). Такие письма помечаются `is_test`: карточка приходит с 🧪 [тест], но в RAG-индекс, историю отправителей и статистику они не попадают.

## Импорт архива

Чтобы наполнить RAG-индекс и историю прошлыми письмами (без классификации и карточек):

```bash
make import-archive days=90            # оба ящика, последние 90 дней
make import-archive account=yandex days=30
```

Письма прошлого сохраняются, попадают в треды и историю отправителей, индексируются воркером в фоне. Повторный запуск пропускает уже импортированные письма, не скачивая их тела.

Проверка: `curl localhost:8000/ready` отвечает `{"status":"ok"}`, если API видит базу.

Для локальной разработки без Docker-сервисов приложения: `make db`, `make migrate`, дальше `make api` / `make worker` / `make mcp` / `make bot`.

## Проверка почтовых ящиков

Скрипты подключаются к ящику и сообщают, что доступно: IMAP, IDLE, отправка через SMTP. Пароли и токены не сохраняются.

```bash
make check-gmail email=you@gmail.com
make check-microsoft email=you@company.com
```

Для Gmail нужен пароль приложения. Для Microsoft 365 — регистрация приложения в Entra ID, шаги описаны в начале [scripts/check_microsoft_imap.py](scripts/check_microsoft_imap.py).

## Mini App

Фронтенд на React + Vite. Вход — через подпись Telegram (`initData`), сервер по ней опознаёт пользователя без ввода Telegram-аккаунта. Три экрана: статистика (ящики, письма, разбивка по важности, стоимость, важное недавно), подключение ящиков, правила VIP/mute.

```bash
make miniapp          # собрать (miniapp/dist) — раздаётся из api на том же порту
make miniapp-dev      # режим разработки на :5173 с прокси на api
```

Mini App грузится только по HTTPS, поэтому для запуска в Telegram нужен туннель (cloudflared/ngrok) или деплой, а URL прописывается в @BotFather. Собранный фронт (`miniapp/dist`) копируется в образ `api` и раздаётся с того же адреса, что и `/api`.

## MCP-сервер и Skills в Claude

Claude Code, из корня репозитория:

```bash
claude mcp add mailpulse -- uv --directory "$(pwd)" run mailpulse-mcp
```

Skills подключены через `.claude/skills` (симлинки на `skills/`), Claude Code подхватит их сам.

Claude Desktop, `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mailpulse": {
      "command": "uv",
      "args": ["--directory", "/абсолютный/путь/к/mailpulse", "run", "mailpulse-mcp"]
    }
  }
}
```

Сервер отдаёт вашу реальную почту (scoped по пользователю). Если пользователей несколько, укажите `MCP_USER_ID` в `.env`. Демо-данные для проверки без БД: добавьте флаг `--demo`.

Через MCP можно искать письма, читать треды и вложения, смотреть, что ждёт ответа, и готовить черновики. Отправить черновик через MCP нельзя, пока он не одобрен в Telegram — это защита от prompt injection.

## Структура

```
src/mailpulse/
  agent/        граф LangGraph, порты, узлы, адаптеры (Claude, Postgres), Skills
  mail/         IMAP, разбор писем, синхронизация папок
  ingest/       сервис приёма почты
  db/           модели SQLAlchemy, сессии, запуск миграций
  mcp_server/   MCP-сервер и бэкенды
  worker/       очередь задач и цикл воркера
  bot/          Telegram-бот, карточки, уведомления
  api/          HTTP API для Mini App (initData-аутентификация, ящики, правила, статистика)
miniapp/        фронтенд Mini App (React + Vite)
skills/         Skills в формате SKILL.md
migrations/     миграции Alembic
scripts/        проверка почтовых ящиков
tests/          тесты графа, разбора писем, синхронизации, LLM, бота, MCP, очереди
```

Все команды: `make help`.
