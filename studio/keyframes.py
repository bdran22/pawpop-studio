"""Keyframe generation with provider chain.

1) Z.ai cogview (same generator as EP001-EP004 look)
2) pollinations.ai (free, no key) — fallback so production never stalls.

Character consistency: every prompt is prefixed with the locked character
sheet from the story JSON (story["style_prefix"]).
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, zai


def _kf_zai(prompt: str, out: Path) -> bool:
    try:
        zai.image(prompt, str(out))
        return out.exists() and out.stat().st_size > 20000
    except Exception as e:
        print(f"[kf.zai] {out.stem}: {str(e)[:160]}", flush=True)
        return False


def _kf_pollinations(prompt: str, out: Path, seed: int) -> bool:
    url = ("https://image.pollinations.ai/prompt/"
           + urllib.parse.quote(prompt[:1600])
           + f"?width={config.W}&height={config.H}&seed={seed}&nologo=true&model=flux")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pawpop/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        ok = out.exists() and out.stat().st_size > 30000
        if ok:  # pollinations always returns an image; sanity check via PIL
            try:
                from PIL import Image
                im = Image.open(out)
                im.verify()
            except Exception:
                return False
        return ok
    except Exception as e:
        print(f"[kf.pollinations] {out.stem}: {str(e)[:160]}", flush=True)
        return False


def ensure_keyframes(story: dict, ep_dir: Path, deadline: float) -> dict:
    """Generate missing keyframes. Returns {kf_id: path}."""
    kdir = ep_dir / "keyframes"
    kdir.mkdir(parents=True, exist_ok=True)
    prefix = story.get("style_prefix", "")
    have = {}
    missing = []
    for shot in story["shots"]:
        kf = shot["kf"]
        out = kdir / f"{kf}.png"
        if out.exists() and out.stat().st_size > 20000:
            have[kf] = str(out)
        else:
            missing.append(shot)
    for shot in missing:
        if time.time() > deadline:
            break
        kf, out = shot["kf"], kdir / f"{shot['kf']}.png"
        seed = abs(hash(story["ep"] + kf)) % 1_000_000
        prompt = f"{prefix} {shot['kf_prompt']}".strip()
        ok = _kf_zai(prompt, out) or _kf_pollinations(prompt, out, seed)
        if ok:
            have[kf] = str(out)
            print(f"[kf] {kf} ok", flush=True)
        else:
            print(f"[kf] {kf} FAILED", flush=True)
    (ep_dir / "keyframes_index.json").write_text(json.dumps(have, indent=1))
    return have
