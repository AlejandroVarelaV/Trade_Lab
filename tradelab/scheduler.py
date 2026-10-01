"""Jobs service: runs the daily job at 22:30 UTC, forever.

Each run is a fresh `python -m tradelab.jobs.daily` subprocess, so a crash or
a leaked connection in one run cannot affect the next. On start-up, if today's
22:30 run is due and no successful run exists for today yet (e.g. the container
was restarted at 22:40), it runs immediately.

Run: python -m tradelab.scheduler
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta

import psycopg2

from tradelab import settings
from tradelab.calendars import UTC, daily_run_time

log = logging.getLogger("tradelab.scheduler")
JOB = [sys.executable, "-m", "tradelab.jobs.daily"]


def next_run(now: datetime) -> datetime:
    today = daily_run_time(now.date())
    return today if now < today else today + timedelta(days=1)


def ran_today(database_url: str, now: datetime) -> bool:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM job_runs WHERE job = 'daily'
                           AND status IN ('ok', 'warn') AND run_at >= %s""",
                        (daily_run_time(now.date()),))
            return cur.fetchone()[0] > 0
    finally:
        conn.close()


def run_job() -> None:
    log.info("starting daily job")
    rc = subprocess.call(JOB)
    log.info("daily job exited with %s", rc)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = settings.load()
    cfg.require("database_url")

    now = datetime.now(UTC)
    try:
        if now >= daily_run_time(now.date()) and not ran_today(cfg.database_url, now):
            log.info("today's 22:30 run is missing: catching up")
            run_job()
    except psycopg2.Error as exc:
        log.error("catch-up check failed: %s", exc)

    while True:
        target = next_run(datetime.now(UTC))
        log.info("next run at %s", target.isoformat())
        while (left := (target - datetime.now(UTC)).total_seconds()) > 0:
            time.sleep(min(left, 300))
        run_job()


if __name__ == "__main__":
    main()
