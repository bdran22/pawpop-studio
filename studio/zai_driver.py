"""Rate-limit-aware z.ai (ENGINE-A) batch driver for EP005 clips.

Strategy: under a hard submission rate limit, POLLING is free but SUBMITS
are throttled. So this driver serializes submissions with adaptive spacing,
checkpoints every task_id into engine_status.json (survives restarts), then
polls all live tasks in parallel and downloads finished mp4s into clips/.

Usage (resumable, idempotent):
    python3 -m studio.zai_driver --budget-min 9
"""
import argparse
import json
import time
from pathlib import Path

from . import config, engines, zai
from .pipeline import ep_dir, load_story, missing_jobs


def _status_path(d: Path) -> Path:
    return d / "engine_status.json"


def _load_status(d: Path) -> dict:
    p = _status_path(d)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_status(d: Path, status: dict):
    _status_path(d).write_text(json.dumps(status, indent=1, ensure_ascii=False))


def run(budget_min: float = 9.0, submit_spacing: float = 100.0) -> dict:
    story = load_story("EP005")
    d = ep_dir("EP005")
    (d / "clips").mkdir(exist_ok=True)
    kf_index = {p.stem: p for p in (d / "keyframes").glob("*")
                if p.suffix in (".png", ".jpg")}
    jobs = missing_jobs(story, d, kf_index)
    status = _load_status(d)
    conf = config.zai_conf()
    if not conf.get("apiKey"):
        print("[zai_driver] no apiKey", flush=True)
        return status
    deadline = time.time() + budget_min * 60

    # upgrade pass: K-engine (animated camera) placeholders are eligible for
    # re-production with real animation once the rate limit allows
    have = {j["shot_id"] for j in jobs}
    for shot in story["shots"]:
        sid = shot["kf"]
        if sid in have or sid not in kf_index:
            continue
        clip = d / "clips" / f"{sid}.mp4"
        if not (clip.exists() and clip.stat().st_size > 200000):
            continue
        rec = status.get(sid) or {}
        if rec.get("engine") in ("A", "E", "F") and rec.get("state") in ("ok", "running"):
            continue  # real animation already
        if rec.get("state") == "running" and rec.get("task_id"):
            continue  # already in flight
        jobs.append({"shot_id": sid, "keyframe": str(kf_index[sid]),
                     "motion": shot.get("motion") or shot.get("kf_prompt") or "",
                     "out": str(clip), "upgrade": True})
    print(f"[zai_driver] {len(jobs)} jobs (incl. upgrades)", flush=True)
    deadline = time.time() + budget_min * 60

    # ---------- phase 1: submit pending shots, spaced & checkpointed ----------
    submitted = 0
    next_submit_ok = 0.0
    spacing = submit_spacing
    for job in jobs:
        if time.time() > deadline - 60:
            break
        rec = status.get(job["shot_id"]) or {}
        if rec.get("engine") == "A" and rec.get("state") == "running" and rec.get("task_id"):
            continue  # already in flight
        if rec.get("state") == "ok" and rec.get("engine") in ("A", "E", "F"):
            continue  # real animation done
        wait = next_submit_ok - time.time()
        if wait > 0 and wait < 240:
            print(f"[zai_driver] spacing {wait:.0f}s", flush=True)
            time.sleep(wait)
        if time.time() > deadline - 60:
            break
        try:
            task_id = zai.video_submit(conf, job["keyframe"], job["motion"],
                                       backoff_budget=min(240.0,
                                                          deadline - time.time() - 60))
            status[job["shot_id"]] = {"engine": "A", "state": "running",
                                      "task_id": task_id, "out": job["out"],
                                      "submitted_at": time.time()}
            _save_status(d, status)
            submitted += 1
            print(f"[zai_driver] SUBMIT {job['shot_id']} -> {task_id}", flush=True)
            if spacing > 40:
                spacing = max(40.0, spacing * 0.8)  # gentle recovery
            next_submit_ok = time.time() + spacing
        except zai.ZaiError as e:
            msg = str(e)
            print(f"[zai_driver] {job['shot_id']} submit fail: {msg[:120]}", flush=True)
            if "429" in msg:
                spacing = min(300.0, spacing * 1.6)
                next_submit_ok = time.time() + min(120.0, max(30.0, deadline - time.time() - 90))
                break  # let polls run; next tick retries
            status[job["shot_id"]] = {"engine": "A", "state": "failed",
                                      "err": msg[:200]}
            _save_status(d, status)
            if time.time() > deadline - 60:
                break

    # ---------- phase 2: poll running tasks in parallel, download ----------
    running = {sid: rec for sid, rec in status.items()
               if rec.get("engine") == "A" and rec.get("state") == "running"
               and rec.get("task_id")}
    print(f"[zai_driver] polling {len(running)} in-flight (submitted this tick: {submitted})", flush=True)
    if running:
        jobs_by_id = {j["shot_id"]: j for j in jobs}
        poll_jobs = []
        for sid, rec in running.items():
            out = rec.get("out") or (jobs_by_id.get(sid, {}).get("out"))
            if out:
                poll_jobs.append({"shot_id": sid, "task_id": rec["task_id"],
                                  "out": out})
        t_end = min(deadline - 30, time.time() + 600)

        def poll_one(pj):
            try:
                url = zai.video_poll(conf, pj["task_id"],
                                     max_seconds=max(60, int(t_end - time.time())))
                if time.time() > t_end:
                    return  # keep task in flight, download next tick
                zai.video_download(url, pj["out"])
                status[pj["shot_id"]] = {"engine": "A", "state": "ok",
                                         "file": pj["out"], "done_at": time.time()}
                _save_status(d, status)
                print(f"[zai_driver] DONE {pj['shot_id']}", flush=True)
            except zai.ZaiError as e:
                if "timeout" in str(e).lower():
                    return  # still in flight
                status[pj["shot_id"]] = {"engine": "A", "state": "failed",
                                         "err": str(e)[:200]}
                _save_status(d, status)
                print(f"[zai_driver] FAIL {pj['shot_id']}: {str(e)[:120]}", flush=True)
            except Exception as e:
                print(f"[zai_driver] {pj['shot_id']} err {str(e)[:120]}", flush=True)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(poll_one, poll_jobs))
    _save_status(d, status)
    done = sum(1 for v in status.values() if v.get("state") == "ok")
    clips = len(list((d / "clips").glob("*.mp4")))
    print(f"[zai_driver] tick end: submitted={submitted} clips={clips} ok={done}/{len(jobs)}", flush=True)
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float, default=9.0)
    ap.add_argument("--spacing", type=float, default=100.0)
    a = ap.parse_args()
    run(a.budget_min, a.spacing)


if __name__ == "__main__":
    main()
