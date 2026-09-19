# Архитектура MailPulse

## Компоненты

```mermaid
flowchart LR
    subgraph mail[Почтовые ящики]
        gmail[Gmail · IMAP]
        ms[Microsoft 365 · IMAP OAuth2]
        imap[Другой IMAP]
    end

    subgraph app[docker compose]
        worker[worker<br/>IMAP IDLE · очередь · граф]
        pg[(Postgres<br/>данные · pgvector · jobs · checkpoints)]
        bot[bot]
        api[api]
        mcp[mcp]
    end

    gmail & ms & imap --> worker
    worker <--> pg
    worker -- карточки, черновики --> tg[Telegram]
    tg -- кнопки, голосовые --> bot
    bot -- задача resume --> pg
    miniapp[Mini App] --> api --> pg
    worker -- tool calls --> mcp
    desktop[Claude Desktop / Code] --> mcp
    worker -- LLM --> claude[Claude API]
    worker -. трейсы .-> langsmith[LangSmith]
```

| Процесс | Отвечает за |
|---|---|
| `worker` | слушает ящики, выполняет задачи из очереди, запускает и возобновляет граф |
| `bot` | принимает нажатия и голосовые, превращает их в задачи; сам LLM не вызывает |
| `mcp` | tool'ы почты для своего агента и для Claude Desktop / Code |
| `api` | бэкенд Mini App, OAuth callback Microsoft |
| `migrate` | миграции Alembic и таблицы чекпоинтера, запускается до остальных |

## Путь одного письма

1. `worker` держит IMAP IDLE. Пришёл новый UID → письмо сохраняется в `messages`, в той же транзакции ставится задача `process_email`.
2. Воркер берёт задачу (`FOR UPDATE SKIP LOCKED`) и запускает граф с `thread_id = email-{id}`.
3. Граф проходит проверки и классификацию. Для важного письма отправляет карточку и встаёт на `interrupt()`; состояние сохраняется в Postgres.
4. Пользователь нажимает кнопку → `bot` ставит задачу `resume_email` с ответом.
5. Воркер возобновляет граф: `Command(resume=…)` с тем же `thread_id`.
6. Черновик отправляется только после «Отправить»: узел `review_draft` помечает его одобренным, `send_draft` без одобрения откажет.

## Граф обработки письма

Сгенерировано из кода: `make graph` → `docs/graph.mmd`. Пунктир — условные переходы.

```mermaid
graph TD;
	__start__([start]):::first
	__end__([end]):::last
	__start__ --> load_email;
	load_email -.-> extract_attachments;
	load_email -.-> guardrail;
	extract_attachments --> guardrail;
	guardrail -.-> apply_rules;
	guardrail -.-> quarantine_notify;
	apply_rules -.-> retrieve;
	apply_rules -.-> __end__;
	retrieve --> classify;
	classify -.-> summarize;
	classify -.-> digest_enqueue;
	classify -.-> quarantine_notify;
	classify -.-> __end__;
	summarize --> send_card;
	send_card --> await_action;
	await_action -.-> draft_reply;
	await_action -.-> record_feedback;
	draft_reply --> send_draft_card;
	send_draft_card --> review_draft;
	review_draft -.-> send_reply;
	review_draft -.-> draft_reply;
	review_draft -.-> __end__;
	digest_enqueue --> __end__;
	quarantine_notify --> __end__;
	record_feedback --> __end__;
	send_reply --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| Механизм | Где |
|---|---|
| Ветвления | `load_email` (есть ли вложения), `guardrail` (подозрительное), `apply_rules` (заглушённый отправитель), `classify` (4 исхода), `await_action`, `review_draft` |
| Цикл | `draft_reply → send_draft_card → review_draft → draft_reply`, не больше 3 итераций |
| Human-in-the-loop | `await_action` (кнопки карточки), `review_draft` (Отправить / Изменить / Отмена) |

Отправка сообщения и ожидание ответа — всегда разные узлы: после resume узел с `interrupt()` выполняется заново с начала, и отправка в том же узле продублировала бы сообщение. Это проверяет тест `test_card_is_sent_once_across_resume`.

## Порты: что заменяется без изменения графа

Узлы получают зависимости через `AgentDeps` (контекст выполнения LangGraph), а не импортируют реализации. Файл: `src/mailpulse/agent/ports.py`.

| Порт | Реализация в работе | Кандидаты для A/B |
|---|---|---|
| `Classifier` | Claude Haiku 4.5, structured output | Sonnet 5 |
| `Summarizer` | Claude Sonnet 5 | effort low / medium |
| `Drafter` | Claude Sonnet 5 или Opus 5 | по итогам A/B |
| `Extractor` | текстовый слой PDF/DOCX, vision для сканов | Tesseract + текстовая модель |
| `Retriever` | pgvector + полнотекстовый поиск, RRF | с reranker / без |
| `Guard` | эвристики + Haiku 4.5 | — |
| `MailStore`, `Notifier`, `Outbox` | Postgres, Telegram, MCP `send_draft` | — |

В тестах все порты — фейки (`tests/fakes.py`), поэтому граф проверяется без сети и LLM.

## Хранилище

Всё в одном Postgres:

- данные: пользователи, ящики, письма, вложения, результаты разбора, черновики, отзывы;
- `chunks` с `vector(1024)` (HNSW, cosine) и `tsvector` (GIN) для гибридного поиска;
- `jobs` — очередь задач;
- таблицы чекпоинтера LangGraph — состояние графов на паузе;
- `llm_usage` — токены и стоимость по узлам.

Схема: `src/mailpulse/db/models.py`, миграции: `migrations/`.

## Инварианты безопасности

1. Каждый поиск по `chunks` фильтруется по `user_id`.
2. Черновик отправляется только из статуса `approved`; этот статус выставляет только нажатие в Telegram. Через MCP одобрить черновик нельзя.
3. Текст писем и вложений — данные. Это прописано в инструкциях MCP-сервера и в Skills, а подозрительные письма уходят в карантин до классификации.
4. Правила пользователя (VIP, mute) применяются кодом, а не просьбой в промпте.
5. Пароли приложений и refresh-токены хранятся зашифрованными (Fernet), ключ — только в окружении.
6. MCP-сервер без авторизации слушает только localhost.
