"""Timeline builder: story JSON + real VO durations -> full timeline.

EP004 carries an authored timeline_override (exact slots, flows, durs) so its
render is identical to the studio-authored plan. EP005+ are laid out
generically: shot duration = narration duration + breathing pad, clips
reused ping-pong when the shot is longer than one 10s generation.
"""
import json
import wave
from pathlib import Path

from . import config

PAD_AFTER = 1.7          # silence after narration inside a shot
VO_OFFSET = 0.45         # narration starts slightly after the shot begins
OUTRO_EXTRA = 2.0


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def _flow_for(dur: float, reuse_idx: int) -> dict:
    """Choose the clip flow for a shot of given duration."""
    if dur <= 9.7:
        src_in = round(0.15 + 0.55 * (reuse_idx % 4), 2)
        return {"kind": "trim", "src_in": src_in, "zoom": 1.0, "speed": 1.0}
    if dur <= 19.5:
        return {"kind": "pingpong", "src_in": round(0.3 + 0.8 * (reuse_idx % 3), 2),
                "zoom": 1.0, "speed": 1.0, "passes": 2}
    return {"kind": "pingpong", "src_in": 0.4, "zoom": 1.0, "speed": 0.9, "passes": 3}


def build(story: dict, ep_dir: Path) -> dict:
    vo_dir = ep_dir / "vo"
    vo = story["vo"]
    override = story.get("timeline_override")

    # ---- real narration durations -------------------------------------
    seg_dur = {seg: wav_seconds(vo_dir / f"{seg}.wav") for seg in vo}
    missing = [s for s, d in seg_dur.items() if d <= 0.2]
    if missing:
        raise RuntimeError(f"VO missing for: {missing[:6]}")

    if override:
        slots = json.loads(json.dumps(override))
        starts, t = [], 0.0
        for s in slots:
            starts.append(round(t, 2))
            t += s["dur"]
        total = round(t, 2)
    else:
        slots, starts, t = [], [], 0.0
        reuse_count: dict[str, int] = {}
        prev_kf = None
        for shot in story["shots"]:
            seg = shot["seg"]
            dur = seg_dur[seg] + PAD_AFTER + (OUTRO_EXTRA if seg == "seg34" else 0.0)
            kf = shot["kf"]
            if kf == prev_kf:
                reuse_count[kf] = reuse_count.get(kf, 1) + 1
            else:
                reuse_count[kf] = 0
            flow = _flow_for(dur, reuse_count[kf])
            flow["zoom"] = float(shot.get("zoom", flow["zoom"]))
            slots.append({"id": shot["id"], "kf": kf, "clip": None,
                          "dur": round(dur, 2), "vo": [seg],
                          "trans": shot.get("trans", "cut"),
                          "reuse": 1 if reuse_count[kf] else 0, "flow": flow})
            starts.append(round(t, 2))
            t += dur
            prev_kf = kf
        total = round(t, 2)

    # ---- placements, srt ----------------------------------------------
    vo_placements = []
    srt = []
    for i, s in enumerate(slots):
        seg = s["vo"][0]
        vo_placements.append({"file": f"{seg}.wav", "at": round(starts[i] + VO_OFFSET, 2)})
        srt.append({"n": i + 1, "text": seg, "str": vo[seg],
                    "t1": round(starts[i] + VO_OFFSET, 2),
                    "t2": round(starts[i] + VO_OFFSET + seg_dur[seg] + 0.45, 2)})

    # ---- chapters / sfx: rescale authored guesses to real runtime ------
    if override and not story.get("chapters"):
        chapters, sfx = [], []
    else:
        n_slots = max(1, len(slots))
        k = total / (n_slots * 11.0)
        chapters = [{"t": round(c["t"] * k, 1), "mode": c.get("mode", "warm"),
                     "intensity": c.get("intensity", 0.6)}
                    for c in story.get("chapters", [])]
        sfx = [{"kind": e.get("kind", "boop"), "at": round(e["at"] * k, 1)}
               for e in story.get("sfx", [])]

    tl = {"ep": story["ep"], "fps": config.FPS, "w": config.W, "h": config.H,
          "fade": config.FADE_DUR, "total": total, "slots": slots,
          "starts": starts, "vo_placements": vo_placements,
          "chapters": chapters, "sfx": sfx, "srt": srt,
          "title": story.get("title", story["ep"])}
    (ep_dir / "timeline.json").write_text(json.dumps(tl, indent=1, ensure_ascii=False))
    return tl
