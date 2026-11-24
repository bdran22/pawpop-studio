#!/usr/bin/env python3
"""PAWPOP CREW — QC Inspector motion gate (zero-static rule).

Owner directive: "الفيديوهات مازالت صور ثابتة" -> the crew must never ship a
static-image scene. This gate measures REAL motion in every clip:

  1. Extract N frames evenly across the clip (grayscale, 48x85 luma).
  2. Mean absolute difference between consecutive frames -> motion score.
  3. A clip is STATIC if every consecutive pair difference is below the
     threshold (a crossfade-only or frozen shot). One moving pair is enough:
     real animation has continuous motion, so we also require the MEAN of all
     pairs to clear the bar (catches single-glitch false passes).

Verdict per clip + episode-level gate: any static clip blocks assembly.
This tool never fabricates: it prints measured numbers.
"""
import argparse, json, os, subprocess, tempfile

THRESH_PAIR = 0.35   # per-pair mean luma diff (0-255 scale)
THRESH_MEAN = 0.60   # mean over all pairs
N_FRAMES = 9


def probe_clip(path: str, n: int = N_FRAMES) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) < 20000:
        return {"clip": path, "exists": False, "static": True, "mean_diff": 0.0,
                "pairs": [], "error": "missing or too small"}
    try:
        with tempfile.TemporaryDirectory() as td:
            # duration
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=60)
            dur = float(r.stdout.strip() or 0)
            if dur <= 0:
                return {"clip": path, "exists": True, "static": True, "mean_diff": 0.0,
                        "pairs": [], "error": "unreadable duration"}
            # extract frames
            fps_expr = f"{max(n - 1, 1)}/{max(dur * 0.98, 0.1)}"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-vf",
                 f"fps={fps_expr},scale=48:85,format=gray", "-vsync", "0",
                 os.path.join(td, "f%03d.png")],
                capture_output=True, timeout=180)
            frames = sorted(f for f in os.listdir(td) if f.endswith(".png"))
            if len(frames) < 3:
                return {"clip": path, "exists": True, "static": True, "mean_diff": 0.0,
                        "pairs": [], "error": f"only {len(frames)} frames extracted"}
            # pairwise luma diff via PIL
            from PIL import Image, ImageChops, ImageStat
            diffs = []
            for a, b in zip(frames, frames[1:]):
                ia = Image.open(os.path.join(td, a))
                ib = Image.open(os.path.join(td, b))
                st = ImageStat.Stat(ImageChops.difference(ia, ib))
                diffs.append(round(st.mean[0], 3))
            mean_all = round(sum(diffs) / len(diffs), 3)
            static = all(d < THRESH_PAIR for d in diffs) or mean_all < THRESH_MEAN
            return {"clip": os.path.basename(path), "exists": True, "duration": round(dur, 2),
                    "frames": len(frames), "pairs": diffs, "mean_diff": mean_all,
                    "static": static, "verdict": "STATIC-BLOCK" if static else "MOTION-OK"}
    except Exception as e:
        return {"clip": os.path.basename(path), "exists": True, "static": True,
                "mean_diff": 0.0, "pairs": [], "error": repr(e)[:200]}


def gate_episode(ep_dir: str, clips: list) -> dict:
    """Run the motion gate over every clip; episode passes only if ALL move."""
    results = [probe_clip(os.path.join(ep_dir, "clips", c + ".mp4")) for c in clips]
    statics = [r for r in results if r["static"]]
    return {"ep": os.path.basename(ep_dir), "checked": len(results),
            "motion_ok": len(results) - len(statics), "static_blocked": len(statics),
            "gate": "PASS" if not statics else "FAIL",
            "details": results}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", default="/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna")
    ap.add_argument("--clips", nargs="*", help="clip ids; default = all *.mp4 in clips/")
    ap.add_argument("--out", default=None, help="write JSON report here")
    a = ap.parse_args()
    if a.clips:
        rep = gate_episode(a.ep, a.clips)
    else:
        ids = sorted(f[:-4] for f in os.listdir(os.path.join(a.ep, "clips"))
                     if f.endswith(".mp4") and "_landscape" not in f and "_vertical" not in f)
        rep = gate_episode(a.ep, ids)
    line = json.dumps(rep, indent=1, ensure_ascii=False)
    print(line)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
