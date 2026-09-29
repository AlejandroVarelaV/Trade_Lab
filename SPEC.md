# TradeLab — Claude as a paper-trading analyst, with a human in the loop

Status: spec v2 (29 Sep 2026): realistic capital in EUR, scenario grid, cost profiles. Paper money only. No real orders, ever, in this version.

## 1. What this is and what question it answers

Every day, Claude proposes trades on a small, fixed set of US stocks, ETFs and crypto. Alejandro approves or rejects each one from Telegram. Everything is simulated and logged, and three portfolios run side by side:

| Portfolio | What it executes | Question it answers |
|---|---|---|
| **A: Claude alone** | Every proposal that passes the risk rules, automatically | Does Claude have any edge by itself? |
| **B: Claude + Alejandro** | Only approved proposals | Does the human filter add or destroy value? |
| **C: Benchmark** | 80% SPY / 20% BTC, rebalanced monthly | Is any of this better than doing nothing? |

The project succeeds if it produces a trustworthy answer, including "no". Making money is not the success criterion.

### 1.1 Scenarios (added in v2)
Claude is called **once per day**. Its output is a set of target weights. Every **scenario** replays those same weights (and, for B, the same approval decisions) through its own capital, risk profile and cost profile. Scenarios cost no extra API calls.

Scenarios live in `config/scenarios.yaml`. Adding or removing one is a config change plus a restart, not a code change. Each scenario gets its own A and B ledgers, plus a C benchmark per (capital, cost profile) pair. Default grid:

| Dimension | Values |
|---|---|
| Capital (EUR) | 200, 500, 1000 |
| Risk profile | `conservative`, `base`, `aggressive` (section 4) |
| Cost profile | `eu_small_account` for the whole grid; plus `zero_commission` for the primary scenario only, to measure fee drag |

**Primary scenario: 200 EUR / base / eu_small_account.** It is pre-registered, and it alone decides the verdict in section 10. All other scenarios are descriptive. With 18+ correlated ledgers, the best-looking one will be flattering by chance, so it is never promoted to "the result" after the fact.

Claude sees the state of the primary scenario's A ledger (see 3.3). Other scenarios may drift from it because their constraints differ; that drift is itself measured.

Non-goals for v1: real money, leverage, shorting, options, intraday trading, news or social media input, backtesting the LLM on history (see 3.4).

## 2. Architecture

```
cron 22:30 UTC ─► ingest ─► features ─► Claude proposer ─► risk validator ─► proposals
                                                                 │
                                          Telegram bot ◄─────────┘ (approve / reject)
                                                 │
fill engine (next fill window) ─► ledgers A / B / C ─► daily mark-to-market ─► weekly report
                                                 └─► Alpaca paper mirror of B (plumbing only)
```

- **Language:** Python 3.11+, one repo, separate from STAIR.
- **Storage:** its own Postgres (own container, own volume). It never shares a database with STAIR.
- **Where it runs:** the Hetzner server, as a separate compose project (for example `/home/avarela/tradelab`), so it keeps running when the PC is off. It must not touch the `stair` compose project, its network or its volumes. Deploy with the same image cycle as STAIR.
- **Broker:** Alpaca paper trading API. It's free, open to non-US residents, starts with $100k virtual cash, and covers US stocks, ETFs and crypto through one API. It's used for market data and as a mirror of the primary scenario's portfolio B. The fill engine is **internal**, so A, B and C are filled by identical rules (see 4). Alpaca fills are logged next to internal fills as a sanity check, never as the source of truth.
- **LLM:** Anthropic API with forced tool use and a structured output schema. A monthly spend limit is set in the Anthropic console.
- **Notifications and approval:** a **new** Telegram bot dedicated to this project, not @Avarela_tfg_bot, using long polling so no inbound port is needed. A healthchecks.io check separate from STAIR's.

## 3. Daily decision run

### 3.1 Universe (fixed for v1, stored in the `instruments` table)
- ETFs: SPY, QQQ, IWM, TLT, GLD, XLE, XLF, XLK, XLV
- Stocks: AAPL, MSFT, NVDA, AMZN, GOOGL, META
- Crypto: BTC/USD, ETH/USD, SOL/USD

Changing the universe is a versioned decision, recorded in the database. It is never done silently.

