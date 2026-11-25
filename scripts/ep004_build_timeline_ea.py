#!/usr/bin/env python3
"""Build the ALL-ENGINE-A timeline for EP004 (owner directive 2026-11-23:
no static-image scenes anywhere). Every VO segment gets its own slot backed
by a real cogvideox-3 clip. Reused keyframes become distinct sub-shots via
in-point offset + center zoom crop. Slot duration respects the clip supply:
trim (speed 1.0) -> mild slow-mo (>=0.80) -> ping-pong passes.

Output: ep004_timeline.json (schema-compatible with ep004_assemble.py).
The previous mixed EB timeline is backed up as ep004_timeline_eb_backup.json.
"""
import json, math, os, subprocess, sys

EP = "/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna"
CLIPS = os.path.join(EP, "clips")
AUD = os.path.join(EP, "audio")
FPS, W, H = 30, 1080, 1920

# seg -> (kf_id, reuse_index, zoom, src_in)   reuse 0 = primary clip
MAP = {
    "seg01": ("K01_hook", 0, 1.0, 0.0),
    "seg02": ("K02_boxdawn", 0, 1.0, 0.0),
    "seg03": ("K02_boxdawn", 1, 1.30, 3.2),
    "seg04": ("K03_theory", 0, 1.0, 0.0),
    "seg05": ("K04_sniff", 0, 1.0, 0.0),
    "seg06": ("K05_squeak", 0, 1.0, 0.0),
    "seg07": ("K06_lunaarrive", 0, 1.0, 0.0),
    "seg08": ("K07_lunasits", 0, 1.0, 0.0),
    "seg09": ("K08_guard", 0, 1.0, 0.0),
    "seg10": ("K11_labelpoint", 0, 1.0, 0.0),
    "seg11": ("K11_labelpoint", 1, 1.38, 4.6),
    "seg12": ("K10_maxjudge", 0, 1.0, 0.0),
    "seg13": ("K12_planpose", 0, 1.0, 0.0),
    "seg14": ("K09_paweyes", 0, 1.0, 0.0),
    "seg15": ("K10_maxjudge", 1, 1.24, 2.6),
    "seg16": ("K13_sitclaim", 0, 1.0, 0.0),
    "seg17": ("K14_scritch", 0, 1.0, 0.0),
    "seg18": ("K15_maxhide", 0, 1.0, 0.0),
    "seg19": ("K16_earlisten", 0, 1.0, 0.0),
    "seg20": ("K17_theorymonster", 0, 1.0, 0.0),
    "seg21": ("K18_lunatap", 0, 1.0, 0.0),
    "seg22": ("K17_theorymonster", 1, 1.32, 3.4),
    "seg23": ("K10_judge2", 0, 1.0, 0.0),          # variant; fallback K10 reuse
    "seg24": ("K14_scritch", 1, 1.26, 4.2),
    "seg25": ("K19_whisker", 0, 1.0, 0.0),
    "seg26": ("K20_towerreveal", 0, 1.0, 0.0),
    "seg27": ("K21_mousebell", 0, 1.0, 0.0),
    "seg28": ("K22_lunacheek", 0, 1.0, 0.0),
    "seg29": ("K23_sadwalk", 0, 1.0, 0.0),
    "seg30": ("K24_queenclimb", 0, 1.0, 0.0),
    "seg31": ("K25_topperch", 0, 1.0, 0.0),
    "seg32": ("K26_lookdown", 0, 1.0, 0.0),
    "seg33": ("K27_taptp", 0, 1.0, 0.0),
    "seg35": ("K28_climbstart", 0, 1.0, 0.0),
    "seg36": ("K30_maxcoach", 0, 1.0, 0.0),
    "seg37": ("K29_wobble", 0, 1.0, 0.0),
    "seg38": ("K32_hammock", 0, 1.0, 0.0),
    "seg39": ("K34_thronestrio", 0, 1.0, 0.0),
    "seg40": ("K34_thronestrio", 1, 1.20, 4.0),
    "seg41": ("K35_tails", 0, 1.0, 0.0),
    "seg42": ("K36_sunsetstars", 0, 1.0, 0.0),
    "seg43": ("K33_maxfort", 0, 1.0, 0.0),
    "seg44": ("K36_sunsetstars", 1, 1.12, 6.4),
    "seg34": ("K37_endcard", 0, 1.0, 0.0),         # branded outro, last slot
}
ORDER = [f"seg{i:02d}" for i in range(1, 34)] + \
        [f"seg{i:02d}" for i in range(35, 45)] + ["seg34"]

MOODS = [
    ("seg01", "warm", 0.55), ("seg09", "comedy", 0.80),
    ("seg17", "dramatic", 0.55), ("seg23", "bright", 0.90),
    ("seg27", "rain", 0.50), ("seg30", "warm", 0.60),
    ("seg35", "comedy", 0.75), ("seg38", "bright", 0.85),
    ("seg41", "lullaby", 0.45), ("seg44", "outro", 0.55),
]

FADE_SEGS = {"seg09", "seg17", "seg23", "seg29", "seg30", "seg35", "seg38", "seg41", "seg44", "seg34"}

SFX_PLAN = [
    ("seg06", "squeak", 0.9), ("seg10", "whoosh", 0.0), ("seg10", "woof", 1.1),
    ("seg14", "boop", 0.4), ("seg17", "knock", 0.5), ("seg19", "purr", 1.2),
    ("seg21", "boop", 0.2), ("seg21", "boop", 0.7), ("seg21", "ding", 1.6),
    ("seg25", "whoosh", 0.0), ("seg26", "chime", 0.3), ("seg27", "squeak", 0.7),
    ("seg29", "thud", 1.4), ("seg33", "boop", 0.25), ("seg33", "boop", 0.75),
    ("seg36", "woof", 0.6), ("seg37", "thud", 1.1), ("seg39", "chime", 0.4),
    ("seg41", "sparkle", 0.3), ("seg34", "chime", 0.4), ("seg34", "ding", 1.0),
    ("seg34", "sparkle", 2.2),
]

