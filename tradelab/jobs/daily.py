"""The daily run (22:30 UTC): ingest → features → scenarios → fills → C → marks.

Usage:
    python -m tradelab.jobs.daily                       # normal run, t = now
    python -m tradelab.jobs.daily --backfill --ingest-only   # one-off history load
    python -m tradelab.jobs.daily --as-of 2026-09-24    # replay as if run at 22:30 UTC that day

Every run re-ingests the last 7 days (upsert), recomputes features and all
ledger snapshots, records itself in job_runs and sends a Telegram ✅/⚠️/🔴.
--as-of is for replaying a fresh database; time may never go backwards
relative to an earlier run in the same database.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import date, datetime, timedelta

import psycopg2

from tradelab import config as config_mod
from tradelab import features, ledger, notify, scenarios, settings
from tradelab.calendars import UTC, daily_run_time, last_complete_day
from tradelab.data import ecb, ingest
from tradelab.data.alpaca import AlpacaClient

log = logging.getLogger("tradelab.jobs.daily")
GAP_LOOKBACK_DAYS = 30
BIG_MOVE = 0.25


def _status(summary: dict) -> str:
    if summary.get("error"):
        return "error"
    marks = summary.get("marks", {})
    if marks.get("missing_latest"):
        return "error"
    warn_keys = ("gaps", "rejected", "crypto_issues", "feature_nulls", "big_moves", "waiting",
                 "unmarked")
    if any(summary.get(k) for k in warn_keys):
        return "warn"
    return "ok"


def run(conn, cfg: settings.Settings, t: datetime, *, backfill: bool = False,
        ingest_only: bool = False, client: AlpacaClient | None = None,
        crypto_price=None, fx_fetch=ecb.fetch, scenarios_path=None) -> tuple[str, dict]:
    """Execute one run at logical time t. Returns (status, summary)."""
    job = "backfill" if ingest_only else "daily"
    with conn, conn.cursor() as cur:
        cur.execute("SELECT max(run_at) FROM job_runs WHERE job = 'daily' AND status <> 'error'")
        latest = cur.fetchone()[0]
        if latest is not None and t < latest:
            raise SystemExit(f"refusing to run at {t:%Y-%m-%d %H:%M} UTC: a run at "
                             f"{latest:%Y-%m-%d %H:%M} UTC already exists (time only moves forward)")
        cur.execute("INSERT INTO job_runs (job, run_at, git_sha) VALUES (%s, %s, %s) RETURNING id",
                    (job, t, os.environ.get("TRADELAB_GIT_SHA")))
        run_id = cur.fetchone()[0]

    summary: dict = {"run_id": run_id, "job": job, "t": t.isoformat(),
                     "mark_date": last_complete_day(t).isoformat()}
    try:
        client = client or AlpacaClient(cfg.alpaca_key_id, cfg.alpaca_secret_key, cfg.alpaca_base_url)
        crypto_price = crypto_price or client.crypto_first_minute

        with conn, conn.cursor() as cur:          # commit data even if a later step fails
            rep = ingest.ingest(cur, client, t, backfill=backfill, fx_fetch=fx_fetch)
            summary["ingest"] = {k: rep[k] for k in ("window", "last_session", "bars", "fx")}
            summary["rejected"] = rep["rejected"]
            summary["crypto_issues"] = [i for i in rep["crypto_issues"] if "only" not in i]
            summary["crypto_partial_days"] = [i for i in rep["crypto_issues"] if "only" in i]

        with conn, conn.cursor() as cur:
            cur.execute("SELECT min(date) FROM bars_daily")
            data_start = cur.fetchone()[0] or t.date()
            since = max(data_start, t.date() - timedelta(days=GAP_LOOKBACK_DAYS))
            summary["gaps"] = ingest.find_gaps(cur, t, since)
            summary["feature_nulls"] = features.recompute_all(cur, last_complete_day(t))
            summary["big_moves"] = _big_moves(cur, since)

            if not ingest_only:
                scfg = config_mod.load(scenarios_path)
                summary["scenarios"] = scenarios.sync(cur, scfg, t)
                summary["config_path"] = scfg.path
                market = ledger.Market(cur, t)
                ledgers = ledger.active_ledgers(cur, scfg)
                fills = ledger.execute_pending(cur, ledgers, market, scfg, crypto_price)
                summary["fills"] = fills["filled"]
                summary["skipped"] = fills["skipped"]
                summary["waiting"] = fills["waiting"]
                summary["rebalances_decided"] = ledger.decide_rebalances(cur, ledgers, market, scfg)
                marks = ledger.mark_all(cur, ledgers, market)
                last = market.last_day.isoformat()
                marks["missing_latest"] = sorted(k for k, v in marks["unmarked"].items() if last in v)
                summary["marks"] = marks
                summary["unmarked"] = marks["unmarked"]
                summary["primary"] = _primary_line(cur, scfg, market.last_day)
    except Exception as exc:     # report every failure, then re-raise for the exit code
        conn.rollback()
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc(limit=8)
        _finish(conn, run_id, "error", summary)
        exc.summary = summary   # type: ignore[attr-defined]
        raise
    status = _status(summary)
    _finish(conn, run_id, status, summary)
    return status, summary


def _big_moves(cur, since: date) -> list[str]:
    """Stock/ETF daily moves beyond BIG_MOVE: prices are raw, so a split shows up here."""
    cur.execute("""SELECT f.symbol, f.date, f.ret_1d FROM features_daily f
                   JOIN instruments i ON i.symbol = f.symbol AND i.active_to IS NULL
                   WHERE i.asset_class <> 'crypto' AND f.date >= %s AND abs(f.ret_1d) > %s
                   ORDER BY f.date, f.symbol""", (since, BIG_MOVE))
    return [f"{sym} {d} {r:+.0%} (split or bad print? prices are unadjusted)"
            for sym, d, r in cur.fetchall()]


def _primary_line(cur, scfg, d: date) -> str | None:
    p = scfg.primary
    cur.execute("""
        SELECT s.name, e.portfolio, e.equity_eur::float8
        FROM equity_daily e JOIN scenarios s ON s.id = e.scenario_id
        WHERE s.active_to IS NULL AND e.date = %s
          AND (s.name = %s OR s.name = %s)
        ORDER BY e.portfolio""", (d, p.name, f"bench_c{int(p.capital_eur)}_{p.cost_profile}"))
    rows = cur.fetchall()
    if not rows:
        return None
    return " · ".join(f"{pf} {eq:.2f}" for _, pf, eq in rows) + f" EUR ({p.name})"


def _finish(conn, run_id: int, status: str, summary: dict) -> None:
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE job_runs SET status = %s, finished_at = now(), summary = %s WHERE id = %s",
                    (status, json.dumps(summary, default=str), run_id))


def format_message(status: str, s: dict) -> str:
    icon = notify.ICONS[status]
    lines = [f"{icon} TradeLab {s['job']} · marks {s['mark_date']} (run {s['run_id']})"]
    if s.get("error"):
        lines.append(f"error: {s['error']}")
    ing = s.get("ingest")
    if ing:
        b, f = ing["bars"], ing["fx"]
        lines.append(f"bars {ing['window'][0]}→{ing['window'][1]}: +{b['inserted']} new, "
                     f"{b['updated']} repaired, {b['unchanged']} unchanged")
        lines.append(f"fx: +{f['inserted']} new, {f['updated']} repaired")
        if b["changed"]:
            lines.append("repaired: " + ", ".join(b["changed"][:6]))
    if s.get("gaps"):
        lines.append("gaps (missing, not filled): " + "; ".join(
            f"{k} {', '.join(v[-3:])}{' …' if len(v) > 3 else ''}" for k, v in s["gaps"].items()))
    for key, label in (("rejected", "rejected"), ("crypto_issues", "crypto"),
                       ("big_moves", "big move")):
        if s.get(key):
            lines.append(f"{label}: " + "; ".join(s[key][:4]))
    if s.get("feature_nulls"):
        lines.append("NaN features: " + "; ".join(
            f"{k}: {', '.join(v)}" for k, v in list(s["feature_nulls"].items())[:5]))
    sc = s.get("scenarios")
    if sc and (sc["created"] or sc["deactivated"]):
        lines.append(f"scenarios: +{len(sc['created'])} created, {len(sc['deactivated'])} deactivated")
    for key, label in (("fills", "filled"), ("skipped", "skipped"), ("waiting", "waiting")):
        if s.get(key):
            lines.append(f"{label}: " + "; ".join(s[key][:4]) + (" …" if len(s[key]) > 4 else ""))
    if s.get("rebalances_decided"):
        lines.append(f"C rebalance decided: {len(s['rebalances_decided'])} ledgers")
    m = s.get("marks")
    if m:
        lines.append(f"ledgers marked: {m['ledgers']}"
                     + (f", missing latest mark: {', '.join(m['missing_latest'])}"
                        if m["missing_latest"] else ""))
    if s.get("primary"):
        lines.append(f"primary: {s['primary']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", help=f"ingest {ingest.BACKFILL_DAYS} days")
    ap.add_argument("--ingest-only", action="store_true", help="data + features only, no ledgers")
    ap.add_argument("--as-of", type=date.fromisoformat, help="replay at 22:30 UTC on this date")
    ap.add_argument("--no-notify", action="store_true", help="don't send Telegram")
    args = ap.parse_args(argv)

    cfg = settings.load()
    cfg.require("database_url", "alpaca_key_id", "alpaca_secret_key")
    now = datetime.now(UTC)
    t = daily_run_time(args.as_of) if args.as_of else now
    if t > now:
        sys.exit(f"--as-of {args.as_of}: 22:30 UTC that day is in the future")

    job = "backfill" if args.ingest_only else "daily"
    try:
        conn = psycopg2.connect(cfg.database_url)
        try:
            status, summary = run(conn, cfg, t, backfill=args.backfill,
                                  ingest_only=args.ingest_only)
        finally:
            conn.close()
    except (Exception, SystemExit) as exc:   # every failure still produces a 🔴
        log.exception("run failed")
        status = "error"
        summary = getattr(exc, "summary", None) or {
            "run_id": "-", "job": job, "mark_date": last_complete_day(t).isoformat(),
            "error": f"{type(exc).__name__}: {exc}"}

    text = format_message(status, summary)
    print(text)
    if not args.no_notify:
        notify.send(cfg, text)
    return 0 if status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
