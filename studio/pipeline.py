"""PAWPOP STUDIO pipeline orchestrator (runs inside GitHub Actions).

One invocation = one 12-hour cycle:
  1. selftest          — which engines/credentials are actually alive
  2. pick work         — the episode closest to completion first
  3. produce           — VO -> keyframes -> animation clips (parallel engines)
  4. assemble          — zero-static assembly + audio mix + captions
  5. QC                — honest gate >= 8.5
  6. publish           — resumable upload with publishAt = next Dubai slot
  7. commit-back       — state.json is committed by the workflow

Usage:
  python -m studio.pipeline --run
  python -m studio.pipeline --status
"""
import argparse
import datetime
import json
import time
from pathlib import Path

from . import assemble, config, engines, keyframes, meta, qc, state, storygen, timeline, vo, yt


# ------------------------------------------------------------------ selftest
def selftest() -> dict:
    rep = {"zai": False, "kie": None, "pixverse_upload": None, "youtube": False}
    conf = config.zai_conf()
    if conf.get("apiKey"):
        try:
            from . import zai
            out = Path("/tmp/zai_tts_probe.wav")
            zai.tts("Pawpop selftest.", out, retries=1)
            rep["zai"] = out.exists() and out.stat().st_size > 4000
        except Exception as e:
            rep["zai_error"] = str(e)[:120]
    if config.KIE_API_KEY:
        rep["kie"] = engines.kie_credit()
    if config.PIXVERSE_API_KEY:
        try:
            engines.pv_upload_image  # noqa: B018 - availability of key only
            rep["pixverse_upload"] = "key-present (verified on first use)"
        except Exception:
            pass
    if config.YT_REFRESH_TOKEN:
        try:
            stats = yt.channel_stats()
            rep["youtube"] = bool(stats.get("id"))
            rep["channel"] = stats
        except Exception as e:
            rep["youtube_error"] = str(e)[:160]
    print("SELFTEST:", json.dumps(rep, default=str)[:600], flush=True)
    return rep


# ------------------------------------------------------------------ episode IO
def load_story(ep_id: str) -> dict:
    p = config.REPO_ROOT / "stories" / f"{ep_id.lower()}.json"
    if p.exists():
        return json.loads(p.read_text())
    return storygen.generate(ep_id)


def ep_dir(ep_id: str) -> Path:
    d = config.ASSETS / ep_id.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def missing_jobs(story: dict, d: Path, kf_index: dict) -> list[dict]:
    jobs = []
    seen = set()
    for shot in story["shots"]:
        kf = shot["kf"]
        out = d / "clips" / f"{kf}.mp4"
        if out.exists() and out.stat().st_size > 200000:
            continue
        if kf in seen or kf not in kf_index:
            continue
        seen.add(kf)
        jobs.append({"shot_id": kf, "keyframe": kf_index[kf],
                     "motion": shot["motion"], "out": str(out)})
    return jobs


# ------------------------------------------------------------------ one episode
def produce_episode(story: dict, st: dict, deadline: float) -> dict:
    ep_id, d = story["ep"], ep_dir(story["ep"])
    s = state.ep(st, ep_id)
    s["status"] = "producing"

    # 1) narration
    if time.time() < deadline:
        vo.ensure_vo(story, d, min(deadline, time.time() + 40 * 60))

    # 2) keyframes
    kf_index = keyframes.ensure_keyframes(story, d, min(deadline, time.time() + 60 * 60))

    # 3) animation clips (multi-engine, parallel)
    (d / "clips").mkdir(exist_ok=True)
    jobs = missing_jobs(story, d, kf_index)
    if jobs and time.time() < deadline:
        engines.produce_shots(jobs, d, deadline, max_workers=2)

    # 4) completion check
    need_kf = {shot["kf"] for shot in story["shots"]}
    have = {p.stem for p in (d / "clips").glob("*.mp4")
            if p.stat().st_size > 200000}
    missing = sorted(need_kf - have)
    s["missing_shots"] = missing
    s["clips_ready"] = len(have & need_kf)
    s["clips_total"] = len(need_kf)
    return s