VARIANT_FALLBACK = {"K10_judge2": "K10_maxjudge", "K11_label2": "K11_labelpoint",
                    "K17_shock2": "K17_theorymonster", "K02_box2": "K02_boxdawn"}


def probe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def main():
    vo_dur = {}
    for i in range(1, 45):
        p = os.path.join(AUD, f"seg{i:02d}.wav")
        d = probe_dur(p)
        if not d:
            raise SystemExit(f"missing VO {p}")
        vo_dur[f"seg{i:02d}"] = d
    print("VO total: %.1fs over 44 segments" % sum(vo_dur.values()))

    clip_dur = {}
    for kf in sorted({m[0] for m in MAP.values()} | set(VARIANT_FALLBACK)):
        p = os.path.join(CLIPS, kf + ".mp4")
        if os.path.exists(p):
            clip_dur[kf] = probe_dur(p) or 10.29
    missing = [k for k in {m[0] for m in MAP.values()} if k not in clip_dur]
    print("clips present:", len(clip_dur), "missing:", missing)

    slots, vo_placements, srt, sfx = [], [], [], []
    sfx_map = {}
    for seg, kind, off in SFX_PLAN:
        sfx_map.setdefault(seg, []).append((kind, off))

    t, n = 0.0, 0
    starts = {}
    for seg in ORDER:
        kf, reuse, zoom, src_in = MAP[seg]
        kf_missing = False
        if kf not in clip_dur:
            base = VARIANT_FALLBACK.get(kf, kf)
            kf = base
            reuse = 1 if reuse == 0 else reuse
            zoom = max(zoom, 1.18)
            src_in = src_in or 2.8
            if kf not in clip_dur:
                print(f"  WARN: no clip yet for {seg} ({kf}) - slot kept, assembly blocked until generated")
                kf_missing = True
            else:
                kf_missing = False
        vd = vo_dur[seg]
        pad = 1.5 if seg in ("seg01", "seg34") else 1.25
        dur = round(vd + pad, 3)
        cd = clip_dur.get(kf, 10.29)
        # Supply-adaptive in-point: never start past the clip can serve.
        src_in = min(src_in, max(0.0, cd - 1.5))
        avail = max(0.8, cd - src_in)
        flow = {"src_in": round(src_in, 3), "zoom": zoom}
        if avail >= dur:
            flow.update(kind="trim", speed=1.0)
        elif avail / dur >= 0.80:
            flow.update(kind="slow", speed=round(avail / dur, 4))
        else:
            # Ping-pong passes as needed to cover the FULL narration slot:
            # a slot is never shorter than vo+pad, so captions/VO never overlap.
            # Reverse playback is still real animation (no freeze frames).
            passes = max(2, math.ceil(dur / (avail * 0.95)))
            speed = min(1.0, round(dur / (passes * avail), 4))
            if speed < 0.55 and passes < 6:
                passes += 1
                speed = min(1.0, round(dur / (passes * avail), 4))
            flow.update(kind="pingpong", speed=speed, passes=passes)
        n += 1
        sid = f"S{n:02d}"
        starts[seg] = round(t, 3)
        slots.append(dict(id=sid, kf=kf,
                          clip=os.path.join(CLIPS, kf + ".mp4") if not kf_missing else None,
                          dur=dur, vo=[seg], trans="fade" if seg in FADE_SEGS else "cut",
                          reuse=reuse, flow=flow))
        vo_at = round(t + 0.40, 3)
        vo_placements.append(dict(file=f"{seg}.wav", at=vo_at, slot=sid))
        srt.append(dict(n=len(srt) + 1, t1=vo_at, t2=round(vo_at + vd + 0.25, 3), text=seg))
        for kind, off in sfx_map.get(seg, ()):
            sfx.append(dict(kind=kind, at=round(t + off + 0.4, 3)))
        t += dur

    chapters = [dict(t=0.0, mode="warm", intensity=0.55)]
    for seg, mode, inten in MOODS[1:]:
        chapters.append(dict(t=starts[seg], mode=mode, intensity=inten))

    total = round(sum(s["dur"] for s in slots), 3)
    tl = dict(ep="EP004", fps=FPS, w=W, h=H, fade=0.35, total=total,
              slots=slots, vo=sorted(vo_dur), vo_placements=vo_placements,
              srt=srt, sfx=sfx, chapters=chapters,
              starts=[starts[ORDER[i]] for i in range(len(ORDER))],
              engine="ENGINE-A full-animation (owner directive 2026-11-23: no static scenes)")

    old = os.path.join(EP, "ep004_timeline.json")
    bak = os.path.join(EP, "ep004_timeline_eb_backup.json")
    if os.path.exists(old) and not os.path.exists(bak):
        os.rename(old, bak)
        print("old mixed timeline backed up")
    json.dump(tl, open(old, "w"), indent=1)
    print(f"NEW ALL-EA TIMELINE: {len(slots)} slots, total {total:.1f}s "
          f"({int(total//60)}:{total%60:04.1f}), chapters {len(chapters)}, sfx {len(sfx)}")
    miss = [s["id"] + ":" + s["kf"] for s in slots if not s["clip"] or not os.path.exists(s["clip"])]
    print("slots still missing clips:", len(miss), miss[:8])


if __name__ == "__main__":
    main()
