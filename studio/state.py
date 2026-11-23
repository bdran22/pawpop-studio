"""Pipeline state (committed back to the repo after every run).

state.json tracks each episode: production status, QC result, scheduled
publish time, video id. The scheduler's invariant: exactly one episode is
scheduled per Dubai slot (09:00 / 21:00) — a new video every 12 hours.
"""
import datetime
import json
from pathlib import Path

from . import config

DUBAI_TZ = datetime.timezone(datetime.timedelta(hours=config.DUBAI_UTC_OFFSET))


def load() -> dict:
    if config.STATE_PATH.exists():
        return json.loads(config.STATE_PATH.read_text())
    return {"episodes": {}, "log": []}


def save(st: dict):
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps(st, indent=1, ensure_ascii=False))


def ep(st: dict, ep_id: str) -> dict:
    return st["episodes"].setdefault(ep_id, {"status": "new"})


def log(st: dict, msg: str):
    now = datetime.datetime.now(DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    st.setdefault("log", []).append(f"[{now}] {msg}")
    st["log"] = st["log"][-60:]
    print(f"[state] {msg}", flush=True)


def now_dubai() -> datetime.datetime:
    return datetime.datetime.now(DUBAI_TZ)
