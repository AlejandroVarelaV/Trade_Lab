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
| `config/scenarios.yaml` | Scenario grid, risk and cost profiles, benchmark (see `config/README.md`) |
| `tradelab/config.py` | Loads/validates the YAML, resolves scenarios, computes `config_hash` |
| `tradelab/costs.py` | Commission `min(max(pct × notional, min), max)`, FX fee, slippage, ETP fee |
| `tradelab/calendars.py` | Crypto 22:00-UTC days, fill windows, ECB/TARGET business days |
| `tradelab/data/` | Alpaca (IEX daily, crypto hourly→daily, 1-min fill price, calendar), ECB, upsert + gap detection |
| `tradelab/features.py` | §3.3 features → `features_daily` (NULL when not computable) |
| `tradelab/scenarios.py` | YAML → `scenarios` rows (create / deactivate, never edit) |
| `tradelab/ledger.py` | Fill engine, C benchmark rebalancing, daily mark-to-market |
| `tradelab/jobs/daily.py` | The 22:30 UTC run; records `job_runs`, sends Telegram ✅/⚠️/🔴 |
| `tradelab/scheduler.py` | `jobs` service: runs the daily job at 22:30 UTC (with catch-up) |
| `deploy/README.md` | Build → push to ghcr.io → pull on the server |
| `docker-compose.yml` | Compose project `tradelab`: `postgres`, `migrate` (one-shot), `bot`, `jobs` |

## Phase 0 setup (manual steps)

1. **Alpaca**: sign up at alpaca.markets, switch to the *Paper* account, generate API keys.
2. **Anthropic**: create an API key and set a monthly spend limit in the console.
3. **Telegram**: create a new bot with @BotFather (not @Avarela_tfg_bot), send it any
   message, then get your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (`message.chat.id`).
4. On the server: see `deploy/README.md` (the server only needs `docker-compose.yml`
   and `.env`; the image is pulled from ghcr.io).

## Phase 0 gate commands (run on the server, in /home/avarela/tradelab)

```bash
# 1. Migrations applied (expect after Phase 1: 2|t, then 18)
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

## Phase 1: data, ledgers and benchmark

Daily run (22:30 UTC, `jobs` service): re-ingest the last 7 days (upsert) →
recompute features → sync `config/scenarios.yaml` → fill pending orders →
decide C rebalances → rewrite every ledger's daily snapshots → `job_runs` row +
Telegram ✅ (clean) / ⚠️ (gaps, rejected values, NaN features, split-like moves,
waiting orders) / 🔴 (exception, or a ledger can't be marked for the latest day).

Ledgers: each strategy scenario has A and B (cash only until Phase 2); C is one
ledger per (capital, cost profile) pair, stored as a `kind = 'benchmark'`
scenario (`bench_c200_myinvestor`, ...). C is 80% SPY / 20% BTC, decided at
inception and on the first run of each month, filled by the same engine as A/B.

```bash
docker compose run --rm jobs python -m tradelab.jobs.daily --backfill --ingest-only   # once
docker compose run --rm jobs python -m tradelab.jobs.daily                            # manual run
```

### Phase 1 gate commands

```bash
docker compose exec -T postgres psql -U tradelab -d tradelab <<'SQL'
-- 5 consecutive daily runs, all ok/warn
SELECT run_at::date, status FROM job_runs WHERE job = 'daily' ORDER BY run_at DESC LIMIT 5;
-- bars per symbol vs expected (stocks: closed sessions; crypto: calendar days)
WITH r AS (SELECT min(date) d0, max(date) d1 FROM bars_daily)
SELECT b.symbol, count(*) AS bars,
       CASE WHEN i.asset_class = 'crypto' THEN (SELECT d1 - d0 + 1 FROM r)
            ELSE (SELECT count(*) FROM market_calendar m, r WHERE m.date BETWEEN r.d0 AND r.d1) END AS expected
FROM bars_daily b JOIN instruments i USING (symbol) GROUP BY b.symbol, i.asset_class, i.id ORDER BY i.id;
-- last full week: crypto 7 bars, stocks 5 (fewer only on a US holiday)
SELECT symbol, count(*) FROM bars_daily
WHERE date BETWEEN date_trunc('week', now())::date - 7 AND date_trunc('week', now())::date - 1
GROUP BY symbol ORDER BY symbol;
-- fx_daily: one row per ECB business day, none on weekends
SELECT count(*), count(*) FILTER (WHERE extract(isodow FROM date) > 5) FROM fx_daily;
-- equity_daily per scenario/portfolio
SELECT s.name, e.portfolio, count(*), max(e.date) FROM equity_daily e
JOIN scenarios s ON s.id = e.scenario_id GROUP BY 1, 2 ORDER BY 1, 2;
SQL
```

### Known limitations (Phase 1)

- **Raw prices.** Stock bars are unadjusted, so dividends are not credited (the
  same for A, B and C) and a split would look like a crash; the run flags any
  stock/ETF move over 25% with ⚠️. Splits need manual handling.
- **IEX feed.** Volume is IEX-only (a few % of the consolidated tape); the volume
  z-score is still comparable over time. The open is IEX's first trade.
- **Fractional quantities.** 200 EUR can't buy one SPY share; the ledger assumes
  fractional units, as the EUR-listed products being modeled would need.
- **FX at fills** uses the ECB rate of the fill day (published ~14:00 UTC, a
  little after the 12:00/13:30 fills). Weekends use the last ECB rate.
- **ETP fee** is debited from cash daily, so a fully invested ledger can show a
  few cents of negative cash; equity is the same as if the fee came off the NAV.
- **Missing data cascades.** If a held crypto's bar is missing, that day's ETP fee
  can't be computed, so that ledger stays unmarked until the bar arrives.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                        # unit tests; DB tests skipped
docker run -d --rm --name tl-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/python -m pytest -q
docker rm -f tl-pg
```