### 3.2 Timing (all UTC)
- **22:30 daily:** ingest, compute features, call Claude, validate, send proposals to Telegram. This runs after the US close in both summer and winter time.
- **Approval deadline: 12:00 the next day.** Anything not answered by then counts as **rejected** for B, and is logged as `timeout`, not as a normal reject.
- **Fills:** stocks and ETFs at the next US session's opening price. Crypto at the first 1-minute bar at or after 12:00. A and B always use the same fill time and price for the same proposal.
- Stock proposals made before a weekend or holiday fill at the next session's open.

### 3.3 Input to Claude (compact and identical every day)
- Per instrument: returns over 1/5/21/63 days, 21-day realized volatility, RSI(14), distance from the 50- and 200-day moving averages, volume z-score (stocks), last close.
- The primary scenario's A ledger: current positions, weights, unrealized P&L and stop levels. Claude always reasons about this A ledger, since that's the portfolio it controls. No B ledger is ever shown to Claude, so Alejandro's choices can't leak into Claude's decisions.
- The last 20 closed trades of A, with their outcomes.
- The risk rules in plain text.
- Optionally, VIX and the 10y-3m yield spread, read-only from STAIR's `market_features`. This is not needed for v1; if added, it goes through a read-only database user.

### 3.4 Methodology rule: forward testing only
The LLM must never be "backtested" on historical dates. The model has seen historical prices in training, so any backtest is contaminated. The only valid evidence is **forward** paper trading from the start date. Rule-based benchmarks (C, random) can be backtested, but they are only compared with A and B over the live period.

### 3.5 Output schema (tool `submit_proposals`)
```json
{
  "market_view": "string, 2-4 sentences",
  "proposals": [
    {
      "symbol": "NVDA",
      "action": "open | increase | reduce | close",
      "target_weight": 0.08,
      "stop_loss_pct": 0.07,
      "horizon_days": 10,
      "confidence": 0.55,
      "rationale": "string, max 400 chars, must cite at least one input number"
    }
  ],
  "no_trade_reason": "string, required if proposals is empty"
}
```
An empty proposal list is a valid and normal outcome.

### 3.6 Validation (lesson from the web auditor)
`required` fields in a tool schema are not strictly enforced, so every response is validated in Python and re-requested up to 3 times with a `<correction>` block. After 3 failures the run is marked `validation_failed`, nothing is traded, and Telegram gets a 🔴 alert. Reject a response if:
- a proposal has an unknown symbol, an empty or placeholder rationale, or a weight or stop outside the allowed range;
- `proposals` is empty and there is no `no_trade_reason`;
- the same symbol appears twice.

## 4. Risk rules: enforced in code, never trusted to the prompt

Proposals that break a rule are **clipped or dropped** by the validator. The original and the adjusted version are both stored, along with the rule that fired.

Rules that hold in every scenario:

| Rule | Value |
|---|---|
| Leverage | none (sum of weights ≤ 1.0, rest in cash) |
| Shorting | not allowed |
| Stop-loss | required for every open or increase |
| Kill switch | drawdown beyond the profile's limit → that ledger pauses new entries, Telegram 🔴, manual resume only |
| Minimum trade size | 10 EUR; smaller trades are skipped and logged as `below_min_size` |

Risk profiles (in `config/scenarios.yaml`):

| Parameter | conservative | base | aggressive |
|---|---|---|---|
| Max weight per instrument | 10% | 20% | 35% |
| Max total crypto weight | 10% | 20% | 40% |
| Max open positions | 4 | 6 | 8 |
| Max new or increased positions per day | 2 | 3 | 4 |
| Stop-loss range | 3–8% | 3–15% | 5–20% |
| Kill switch drawdown | 10% | 15% | 25% |

Claude is told the **base** limits. Each scenario clips Claude's target weights to its own profile, scaling a trade down to the maximum or dropping it. Every clip is logged per scenario.

Stops are checked on the daily mark (stock close, and crypto at 22:00 UTC), and exits fill at the next fill window. A stop counts as approved when its entry is approved, so it never waits for a tap. This is identical for A and B.

## 5. Costs model (applied identically to A, B and C within a scenario)
- **Ledger currency: EUR.** Instruments are priced in USD and converted with the daily ECB EUR/USD reference rate. Currency moves are part of the result, as they would be with real money.
- **Cost profiles** (in `config/scenarios.yaml`). Every fee is `max(pct_fee × notional, min_fee)`, plus slippage:

| Parameter | zero_commission | eu_small_account |
|---|---|---|
| Stocks/ETFs fee | 0 bps, no minimum | **TO VERIFY** against the broker Alejandro would actually use (placeholder: 5 bps, min 1.00 EUR per order) |
| Crypto fee | 25 bps | **TO VERIFY** (placeholder: 25 bps, min 1.00 EUR) |
| FX conversion | 0 bps | **TO VERIFY** (placeholder: 25 bps on each EUR↔USD conversion) |
| Slippage | stocks 2 bps, crypto 5 bps | same |

  The placeholders are deliberately pessimistic. At 200 EUR, minimum fees dominate: a 20 EUR position with a 1 EUR minimum costs 5% per side. The scenarios exist to make that visible.
- **LLM cost:** the daily Anthropic API cost is recorded in `api_costs`, converted to EUR, and deducted in full from **every** A and B ledger when reporting net returns. It is not split across scenarios, because a real single account would pay all of it.
- Starting capital is set per scenario (200 / 500 / 1000 EUR).

## 6. Telegram approval flow
- One message per proposal: symbol, action, target weight, stop, confidence, the rationale, and the current price.
- Inline buttons: ✅ Approve, ❌ Reject. On reject, a second tap picks a reason: `disagree`, `too risky`, `no time to check`, `other`. The `no time to check` rate is itself a key metric, because it shows when approval has turned into rubber-stamping.
- One daily summary after fills: what filled, the equity of A, B and C, and whether any stops were hit.
- No "approve all" button. The point of B is a per-trade decision.

## 7. Data model (Postgres)
- `instruments(symbol, asset_class, active_from, active_to)`
- `bars_daily(symbol, date, open, high, low, close, volume, source)`
- `runs(id, started_at, model, prompt_version, input_snapshot jsonb, raw_response jsonb, attempts, status, cost_usd)`
- `proposals(id, run_id, symbol, action, target_weight, stop_loss_pct, horizon_days, confidence, rationale, original jsonb, validator_status, validator_notes)`
- `approvals(proposal_id, decision[approve|reject|timeout], reason, decided_at)`
- `scenarios(id, name, capital_eur, risk_profile, cost_profile, config_hash, active_from, active_to, is_primary)`. Scenarios are only ever deactivated, never edited: changing a parameter creates a new scenario row.
- `scenario_adjustments(scenario_id, proposal_id, adjusted_weight, rule_fired)`
- `fills(id, scenario_id, portfolio[A|B|C], proposal_id null, symbol, side, qty, price_usd, fx_rate, fees_eur, filled_at, fill_source[internal|alpaca])`
- `positions_daily(scenario_id, portfolio, date, symbol, qty, avg_price_eur, market_value_eur, stop_price)`
- `equity_daily(scenario_id, portfolio, date, equity_eur, cash_eur, drawdown)`
- `fx_daily(date, eurusd, source)`
- `api_costs(date, input_tokens, output_tokens, usd, eur)`
- `schema_migrations`, using the same custom-runner pattern as STAIR (filename, sha256, applied_at)

Everything is append-only except the daily snapshots. A proposal is written **before** any outcome exists, and its timestamp is the proof.

## 8. Reporting
Weekly (Sunday) Telegram message plus a markdown file in `reports/`:
- **Primary scenario first**, on its own: for A, B and C, net return, annualized volatility, Sharpe, max drawdown, number of trades, win rate, average win vs average loss, total fees and total API cost as a percentage of capital.
- A minus B for the primary scenario, which is the value of Alejandro's filter.
- Then the scenario grid as one compact table: rows are capital × risk profile, columns are B's net return, C's net return, B − C, and fees as % of capital. The table is titled "descriptive only, not selected results".
- Fee drag: the primary scenario under `zero_commission` vs `eu_small_account`.
- Approval stats: approve, reject and timeout rates, and the reject-reason breakdown.
- API cost to date.
- Always shown: days elapsed and a line saying that under about 120 trading days, the numbers are not evidence of skill.

## 9. Phases and evidence gates
Each phase ends with a gate checked against hard evidence (psql counts, logs, HTTP codes), never against an agent's self-report.