def finish_episode(story: dict, st: dict) -> dict:
    """Assemble + audio + QC + meta. Returns episode state."""
    ep_id, d = story["ep"], ep_dir(story["ep"])
    s = state.ep(st, ep_id)
    build = config.WORK_DIR / ep_id.lower() / "build"
    build.mkdir(parents=True, exist_ok=True)

    tl = timeline.build(story, d)
    slots, pieces_dir = tl["slots"], build / "pieces"
    pieces_dir.mkdir(exist_ok=True)

    # resolve clip paths (unique keyframes only)
    kf_clip = {p.stem: str(p) for p in (d / "clips").glob("*.mp4")}
    pieces = []
    for i, slot in enumerate(slots):
        pieces.append(assemble.make_piece(i, slot, tl, pieces_dir, kf_clip.get(slot["kf"])))
    print(f"pieces: {len(pieces)}", flush=True)

    blends = {}
    for i in range(len(slots) - 1):
        if slots[i]["trans"] == "fade":
            blends[i] = assemble.make_blend(f"{i:02d}", pieces[i], pieces[i + 1],
                                            pieces_dir, tl["fade"], tl["fps"])

    silent, vdur = assemble.stage_concat(tl, pieces, blends, build)
    wav = build / "audio_mix.wav"
    if not wav.exists():
        assemble.build_audio(tl, d, wav)
    srt = build / "captions.srt"
    assemble.write_srt(tl, srt)

    master = d / f"PAWPOP_{ep_id}_Longform_1080x1920.mp4"
    assemble.stage_master(silent, wav, srt, master)

    report = qc.run_qc(master, tl)
    (d / "qc_report.json").write_text(json.dumps(report, indent=1))
    s["qc"] = report
    if not report["pass"]:
        s["status"] = "qc_failed"
        state.log(st, f"{ep_id} QC {report['score']} < {report['gate']} — NOT uploaded")
        return s

    m = meta.build(story, tl, config.CHANNEL_HANDLE)
    (d / "upload_metadata.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))
    s["status"] = "ready"
    s["master"] = str(master)
    state.log(st, f"{ep_id} ready — QC {report['score']}, "
                  f"{report['measured']['duration_s']}s, {report['measured']['size_mb']}MB")
    return s


def publish_episode(story: dict, st: dict) -> dict:
    ep_id, d = story["ep"], ep_dir(story["ep"])
    s = state.ep(st, ep_id)
    if s.get("status") != "ready" or not s.get("master"):
        return s
    slot = yt.next_slot_utc()
    meta_path = d / "upload_metadata.json"
    m = json.loads(meta_path.read_text())
    vid = yt.upload(s["master"], m, slot)
    s["status"] = "scheduled"
    s["video_id"] = vid
    s["publish_at"] = slot.strftime("%Y-%m-%d %H:%M Dubai")
    state.log(st, f"{ep_id} SCHEDULED {vid} -> {s['publish_at']}")
    return s


# ------------------------------------------------------------------ run
def pick_episode(st: dict) -> dict | None:
    """Most-advanced episode that is not finished yet; then the next story.

    Status 'delegated' = another producer owns it (duplicate-upload
    protection): never touched here.
    """
    skip = ("scheduled", "published", "delegated")
    order = {"qc_failed": 0, "producing": 1, "ready": 2}
    cands = [(order.get(s.get("status", "new"), 1), ep_id)
             for ep_id, s in st["episodes"].items() if s.get("status") not in skip]
    cands.sort()
    return cands[0][1] if cands else None


def next_ep_id(st: dict) -> str:
    nums = [int(e[2:]) for e in st["episodes"] if e.startswith("EP") and e[2:].isdigit()]
    n = (max(nums) if nums else 4) + 1
    while f"EP{n:03d}" in st["episodes"]:
        n += 1
    return f"EP{n:03d}"


def run(produce_budget_min: int | None = None):
    config.ensure_dirs()
    st = state.load()
    selftest()

    produce_min = produce_budget_min if produce_budget_min is not None else config.PRODUCE_BUDGET_MIN
    prod_deadline = time.time() + produce_min * 60

    # -- finish the most advanced episode -------------------------------
    target = pick_episode(st)
    if target is None:
        target = next_ep_id(st)
        st["episodes"][target] = {"status": "new"}
        state.log(st, f"new episode {target} started")
    try:
        story = load_story(target)
    except Exception as e:
        st["episodes"][target]["status"] = "story_failed"
        st["episodes"][target]["error"] = str(e)[:300]
        state.log(st, f"{target} story authoring failed: {str(e)[:160]}")
        state.save(st)
        print(f"RUN RESULT: {target} story_failed (will retry next cycle)", flush=True)
        return st
    s = produce_episode(story, st, prod_deadline)
    if not s.get("missing_shots") and s.get("status") in ("producing", "qc_failed", "ready"):
        s = finish_episode(story, st)
        if s.get("status") == "ready":
            s = publish_episode(story, st)

    # -- keep a second episode in production if budget remains ----------
    if time.time() < prod_deadline - 20 * 60 and s.get("status") in ("scheduled", "ready"):
        nxt = next_ep_id(st)
        if f"EP{nxt[2:]}" not in st["episodes"]:
            try:
                story2 = load_story(nxt)
                st["episodes"][nxt] = {"status": "new"}
                produce_episode(story2, st, prod_deadline)
            except Exception as e:
                state.log(st, f"{nxt} story authoring failed: {str(e)[:160]}")

    state.save(st)
    return st


def status():
    st = state.load()
    print(json.dumps(st, indent=1, ensure_ascii=False)[:4000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--produce-budget-min", type=int, default=None)
    a = ap.parse_args()
    if a.status:
        status()
    elif a.run:
        try:
            run(a.produce_budget_min)
        except Exception as e:
            # never leave the cycle without an honest record + saved state
            try:
                st = state.load()
                state.log(st, f"cycle crashed: {type(e).__name__}: {str(e)[:250]}")
                state.save(st)
            except Exception:
                pass
            print(f"CYCLE CRASHED: {type(e).__name__}: {str(e)[:300]}", flush=True)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
