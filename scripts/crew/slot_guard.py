#!/usr/bin/env python3
"""PAWPOP SLOT GUARD — mechanical guarantee for the 12h publishing rhythm.

Tick mode (--tick, called by cron every 2h):
  1. If a detached upload is already running -> print its progress, exit.
  2. Find an episode with status "ready" (QC >= gate), master file present,
     not yet scheduled and not claimed -> claim it, spawn detached uploader
     with publishAt = next Dubai slot (>= 60 min away), exit.
  3. Otherwise print one honest status line (no fabricated claims).

Detached uploader (--upload EPxxx): runs outside the cron turn (uploads are
50+ min). Writes result to state/slot_guard.json and updates state.json:
  - success: status=scheduled, video_id, publish_at
  - failure: status back to "ready", claim cleared, error recorded

Rules kept from the studio charter:
  - never upload an episode that did not pass the QC gate
  - never fabricate: the status line reports real file counts
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/z/my-project/pawpop-studio")
sys.path.insert(0, str(REPO))

GUARD_DIR = REPO / "state"
RESULT_PATH = GUARD_DIR / "slot_guard.json"
LOCK_PATH = Path("/tmp/pawpop_slot_upload.lock")
PROGRESS_PATH = Path("/tmp/pawpop_slot_upload.progress")
DUBAI = datetime.timezone(datetime.timedelta(hours=4))
GATE = 8.5


def dubai_now() -> datetime.datetime:
    return datetime.datetime.now(DUBAI)


def next_slot(min_minutes: int = 60) -> datetime.datetime:
    now = dubai_now()
    for add_days in (0, 1):
        for hour in (9, 21):
            cand = (now + datetime.timedelta(days=add_days)).replace(
                hour=hour, minute=0, second=0, microsecond=0)
            if cand - now > datetime.timedelta(minutes=min_minutes):
                return cand
    raise AssertionError


def load_state() -> dict:
    return json.loads((REPO / "state" / "state.json").read_text())


def save_state(st: dict):
    (REPO / "state" / "state.json").write_text(
        json.dumps(st, indent=2, ensure_ascii=False))


def upload_running() -> bool:
    if not LOCK_PATH.exists():
        return False
    age = time.time() - LOCK_PATH.stat().st_mtime
    if age > 3 * 3600:  # stale lock (upload never takes >3h)
        LOCK_PATH.unlink(missing_ok=True)
        return False
    return True


def find_ready(st: dict):
    for ep_id, s in st.get("episodes", {}).items():
        if s.get("status") != "ready":
            continue
        if s.get("video_id") or s.get("upload_claimed_at"):
            continue
        qc = (s.get("qc") or {})
        if float(qc.get("score", 0)) < GATE:
            continue
        master = s.get("master")
        if not master or not Path(master).exists() or Path(master).stat().st_size < 10_000_000:
            continue
        return ep_id, s
    return None, None


def ep_progress(st: dict, ep_id: str) -> str:
    s = st.get("episodes", {}).get(ep_id, {})
    clips = s.get("clips_ready")
    total = s.get("clips_total")
    if clips is None:
        d = REPO / "work" / "assets" / ep_id.lower() / "clips"
        clips = len([p for p in d.glob("*.mp4") if p.stat().st_size > 200000]) if d.exists() else 0
        total = 38
    return f"{ep_id} clips {clips}/{total}"


def tick():
    now = dubai_now().strftime("%Y-%m-%d %H:%M")
    slot = next_slot()
    if upload_running():
        prog = ""
        if PROGRESS_PATH.exists():
            prog = PROGRESS_PATH.read_text()[-160:].replace("\n", " | ")
        print(f"SLOT GUARD {now}: upload RUNNING for next slot {slot:%m-%d %H:%M} Dubai; {prog}")
        return
    st = load_state()
    ep_id, s = find_ready(st)
    if ep_id:
        s["upload_claimed_at"] = dubai_now().strftime("%Y-%m-%d %H:%M")
        save_state(st)
        slot_str = slot.strftime("%Y-%m-%d %H:%M")
        with open(LOCK_PATH, "w") as f:
            f.write(f"{ep_id} -> {slot_str}")
        subprocess.Popen(
            ["python3", str(Path(__file__).resolve()), "--upload", ep_id,
             "--publish-at", slot_str],
            stdout=open("/tmp/pawpop_slot_upload.log", "ab"),
            stderr=subprocess.STDOUT, start_new_session=True)
        print(f"SLOT GUARD {now}: UPLOADING {ep_id} -> publishAt {slot_str} Dubai (detached)")
        return
    prog = ep_progress(st, "EP005")
    print(f"SLOT GUARD {now}: no QC-passed master ready "
          f"(next slot {slot:%m-%d %H:%M} Dubai; {prog}; honest miss if slot passes)")


def run_upload(ep_id: str, publish_at: str):
    RESULT_PATH.parent.mkdir(exist_ok=True)
    d = REPO / "work" / "assets" / ep_id.lower()
    st = load_state()
    s = st["episodes"].get(ep_id, {})
    master = s.get("master", "")
    meta_path = d / "upload_metadata.json"
    if not master or not Path(master).exists() or not meta_path.exists():
        RESULT_PATH.write_text(json.dumps({
            "ts": dubai_now().isoformat(), "ep": ep_id,
            "error": "master or metadata missing"}))
        s.pop("upload_claimed_at", None)
        save_state(st)
        LOCK_PATH.unlink(missing_ok=True)
        return
    PROGRESS_PATH.write_text(f"starting {ep_id} at {dubai_now():%H:%M}")
    r = subprocess.run(
        ["python3", "/home/z/my-project/scripts/yt_upload_resumable.py",
         "--video", master, "--meta", str(meta_path),
         "--publish-at", publish_at],
        capture_output=True, text=True, timeout=3 * 3600)
    PROGRESS_PATH.write_text((r.stdout + r.stderr)[-400:])
    vid = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("OK "):
            vid = line.split()[1].strip()
    if vid:
        st = load_state()
        s = st["episodes"].setdefault(ep_id, {})
        s["status"] = "scheduled"
        s["video_id"] = vid
        s["publish_at"] = f"{publish_at} Dubai"
        from datetime import datetime as _dt
        s.pop("upload_claimed_at", None)
        st.setdefault("log", []).append(
            f"[{_dt.now(DUBAI):%Y-%m-%d %H:%M}] SLOT GUARD: {ep_id} uploaded {vid} "
            f"-> publishAt {publish_at} Dubai")
        save_state(st)
        RESULT_PATH.write_text(json.dumps({
            "ts": dubai_now().isoformat(), "ep": ep_id, "video_id": vid,
            "publish_at": publish_at}))
    else:
        st = load_state()
        s = st["episodes"].get(ep_id, {})
        s.pop("upload_claimed_at", None)   # release claim; retry next tick
        save_state(st)
        RESULT_PATH.write_text(json.dumps({
            "ts": dubai_now().isoformat(), "ep": ep_id, "error": "no video id",
            "tail": (r.stdout + r.stderr)[-400:]}))
    LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--upload", type=str)
    ap.add_argument("--publish-at", dest="publish_at", type=str)
    a = ap.parse_args()
    if a.tick:
        tick()
    elif a.upload:
        run_upload(a.upload, a.publish_at)
