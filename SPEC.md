# TradeLab — Claude as a paper-trading analyst, with a human in the loop

Status: spec v3 (1 Oct 2026): five cost profiles over the full scenario grid, primary scenario on `ibkr`, fee engine with per-share fees, fractional flags, free orders, spreads and interest on cash. (v2, 29 Sep 2026: realistic capital in EUR, scenario grid, cost profiles.) Paper money only. No real orders, ever, in this version.

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

Scenarios live in `config/scenarios.yaml`. Adding or removing one is a config change plus a restart, not a code change. Each scenario gets its own A and B ledgers, plus a C benchmark per (capital, cost profile) pair. Default layout, the full grid for every cost profile:

| Dimension | Values |
|---|---|
| Capital (EUR) | 200, 500, 1000 |
| Risk profile | `conservative`, `base`, `aggressive` (section 4) |
| Cost profile (section 5) | `ibkr`, `trade_republic`, `revolut_standard`, `myinvestor`, and `zero_commission`, a fee-free **reference, not a real broker**, kept to measure fee drag |

That is 3 × 3 × 5 = **45 strategy scenarios** (A and B each), plus one C benchmark per (capital, cost profile) pair = **15 benchmark ledgers**: 105 ledgers in total.

**Primary scenario: 200 EUR / base / ibkr** (`c200_base_ibkr`). It is pre-registered, and it alone decides the verdict in section 10. All other scenarios, including the other brokers at 200 EUR / base, are descriptive. With 105 correlated ledgers, the best-looking one will be flattering by chance, so it is never promoted to "the result" after the fact.

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
- **Ledger currency: EUR.** Instruments are priced in USD and converted with the daily ECB EUR/USD reference rate. This applies to all assets, including ETFs and crypto that `ibkr` and `myinvestor` model as EUR-listed products: the USD price stays the price source, so currency moves are part of the result, as they would be with real money.
- **Fee engine.** For each order, per asset class of the cost profile:
  - **Commission** = `clamp(pct × notional + per_share × qty, min, max)`. Each fee schedule has a **currency**: EUR, or USD (IBKR US stocks), which is converted to EUR at the fill's ECB rate. The maximum is an absolute amount and/or a **% of trade value**; if both are set the lower applies, and the maximum is applied after the minimum, so on a tiny trade it wins (IBKR's 1% cap).
  - **FX fee**: a % of the notional on each EUR↔USD conversion, once per order, where the profile charges one.
  - **FX allowance**: `fx_free_eur_per_month` per profile. Conversions (fills in asset classes with an FX fee, buys and sells) are summed in EUR per UTC calendar month **in each ledger**, and the FX fee applies only to the part above the allowance. It resets at the start of every month, like free orders.
  - **Slippage + spread**: `slippage_bps + spread_bps` move the execution price against us, on buys and sells.
  - **Fractional shares**: a per-asset-class flag. When it's false, buys (and partial sells) round down to whole shares, and an order under one share is skipped and logged as `below_one_share`.
  - **Free orders**: `free_orders_per_month` per profile, for the asset classes it lists. The first N fills of each UTC calendar month **in each ledger** pay no commission (the FX fee still applies). The counter resets at the start of every month.
  - **Interest on cash**: `cash_interest_annual_pct`, accrued daily over 365 calendar days (weekends included) on positive cash, from the day after the ledger starts, and stored in `accruals_daily` with kind `interest`. It applies identically to A, B and C.
- **Cost profiles** (in `config/scenarios.yaml`, each with its source and check date in a comment):

| Parameter | ibkr | trade_republic | revolut_standard | myinvestor | zero_commission (reference) |
|---|---|---|---|---|---|
| US stocks fee | USD 0.0035/share, min USD 0.35, max 1% of trade value | EUR 1.00 flat (**TO VERIFY**) | 1 free order/month, then max(0.25%, EUR 1.00) | 0.12%, min EUR 3.00, max EUR 25.00 | 0% |
| ETFs fee | 0.05%, min EUR 1.25, max EUR 29 (Xetra-listed) | EUR 1.00 flat (**TO VERIFY**) | as US stocks (shares the free order) | 0.12%, min EUR 1.00, max EUR 25.00 (EUR-listed UCITS) | 0% |
| Crypto fee | 0.05%, min EUR 1.25, max EUR 29 (Xetra-listed ETP) | EUR 1.00 flat (**TO VERIFY**) | 1.49%, min EUR 1.00 (**TO VERIFY**, unverified placeholder) | 0.12%, min EUR 1.00, max EUR 25.00 (EUR-listed ETP) | 0.25% |
| FX fee | US stocks 0.03%; none on ETFs, crypto | US stocks 0.35% (**TO VERIFY**) | US stocks: first EUR 1,000/month free, then 0.5% (stock trades assumed to use the allowance: **TO VERIFY**); none on ETFs (EUR-listed), crypto | US stocks 0.30%; none on ETFs, crypto | 0% |
| Spread | 0 | crypto 100 bps (**TO VERIFY**) | 0 | 0 | 0 |
| Slippage | stocks/ETFs 2 bps, crypto 5 bps | same | same | same | same |
| Fractional | yes (min USD 1) | yes | yes | ETFs and crypto yes, **US stocks no** | yes |
| `etp_annual_fee_pct` (crypto) | **TO VERIFY** (placeholder 1.5%) | 0 (crypto held directly) | 0 | **TO VERIFY** (placeholder 1.5%) | 0 |
| Cash interest | 0 | 2.3% (**TO VERIFY**) | 0 (not modeled) | 0 | 0 |

  Sources, all checked 2026-10-01:
  - `ibkr`: interactivebrokers.ie: the commissions-stocks page, the fractional-trading page ("IE: Fractional shares are available for all account types", minimum USD 1), and the EU US-stock cost page.
  - `trade_republic`: curvo.eu and brokerchooser.com reviews; the official traderepublic.com pricing page did not load. The EUR 1 fee, 0.35% FX, 1% crypto spread and 2.3% interest are **from reviews, TO VERIFY on traderepublic.com**.
  - `revolut_standard`: help.revolut.com (en-FI) trading fees and currency exchange fees, Standard plan. The weekend FX markup is ignored: fills are on weekdays, and Revolut crypto is in EUR. Crypto 1.49% is an **unverified placeholder**.
  - `myinvestor`: myinvestor.es/inversion/broker (unchanged from 29 Sep 2026).
  - `zero_commission` has no source: it's synthetic, a reference and never a broker recommendation.

  Every **TO VERIFY** value is a placeholder that must be checked before results are read; changing it later creates new scenario rows (section 7).

  `etp_annual_fee_pct` accrues daily over 365 calendar days (weekends included) on the position's EUR market value.

