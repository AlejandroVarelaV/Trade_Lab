# TradeLab

Claude as a paper-trading analyst with a human in the loop. Full spec: `SPEC.md` (v2).
**Paper money only.** Every entrypoint refuses to start unless `ALPACA_BASE_URL` is
`https://paper-api.alpaca.markets`.

## Layout

| Path | What |
|---|---|
| `database/migrate.py` | Migration runner (STAIR pattern; ledger `schema_migrations(filename, sha256, applied_at)`, one transaction per file) |
| `database/migrations/` | SQL migrations. Never edit an applied file; add a new one. |
| `tradelab/settings.py` | Env settings + paper-only guard |
| `tradelab/bot.py` | Telegram bot (long polling). Phase 0: `/ping` |
| `tradelab/checks/alpaca.py` | Phase 0 gate check for the Alpaca paper account |
| `docker-compose.yml` | Compose project `tradelab`: `postgres`, `migrate` (one-shot), `bot` |

## Phase 0 setup (manual steps)

1. **Alpaca**: sign up at alpaca.markets, switch to the *Paper* account, generate API keys.
2. **Anthropic**: create an API key and set a monthly spend limit in the console.
3. **Telegram**: create a new bot with @BotFather (not @Avarela_tfg_bot), send it any
   message, then get your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (`message.chat.id`).
4. On the server:
   ```bash
   mkdir -p /home/avarela/tradelab && cd /home/avarela/tradelab   # copy the repo here
   cp .env.example .env && chmod 600 .env && nano .env
   docker compose build && docker compose up -d
   ```

## Phase 0 gate commands (run on the server, in /home/avarela/tradelab)

```bash
# 1. Migrations applied (expect: 1|t, then 13)
docker compose exec -T postgres psql -U tradelab -d tradelab -tA <<'SQL'
SELECT count(*), bool_and(sha256 ~ '^[0-9a-f]{64}$') FROM schema_migrations;
SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';
SQL

# 2. Bot answers /ping: send /ping in Telegram, then show the log line
docker compose logs bot | grep -c "bot starting"

# 3. Alpaca paper account (expect http_200/paper_endpoint/paper_account all True, exit 0)
docker compose run --rm --no-deps migrate python -m tradelab.checks.alpaca; echo "exit=$?"

# Isolation from STAIR: nothing TradeLab-owned in the stair project
docker ps --format '{{.Names}}' | grep -c '^tradelab-'
docker volume ls --format '{{.Name}}' | grep '^tradelab_'
```

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                        # unit tests; DB tests skipped
docker run -d --rm --name tl-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/python -m pytest -q
docker rm -f tl-pg
```
