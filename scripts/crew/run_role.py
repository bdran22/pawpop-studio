#!/usr/bin/env python3
"""PAWPOP CREW — role shift runner.

Run one professional agent's shift against an episode:
  python3 run_role.py animator  [--ep EP_DIR] [--rounds N] [--seconds S]
  python3 run_role.py qc        [--ep EP_DIR]
  python3 run_role.py showrunner [--ep EP_DIR]

Roles map onto the proven production modules:
  animator    -> ep004_engine_a.py fleet loop + engine_router capacity guard
                 (+ qc_motion on every freshly downloaded clip)
  qc          -> qc_motion gate over all timeline clips + verdict
  showrunner  -> slate report: what is ready, what is blocked, next slot
The board (crew_board.json) records every handoff, so any agent can resume.
"""
import argparse, json, os, subprocess, sys, time

sys.path.insert(0, "/home/z/my-project/scripts")
sys.path.insert(0, "/home/z/my-project/scripts/crew")
import crew_core as cc
import engine_router as router

DEFAULT_EP = "/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna"


def load_missing_kf(ep_dir: str) -> list:
    """Keyframes of the episode that still have no clip on disk."""
    st_p = os.path.join(ep_dir, "engine_a_status.json")
    missing = []
    if os.path.exists(st_p):
        st = json.load(open(st_p))
        missing = [k for k, v in st.items() if v.get("state") not in ("ok",)]
    return missing


def role_animator(ep: str, rounds: int, seconds: int) -> dict:
    av = router.available_engines("sandbox")
    print("[router]", json.dumps(av))
    if not av["engine_a"] and not av["engine_c"] and not av["engine_d"]:
        print(router.escalation_message(ep))
        return {"blocked": True}
    missing = load_missing_kf(ep)
    print(f"[animator] {len(missing)} clips missing: {missing[:8]}...")
    if not missing:
        return {"done": "nothing to animate"}
    # Drive the proven ENGINE-A batch driver (it honors the lock + 429 backoff).
    did = 0
    for i in range(rounds):
        before = len([k for k in load_missing_kf(ep)])
        if before == 0:
            break
        try:
            subprocess.run(
                [sys.executable, "/home/z/my-project/scripts/ep004_engine_a.py",
                 "--deadline", "8", "--task-timeout", "540"],
                timeout=seconds, check=False)
        except subprocess.TimeoutExpired:
            print(f"[animator] round {i+1} hit shift time limit")
        after = len(load_missing_kf(ep))
        did += max(before - after, 0)
        print(f"[animator] round {i+1}: missing {before} -> {after}")
        if after == before:
            print("[animator] no progress this round (engine throttled?)")
            break
    # QC every new clip the moment it lands.
    still = load_missing_kf(ep)
    fresh = [k for k in json.load(open(os.path.join(ep, "engine_a_status.json")))
             if k not in still]
    if fresh:
        rep = subprocess.run([sys.executable, "/home/z/my-project/scripts/crew/qc_motion.py",
                              "--ep", ep, "--clips"] + fresh, capture_output=True, text=True)
        print(rep.stdout[-1500:])
    return {"animated": did, "still_missing": len(still)}


def role_qc(ep: str) -> dict:
    tl = json.load(open(os.path.join(ep, "ep004_timeline.json")))
    kfs = []
    seen = set()
    for s in tl["slots"]:
        if s["kf"] not in seen:
            seen.add(s["kf"])
            kfs.append(s["kf"])
    rep = subprocess.run([sys.executable, "/home/z/my-project/scripts/crew/qc_motion.py",
                          "--ep", ep, "--clips"] + kfs,
                         capture_output=True, text=True)
    out = rep.stdout
    print(out[-3000:] if len(out) > 3000 else out)
    try:
        return json.loads(out)
    except Exception:
        return {"error": "qc output not json"}


def role_showrunner(ep: str) -> dict:
    tl = json.load(open(os.path.join(ep, "ep004_timeline.json")))
    have = {f[:-4] for f in os.listdir(os.path.join(ep, "clips"))
            if f.endswith(".mp4") and "_landscape" not in f and "_vertical" not in f}
    need = {s["kf"] for s in tl["slots"]}
    missing = sorted(need - have)
    av = router.available_engines("sandbox")
    slate = {
        "ep": os.path.basename(ep),
        "duration": tl.get("total"),
        "unique_shots": len(need),
        "clips_ready": len(need) - len(missing),
        "missing": missing,
        "engines": av,
        "next_slot": "21:00 Dubai today if complete+QC>=8.5, else tomorrow 09:00",
        "rule": "zero static scenes; never publish incomplete",
    }
    print(json.dumps(slate, indent=1, ensure_ascii=False))
    return slate


ROLES = {"animator": role_animator, "qc": role_qc, "showrunner": role_showrunner}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("--ep", default=DEFAULT_EP)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--seconds", type=int, default=560)
    a = ap.parse_args()
    res = ROLES[a.role](a.ep, a.rounds, a.seconds) if a.role == "animator" \
        else ROLES[a.role](a.ep)
    with open(f"/tmp/crew_{a.role}_last.json", "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False, default=str)