- **Worked example: a 40 EUR US-stock buy** at EUR/USD 1.10 (commission + FX fee; slippage and spread come on top in the price):

| Profile | Commission | FX fee | Total | % of trade |
|---|---|---|---|---|
| ibkr | USD 0.35 minimum = 0.32 EUR | 0.012 EUR | **0.33 EUR** | 0.8% |
| trade_republic | 1.00 EUR | 0.14 EUR | **1.14 EUR** | 2.9% |
| revolut_standard | 1.00 EUR (0.00 for the month's free order) | 0 (within the EUR 1,000 monthly allowance) | **1.00 EUR** (0 free) | 2.5% (0%) |
| myinvestor | 3.00 EUR minimum (not 0.12% = 0.05 EUR) | 0.12 EUR | **3.12 EUR** | 7.8% |
| zero_commission | 0 | 0 | **0** | 0% |

  Under `myinvestor` that order never fills in practice: every stock in the universe trades above about 44 USD, so 40 EUR is less than one share and the order is skipped as `below_one_share`. At 200 EUR, minimum fees and whole shares dominate. The broker comparison exists to make that visible.
- **Known biases** (they apply to every profile and to A, B and C alike unless stated):
  - **No dividends credited.** Prices are raw, not total-return, so dividends are missing for stocks, ETFs and the C benchmark (80% SPY).
  - **Interest only where modeled.** Only `trade_republic` earns interest on cash. Real interest elsewhere (for example IBKR on larger balances, or a Revolut savings pocket) is ignored, which favors `trade_republic` relative to the others. Interest is credited daily and compounds daily, while brokers pay monthly; the difference is negligible at these rates.
  - **Exchange, clearing and regulatory pass-through fees** (IBKR Tiered) are not modeled, so `ibkr` is slightly optimistic.
  - **Values marked TO VERIFY** are unverified placeholders or come from third-party reviews (`trade_republic`), not the broker's own pricing page.
  - **Spread** is a fixed number of bps, not the live quoted spread.
  - **Free orders and the FX allowance** are counted by UTC calendar month, and the broker's own month boundary may differ.
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
Weekly (Sunday) Telegram message plus a markdown file in `reports/`, always in this order:
1. **The primary scenario alone** (200 EUR / base / ibkr): for A, B and C, net return, annualized volatility, Sharpe, max drawdown, number of trades, win rate, average win vs average loss, total fees and total API cost as a percentage of capital; then A minus B, which is the value of Alejandro's filter.
2. **Broker comparison at 200 EUR / base**: one row per cost profile (`ibkr`, `trade_republic`, `revolut_standard`, `myinvestor`, `zero_commission`). Columns: B's net return, C's net return, B − C, fees (commission + FX) as % of capital, interest earned, and orders skipped as `below_one_share`. The `zero_commission` row is labeled "reference, not a broker"; the gap between it and `ibkr` is the primary scenario's fee drag.
3. **Appendix: the full grid**, titled "descriptive only, not selected results": every (capital, risk, cost profile) scenario with the same columns as (2).
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
Gate: 5 consecutive daily runs with no gaps (`bars_daily` count per symbol matches trading days, `fx_daily` has one row per ECB business day); one C ledger reconciles by hand for one day (qty × price_usd ÷ eurusd + cash); crypto has 7 bars per week, stocks 5; a unit test shows a 40 EUR US-stock buy under `myinvestor` paying 3.00 EUR commission (the minimum, not 0.12%) plus 0.12 EUR FX; adding a scenario to the YAML and restarting creates its ledgers with no code change.

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
- **Start date = first daily job_run on the production server.** Every month below counts from it; runs on any other machine don't count.
- **Months 0–3:** a learning run. Fix bugs, observe behavior, don't conclude anything about profitability.
- **Month 6:** first real review. B's net return and Sharpe are compared with C's, and A minus B shows the filter's effect.
- **All criteria below are evaluated on the primary scenario only** (200 EUR / base / ibkr). Results from other scenarios, including the broker comparison, can explain *why* (for example, "fees ate the edge at 200 EUR but not at 1000 EUR", or "it only works under the `zero_commission` reference"), but they can't turn a "no" into a "yes". `zero_commission` is a reference, not a broker, so a result that only holds there is a "no".
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
- Read-only git (status, diff, log) is allowed; anything that writes is Alejandro's. Propose commit boundaries and messages in conventional format, in English.
- Don't touch anything in the STAIR repo or the `stair` compose project on the server.