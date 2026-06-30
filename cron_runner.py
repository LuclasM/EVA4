#!/usr/bin/env python3
"""
EVA4 cron runner — system-level scheduler for nightly reflection and user-defined tasks.

Add to crontab (crontab -e):
    * * * * * /usr/bin/python3 /home/luclas/EVA4/cron_runner.py >> /home/luclas/EVA4/data/sessions/logs/cron.log 2>&1

This script is called every minute. It does nothing if an interactive EVA4 session
is currently running (checked via PID file).
"""
import datetime
import os
import sqlite3
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import DATA_DIR, DB_PATH

_PID_FILE    = os.path.join(DATA_DIR, "eva4.pid")
_ACTIVE_FILE = os.path.join(DATA_DIR, "last_active")
_LOG_DIR     = os.path.join(DATA_DIR, "sessions", "logs")
_EVA_PY      = os.path.join(BASE_DIR, "eva.py")

_DAY_NAMES   = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _eva_running() -> bool:
    if not os.path.isfile(_PID_FILE):
        return False
    try:
        pid = int(open(_PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _idle_hours() -> float:
    if not os.path.isfile(_ACTIVE_FILE):
        return float("inf")
    try:
        ts   = open(_ACTIVE_FILE).read().strip()
        last = datetime.datetime.fromisoformat(ts)
        return (datetime.datetime.now() - last).total_seconds() / 3600
    except Exception:
        return float("inf")


def _launch(extra_args: list[str], log_suffix: str) -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(_LOG_DIR, f"{log_suffix}_{stamp}.log")
    with open(log_path, "w") as lf:
        subprocess.Popen(
            [sys.executable, _EVA_PY] + extra_args,
            stdout=lf, stderr=lf,
            cwd=BASE_DIR, start_new_session=True,
        )
    _log(f"launched {extra_args} → {log_path}")


def _check_reflection(now: datetime.datetime) -> None:
    if now.hour != 4 or now.minute != 0:
        return
    idle = _idle_hours()
    if idle >= 1:
        _log(f"nightly reflection triggered (idle {idle:.1f}h)")
        _launch(["--reflect"], "reflect")
    else:
        _log(f"nightly reflection skipped (idle only {idle:.1f}h)")


def _check_scheduled(now: datetime.datetime) -> None:
    if not os.path.isfile(DB_PATH):
        return
    today    = _DAY_NAMES[now.weekday()]
    now_hhmm = now.strftime("%H:%M")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE enabled=1"
        ).fetchall()
    except Exception:
        conn.close()
        return

    for row in rows:
        if row["schedule_time"] != now_hhmm:
            continue
        if row["schedule_type"] == "weekly" and row["schedule_day"] != today:
            continue
        last_run = row["last_run"] or ""
        if last_run.startswith(now.strftime("%Y-%m-%d %H:%M")):
            continue  # already triggered this minute
        conn.execute(
            "UPDATE scheduled_tasks SET last_run=? WHERE id=?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
        )
        conn.commit()
        _log(f"scheduled task [{row['id']}] '{row['name']}' triggered")
        _launch(["--run", row["goal"]], f"sched_{row['id']}")

    conn.close()


def _log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main():
    if _eva_running():
        return  # interactive session active — skip
    now = datetime.datetime.now()
    _check_reflection(now)
    _check_scheduled(now)


if __name__ == "__main__":
    main()