**Phase 0: accounts and skeleton**
Set up the Alpaca paper account and API keys, an Anthropic API key with a monthly spend limit, the new Telegram bot, the repo, the compose project with Postgres, and the migrations runner.
Gate: `psql` shows the migrations applied; the bot answers `/ping`; the Alpaca `/v2/account` endpoint returns 200 with the paper flag.

**Phase 1: data, ledgers and benchmark (no LLM)**
Build ingestion for the 18 instruments plus the ECB EUR/USD rate, feature computation, `config/scenarios.yaml` loading, the fill engine, both cost profiles, daily mark-to-market, and the C benchmarks for every (capital, cost profile) pair running live.
Gate: 5 consecutive daily runs with no gaps (`bars_daily` count per symbol matches trading days, `fx_daily` has one row per ECB business day); one C ledger reconciles by hand for one day (qty × price_usd ÷ eurusd + cash); crypto has 7 bars per week, stocks 5; a unit test shows a 20 EUR trade under `eu_small_account` paying the 1 EUR minimum, not 5 bps; adding a scenario to the YAML and restarting creates its ledgers with no code change.

**Phase 2: Claude proposer + validator, portfolio A only**
Build the proposer, validation with retries, risk clipping, and A trading automatically. Telegram gets read-only proposal messages.
Gate: 5 runs with `runs.status` all `ok` or explained; tests feed deliberately rule-breaking proposals (over-weight, no stop, crypto cap, unknown symbol) and each one is clipped or dropped with the rule logged; `api_costs` filled; no fill for A before its proposal's `created_at`.

**Phase 3: approval flow, portfolio B, Alpaca mirror**
Build the Telegram buttons and reasons, timeout handling, B's ledger, and the Alpaca paper orders for B.
Gate: one approve, one reject and one timeout each end up in `approvals` with the right status; A and B fills for the same approved proposal have the same internal price; Alpaca order IDs are logged for B.

**Phase 4: reports and monitoring**
Build the weekly report, healthchecks.io ping, and 🔴 alerts for validation failure, missing data and the kill switch.
Gate: one weekly report generated from real data; a simulated missing-bar failure produces a 🔴 alert.

**Phase 5 (optional, after 4 weeks live): random baseline R**
R makes random proposals with the same frequency, sizes and stops as A. If A can't beat R, Claude is adding noise, not insight.

## 10. Evaluation plan (pre-registered, written before any results)
- **Months 0–3:** a learning run. Fix bugs, observe behavior, don't conclude anything about profitability.
- **Month 6:** first real review. B's net return and Sharpe are compared with C's, and A minus B shows the filter's effect.
- **All criteria below are evaluated on the primary scenario only** (200 EUR / base / eu_small_account). Results from other scenarios can explain *why* (for example, "fees ate the edge at 200 EUR but not at 1000 EUR"), but they can't turn a "no" into a "yes".
- **Month 12:** decision point. "Worth considering with real money" requires **all** of the following:
  1. B beats C net of all costs, including API costs.
  2. B's max drawdown is no worse than C's.
  3. A beats R.
  4. The `no time to check` rate is under 20%.
  
  Otherwise the conclusion is "no edge demonstrated", which is a valid and useful result.
- Any change to prompts, the universe or the rules starts a new `prompt_version`. Results are reported per version and never pooled across versions.

## 11. Things to know before ever going live (out of scope for v1)
- EU retail investors generally can't buy US-domiciled ETFs like SPY with real money, because of the EU's key-information-document rules. A live version would need UCITS equivalents, and possibly a different broker (for example IBKR).
- Taxes on gains in Finland, and crypto regulation under MiCA, would need looking into first.

## 12. Instructions for the implementing agent
- Work phase by phase. Don't start a phase until the previous gate has passed and Alejandro has confirmed it.
- Show evidence for every gate: the exact command plus its output. Prefer outputs that return counts or booleans over copied strings.
- Never place orders anywhere except the Alpaca **paper** endpoint (`paper-api.alpaca.markets`). Hard-fail at startup if the configured base URL is anything else.
- Keep secrets in `.env`, which is gitignored. Commit a `.env.example`.
- Don't run git commands. Alejandro makes all commits himself. Propose commit boundaries and messages in conventional format, in English.
- Don't touch anything in the STAIR repo or the `stair` compose project on the server.