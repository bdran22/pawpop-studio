"""Narration (VO) generation with provider chain.

1) Z.ai TTS (voice "jam" — British storyteller, same as EP001-EP004)
2) edge-tts fallback (en-GB-RyanNeural, free, no key) — keeps the pipeline
   alive if the Z.ai voice quota is throttled.
"""
import json
import re
import subprocess
import time
from pathlib import Path

from . import config, zai


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tts_zai(text: str, out: Path) -> bool:
    try:
        zai.tts(_clean(text), str(out), voice=config.ZAI_TTS_VOICE)
        return out.exists() and out.stat().st_size > 20000
    except Exception as e:
        print(f"[vo.zai] {out.stem}: {str(e)[:160]}", flush=True)
        return False


def tts_edge(text: str, out: Path) -> bool:
    try:
        subprocess.run(["edge-tts", "--voice", "en-GB-RyanNeural", "--rate", "-4%",
                        "--text", _clean(text), "--write-media", str(out)],
                       check=True, capture_output=True, timeout=180)
        return out.exists() and out.stat().st_size > 20000
    except Exception as e:
        print(f"[vo.edge] {out.stem}: {str(e)[:160]}", flush=True)
        return False


def ensure_vo(story: dict, ep_dir: Path, deadline: float) -> dict:
    """Generate every missing VO wav. Returns report {seg: {file, seconds, provider}}."""
    aud = ep_dir / "vo"
    aud.mkdir(parents=True, exist_ok=True)
    rep_path = ep_dir / "vo_report.json"
    report = json.loads(rep_path.read_text()) if rep_path.exists() else {}
    for seg, text in story["vo"].items():
        out = aud / f"{seg}.wav"
        if out.exists() and out.stat().st_size > 20000:
            continue
        if time.time() > deadline:
            break
        ok = tts_zai(text, out) or tts_edge(text, out)
        report[seg] = {"ok": ok, "provider": "zai" if ok else None}
        if ok:
            print(f"[vo] {seg} ok ({out.stat().st_size // 1024}KB)", flush=True)
        report[seg]["file"] = out.name
    rep_path.write_text(json.dumps(report, indent=1))
    return report
