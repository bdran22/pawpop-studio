"""Generic episode assembler (ported from the EP004 pipeline).

Stage 1  normalize every slot to exact-duration 1080x1920@30 pieces
Stage 2  0.35s pre-blended crossfades at fade boundaries
Stage 3  chunked concat (concat demuxer, -c copy)
Stage 4  audio mix: VO + original synth music bed + SFX + ducking
Stage 5  captions burn + master encode
Owner directive: ZERO static-image scenes — a missing clip aborts the
assembly, it is never filled with a still frame.
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import audio as aud
from . import config

ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
       "-pix_fmt", "yuv420p", "-an", "-video_track_timescale", "90000"]
MASTER_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart"]


def run(cmd, allow_fail=False):
    print(">>", " ".join(str(c) for c in cmd[:8]), "...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not allow_fail:
        print(r.stderr[-1200:], file=sys.stderr)
        raise RuntimeError(f"command failed: {cmd[:6]}")
    return r


def probe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def make_piece(idx, slot, tl, pieces_dir: Path, clip_path):
    out = pieces_dir / f"piece_{idx:02d}_{slot['id']}.mp4"
    if out.exists() and out.stat().st_size > 30000:
        return out
    if not clip_path or not Path(clip_path).exists():
        raise RuntimeError(
            f"missing animation clip for slot {slot['id']} ({slot['kf']}) — "
            f"owner directive: zero static scenes, assembly aborted")
    dur, W, H, FPS = slot["dur"], tl["w"], tl["h"], tl["fps"]
    flow = slot.get("flow") or {}
    kind = flow.get("kind", "trim")
    src_in = float(flow.get("src_in", 0.0))
    zoom = float(flow.get("zoom", 1.0))
    speed = float(flow.get("speed", 1.0))
    passes = int(flow.get("passes", 2))
    # Supply-adaptive flow: a piece must NEVER be shorter than its slot or the
    # concat timeline desyncs. If the source cannot cover the slot as
    # trim/slow, fall back to a ping-pong with enough passes (reverse playback
    # is still real animation - no freeze frames).
    cd = probe_dur(clip_path) or 10.29
    src_in = min(src_in, max(0.0, cd - 1.0))
    avail = max(0.8, cd - src_in)
    if kind in ("trim", "slow") and avail < dur - 0.05:
        kind = "pingpong"
    if kind == "pingpong":
        eff = avail * min(speed, 1.0) * 0.95
        need = max(2, math.ceil(dur / max(eff, 0.5)))
        if need > passes:
            passes = need
        total_supply = passes * avail * min(speed, 1.0)
        if total_supply < dur - 0.05:
            passes = max(2, math.ceil(dur / (avail * 0.92)))
            speed = min(1.0, round(dur / (passes * avail), 4))
    zcrop = ""
    if zoom > 1.001:
        cw, chh = int(W / zoom) // 2 * 2, int(H / zoom) // 2 * 2
        zcrop = f"crop={cw}:{chh},"
    base = f"{zcrop}scale={W}:{H}:flags=bicubic,setsar=1,fps={FPS},format=yuv420p"
    slow = f"setpts=PTS*{1.0 / max(speed, 0.05):.5f}," if speed < 0.999 else ""
    if kind == "trim":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip_path),
               "-ss", f"{src_in:.3f}", "-t", f"{dur:.3f}", "-vf", base] + ENC + [str(out)]
    elif kind == "slow":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip_path),
               "-ss", f"{src_in:.3f}", "-t", f"{dur:.3f}", "-vf", slow + base] + ENC + [str(out)]
    else:
        # Generic ping-pong: build one forward+reverse block, then loop it as
        # many times as needed (works for any pass count, long narration safe).
        n_loops = max(1, math.ceil(passes / 2))
        pp = pieces_dir / f"pp_{idx:02d}_{slot['id']}.mp4"
        if not (pp.exists() and pp.stat().st_size > 10000):
            fc = (f"[0:v]{slow}{base},split[a][b];[b]reverse[r];"
                  f"[a][r]concat=n=2:v=1[out]")
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip_path),
                 "-ss", f"{src_in:.3f}", "-filter_complex", fc,
                 "-map", "[out]"] + ENC + [str(pp)])
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-fflags", "+genpts",
               "-stream_loop", str(n_loops - 1), "-i", str(pp),
               "-t", f"{dur:.3f}"] + ENC + [str(out)]
    run(cmd)
    return out


def make_blend(out_id, prev_piece, next_piece, pieces_dir: Path, fd, fps):
    out = pieces_dir / f"blend_{out_id}.mp4"
    if out.exists() and out.stat().st_size > 8000:
        return out
    fc = (f"[0:v]settb=AVTB,format=yuv420p,trim={0.0}:{fd},setpts=PTS-STARTPTS,fps={fps}[a];"
          f"[1:v]settb=AVTB,format=yuv420p,trim=0:{fd},setpts=PTS-STARTPTS,fps={fps}[b];"
          f"[a][b]xfade=transition=fade:duration={fd}:offset=0[v]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(prev_piece), "-i", str(next_piece),
         "-filter_complex", fc, "-map", "[v]"] + ENC + [str(out)])
    return out


def srt_time(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(tl, path: Path):
    blocks = []
    for cue in tl["srt"]:
        blocks.append(f"{cue['n']}\n{srt_time(cue['t1'])} --> {srt_time(cue['t2'])}\n{cue['str']}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    print(f"SRT: {len(blocks)} cues -> {path}", flush=True)


def build_audio(tl, ep_dir: Path, out_wav: Path):
    total = tl["total"]
    n = int(total * aud.SR) + aud.SR
    vo_track = np.zeros(n, dtype=np.float32)
    for pl in tl["vo_placements"]:
        x = aud.load_wav_mono(Path(ep_dir) / "vo" / pl["file"])
        i0 = int(pl["at"] * aud.SR)
        x = x[: len(vo_track) - i0]
        if len(x) > 0:
            vo_track[i0: i0 + len(x)] += x
    print(f"VO track built: {len(tl['vo_placements'])} segments", flush=True)
    bed = aud.music_bed(tl.get("chapters"), total)
    print("music bed synthesized", flush=True)
    sfx = aud.build_sfx(tl.get("sfx"), total)
    print("SFX rendered", flush=True)
    gain = aud.duck_gain(aud.rms_env(vo_track))
    mix = bed * gain + sfx * (10 ** (-5 / 20)) * gain + vo_track * 0.92
    fade = int(1.0 * aud.SR)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix[-fade:] *= np.linspace(1, 0, fade)
    peak = float(np.abs(mix).max())
    if peak > 10 ** (-1.2 / 20):
        mix *= 10 ** (-1.2 / 20) / peak
    print(f"audio peak: {20 * math.log10(peak + 1e-9):.1f} dBFS", flush=True)
    aud.write_wav(out_wav, np.stack([mix, mix], axis=1))
    return out_wav


def stage_concat(tl, pieces, blends, tmp: Path):
    slots, durs = tl["slots"], [s["dur"] for s in tl["slots"]]
    fd = tl["fade"]
    order = []
    for i in range(len(slots)):
        order.append(("p", i))
        if i in blends:
            order.append(("b", i))

    def trim_window(i):
        lo, hi = 0.0, durs[i]
        if i > 0 and slots[i - 1]["trans"] == "fade":
            lo = fd
        if i < len(slots) - 1 and slots[i]["trans"] == "fade":
            hi = durs[i] - fd
        return lo, hi

    chunks = []
    CHUNK = 10
    fps = tl["fps"]
    for c0 in range(0, len(order), CHUNK):
        group = order[c0: c0 + CHUNK]
        inputs = []
        for kind, idx in group:
            inputs += ["-i", str(pieces[idx] if kind == "p" else blends[idx])]
        lines = []
        for k, (kind, idx) in enumerate(group):
            if kind == "b":
                lines.append(f"[{k}:v]settb=AVTB,format=yuv420p,fps={fps}[g{k}]")
            else:
                lo, hi = trim_window(idx)
                if hi - lo < 0.05:
                    lo, hi = 0.0, durs[idx]
                lines.append(f"[{k}:v]settb=AVTB,format=yuv420p,trim={lo:.3f}:{hi:.3f},"
                             f"setpts=PTS-STARTPTS,fps={fps}[g{k}]")
        labels = "".join(f"[g{k}]" for k in range(len(group)))
        lines.append(f"{labels}concat=n={len(group)}:v=1:a=0[vout]")
        chunk_path = tmp / f"chunk_{c0 // CHUNK:02d}.mp4"
        if not (chunk_path.exists() and chunk_path.stat().st_size > 100000):
            run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", ";".join(lines), "-map", "[vout]"] + ENC + [str(chunk_path)])
        chunks.append(chunk_path)
        print(f"chunk {c0 // CHUNK}: {len(group)} pieces", flush=True)

    list_path = tmp / "chunks.txt"
    list_path.write_text("".join(f"file '{c}'\n" for c in chunks))
    silent = tmp / "video_silent.mp4"
    if not (silent.exists() and silent.stat().st_size > 1000000):
        run(["ffmpeg", "-y", "-loglevel", "error", "-fflags", "+genpts", "-f", "concat",
             "-safe", "0", "-i", str(list_path), "-c", "copy", str(silent)])
    vdur = probe_dur(silent)
    print(f"silent video: {vdur:.2f}s", flush=True)
    return silent, vdur


def stage_master(silent, wav, srt, out_master: Path):
    """Captions burn + audio mux + final encode."""
    vf = (f"subtitles={srt}:force_style='FontName=DejaVu Sans,Fontsize=13,"
          "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
          "Outline=1,Shadow=0.6,MarginV=28'")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(wav),
         "-vf", vf, "-shortest"] + MASTER_ENC + [str(out_master)])
    if not out_master.exists() or out_master.stat().st_size < 1_000_000:
        raise RuntimeError("master encode failed or too small")
    return out_master
