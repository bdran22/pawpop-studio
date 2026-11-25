#!/usr/bin/env python3
"""Incremental motion-gate runner: probes clips one by one, appends JSONL log."""
import json, os, sys, time

sys.path.insert(0, "/home/z/my-project/scripts/crew")
from qc_motion import probe_clip

EP = "/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna"
LOG = "/tmp/motion_log.jsonl"
OUT = "/home/z/my-project/download/PAWPOP/crew/motion_report_2026-11-25.json"

ids = [l.strip() for l in open("/tmp/clip_ids.txt") if l.strip()]
# canonical only
st = json.load(open(f"{EP}/engine_a_status.json"))
ids = sorted(k for k in st if k in set(ids))
done = set()
if os.path.exists(LOG):
    for line in open(LOG):
        try:
            done.add(json.loads(line)["clip"][:-4])
        except Exception:
            pass

with open(LOG, "a") as log:
    for cid in ids:
        if cid in done:
            continue
        t0 = time.time()
        r = probe_clip(f"{EP}/clips/{cid}.mp4")
        r["secs"] = round(time.time() - t0, 1)
        log.write(json.dumps(r, ensure_ascii=False) + "\n")
        log.flush()
        print(f"{cid}: {r['verdict']} mean={r['mean_diff']} ({r['secs']}s)", flush=True)

# final merge
rows = [json.loads(l) for l in open(LOG)]
by = {r["clip"][:-4]: r for r in rows}
results = [by[c] for c in ids if c in by]
statics = [r for r in results if r["static"]]
rep = {"ep": "EP004", "checked": len(results),
       "motion_ok": len(results) - len(statics), "static_blocked": len(statics),
       "gate": "PASS" if results and not statics else "FAIL",
       "details": results}
with open(OUT, "w") as f:
    json.dump(rep, f, indent=1, ensure_ascii=False)
print("FINAL GATE:", rep["gate"], f"({rep['motion_ok']}/{rep['checked']} ok)", flush=True)
