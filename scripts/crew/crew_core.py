#!/usr/bin/env python3
"""PAWPOP CREW — core collaboration protocol.

Implements the studio task board that the eight professional agents use to
hand work to each other. Every agent:
  1. claims a task from the board (atomic rename, no double-claim),
  2. does its work and writes artifacts under the episode folder,
  3. appends a handoff record (from -> to, artifact, verdict),
  4. updates the board so the next agent in the chain picks it up.

The board is a single JSON file per episode: crew_board.json. It is the single
source of truth for "who did what, with which engine, and did QC pass".
No agent may fabricate results: every write carries real file evidence.
"""
import json, os, time, uuid

CREW_ROOT = "/home/z/my-project/PAWPOP_STUDIO/crew"
BOARD_VERSION = 1

# The eight professional agents (Arabic display names for reports).
ROLES = {
    "showrunner": {"ar": "المنتج التنفيذي", "en": "Showrunner",
                   "owns": "slate, slot assignment, escalation"},
    "director":   {"ar": "المخرج", "en": "Director",
                   "owns": "story, shot list, keyframe briefs"},
    "animator":   {"ar": "وحدة التحريك", "en": "Animator Unit",
                   "owns": "real animated clips via ENGINE-A/C/D"},
    "sound":      {"ar": "مهندس الصوت", "en": "Sound Designer",
                   "owns": "VO, music bed, SFX, ducking mix"},
    "editor":     {"ar": "المونتير", "en": "Editor",
                   "owns": "timeline, assembly, transitions, master"},
    "qc":         {"ar": "مراقب الجودة", "en": "QC Inspector",
                   "owns": "zero-static gate, rubric >= 8.5, honest verdicts"},
    "publisher":  {"ar": "الناشر", "en": "Publisher",
                   "owns": "YouTube upload with publishAt, SEO metadata"},
    "archivist":  {"ar": "أمين الأرشيف", "en": "Archivist",
                   "owns": "GitHub releases, state sync, durability"},
}

# Standard production chain for one episode.
CHAIN = ["director", "animator", "qc", "editor", "qc", "publisher", "archivist"]


def board_path(ep_dir: str) -> str:
    return os.path.join(ep_dir, "crew_board.json")


def load_board(ep_dir: str) -> dict:
    p = board_path(ep_dir)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"version": BOARD_VERSION, "ep": os.path.basename(ep_dir),
            "tasks": [], "handoffs": [], "updated": now()}


def save_board(ep_dir: str, board: dict) -> None:
    board["updated"] = now()
    tmp = board_path(ep_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(board, f, indent=1, ensure_ascii=False)
    os.replace(tmp, board_path(ep_dir))


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def add_task(ep_dir: str, task_id: str, kind: str, payload: dict,
             assigned_to: str, depends_on: list = None) -> None:
    """Register one unit of work on the board (idempotent)."""
    board = load_board(ep_dir)
    if any(t["id"] == task_id for t in board["tasks"]):
        return
    board["tasks"].append({
        "id": task_id, "kind": kind, "payload": payload,
        "assigned_to": assigned_to, "depends_on": depends_on or [],
        "state": "pending", "attempts": 0, "created": now(),
        "started": None, "finished": None, "engine": None,
        "artifact": None, "verdict": None, "error": None,
    })
    save_board(ep_dir, board)


def claim_task(ep_dir: str, role: str, kinds: list = None) -> dict | None:
    """Atomically claim the oldest pending task for this role whose deps are met."""
    board = load_board(ep_dir)
    by_id = {t["id"]: t for t in board["tasks"]}
    for t in board["tasks"]:
        if t["state"] != "pending" or t["assigned_to"] != role:
            continue
        if kinds and t["kind"] not in kinds:
            continue
        if any(by_id.get(d, {}).get("state") not in ("done", "skipped")
               for d in t["depends_on"]):
            continue
        t["state"] = "running"
        t["attempts"] += 1
        t["started"] = now()
        save_board(ep_dir, board)
        return t
    return None


def finish_task(ep_dir: str, task_id: str, verdict: str, artifact: str = None,
                engine: str = None, error: str = None) -> None:
    """Record the outcome. verdict in {done, fail, blocked, skipped}."""
    board = load_board(ep_dir)
    for t in board["tasks"]:
        if t["id"] == task_id:
            t["state"] = {"done": "done", "fail": "pending_retry" if t["attempts"] < 3 else "failed",
                          "blocked": "blocked", "skipped": "skipped"}[verdict]
            if verdict == "fail" and t["attempts"] >= 3:
                t["state"] = "failed"
            t["finished"] = now()
            t["artifact"] = artifact
            t["engine"] = engine
            t["verdict"] = verdict
            t["error"] = error
            break
    save_board(ep_dir, board)


def handoff(ep_dir: str, from_role: str, to_role: str, note: str,
            artifact: str = None) -> None:
    """Append a collaboration record: who passed what to whom, and why."""
    board = load_board(ep_dir)
    board["handoffs"].append({"t": now(), "from": from_role, "to": to_role,
                              "note": note[:300], "artifact": artifact})
    save_board(ep_dir, board)


def summary(ep_dir: str) -> dict:
    board = load_board(ep_dir)
    states = {}
    for t in board["tasks"]:
        states[t["state"]] = states.get(t["state"], 0) + 1
    return {"ep": board.get("ep"), "tasks": states,
            "handoffs": len(board["handoffs"]),
            "last_handoff": board["handoffs"][-1] if board["handoffs"] else None}


if __name__ == "__main__":
    import sys
    print(json.dumps(summary(sys.argv[1]), indent=1, ensure_ascii=False))
