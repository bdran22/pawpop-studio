#!/usr/bin/env python3
"""Final motion gate on the EP004 MASTER file itself.
Samples 9 frames evenly inside several windows (slot regions) and measures
adjacent grayscale frame differences (same thresholds as crew/qc_motion.py).
Windows cover: hook, 9-pass pingpong S22, K34 trio, K36 stars, K37 endcard.
"""
import json, subprocess, sys, os
from PIL import Image, ImageChops

MASTER = "/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna/PAWPOP_EP004_Longform_1080x1920.mp4"
OUT = "/tmp/pawpop_master_motion"
THRESH_PAIR, THRESH_MEAN, N = 0.35, 0.60, 9

WINDOWS = {
    "S01_hook(0-9s)": (0.0, 9.0),
    "S22_pingpong9x(~236-245s)": (236.0, 245.0),
    "S39_K34_trio(~426-436s)": (426.0, 436.0),
    "S43_K36_stars(~469-477s)": (469.0, 477.0),
    "S44_endcard(~477-481.7s)": (477.5, 481.5),
}

def window_motion(name, t0, t1):
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))
    dur = t1 - t0
    fps_arg = (N - 1) / dur
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t0), "-t", str(dur),
         "-i", MASTER, "-vf", f"fps={fps_arg:.4f},scale=48:85,format=gray",
         os.path.join(OUT, "f%02d.png")], check=True)
    files = sorted(os.listdir(OUT))
    if len(files) < 2:
        return name, [], 0.0, True
    diffs = []
    for a, b in zip(files, files[1:]):
        ia = Image.open(os.path.join(OUT, a))
        ib = Image.open(os.path.join(OUT, b))
        d = ImageChops.difference(ia, ib)
        px = list(d.getdata())
        diffs.append(sum(px) / len(px))
    mean_all = sum(diffs) / len(diffs)
    static = all(d < THRESH_PAIR for d in diffs) or mean_all < THRESH_MEAN
    return name, diffs, mean_all, static

fails = 0
for name, (t0, t1) in WINDOWS.items():
    nm, diffs, mean_all, static = window_motion(name, t0, t1)
    verdict = "STATIC-BLOCK" if static else "MOTION-OK"
    if static:
        fails += 1
    print(f"{verdict:13s} | mean {mean_all:6.2f} | min {min(diffs):5.2f} | {nm}")
    if static:
        print("   diffs:", [round(d, 2) for d in diffs])
print("MASTER MOTION GATE:", "FAIL" if fails else "PASS (0 static windows)")
sys.exit(1 if fails else 0)
