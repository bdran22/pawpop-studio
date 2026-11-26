"""Multi-engine video segment production.

ENGINE-A  Z.ai cogvideox-3  (10s 1080x1920, keyframe as first frame, sandbox-only)
ENGINE-C  kie.ai jobs API   (backup: seedance image-to-video, costs credits;
          demoted to last — owner's $1.65 top-up never landed, balance 1.6 cr)
ENGINE-D  PixVerse          (image hosting; account balance = 0 credits,
          video generation unavailable until owner tops up)
ENGINE-E  free Wan2.2 14B i2v HuggingFace Space (gradio_client, owner token;
          ~3.5s 480x832@16 -> normalized 1080x1920@30, reachable everywhere)
ENGINE-F  free Wan2.2-Fast distilled 6-step Space (prithivMLmods/Wan2.2-Fast;
          ~50s/clip, returns base64 video — no result-file 403 issue.
          Quality-fragile on aggressive motion: only fed calm prompts, and
          its clips face the same episode-level QC gate as every other clip)

All HF ZeroGPU spaces share ONE daily quota pool per token. The router
detects "exceeded your free ZeroGPU quota" and demotes E+F for a cooldown
(default 3h) instead of wasting wall time on doomed attempts.

Strategy per shot: ENGINE-E (proven EP004 quality) -> ENGINE-F (fast) ->
ENGINE-A (z.ai, unlimited but throttled) -> ENGINE-C (kie.ai if credits).
State lands in assets/<ep>/engine_status.json so later runs resume orphans.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import config, zai

LOCK = threading.Lock()

# module-level ZeroGPU quota state (survives within one process run;
# persisted copies live in engine_status.json)
_HF = {"dead_until": 0.0, "reason": ""}


def hf_quota_note() -> str:
    if time.time() < _HF["dead_until"]:
        mins = int((_HF["dead_until"] - time.time()) / 60)
        return f"HF ZeroGPU cooldown {mins}m left ({_HF['reason'][:60]})"
    return ""


def _note_hf_quota_error(err: str):
    low = str(err).lower()
    if ("zerogpu quota" in low or "exceeded your free" in low
            or "gpu quota" in low):
        until = time.time() + config.HF_QUOTA_COOLDOWN_H * 3600
        with LOCK:
            if until > _HF["dead_until"]:
                _HF["dead_until"] = until
                _HF["reason"] = str(err)[:160]


def _hf_available() -> bool:
    return time.time() >= _HF["dead_until"]


# ------------------------------------------------------------------ pixverse image hosting
def pv_upload_image(png_path: str) -> dict:
    """Upload keyframe to PixVerse free hosting -> {img_id, img_url}."""
    key = config.PIXVERSE_API_KEY
    if not key:
        raise RuntimeError("PIXVERSE_API_KEY missing")
    try:
        from PIL import Image
        im = Image.open(png_path).convert("RGB")
        im.thumbnail((1080, 1920))
        tmp = Path("/tmp") / f"pv_{uuid.uuid4().hex[:8]}.jpg"
        im.save(tmp, "JPEG", quality=88)
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
    except ImportError:
        data = Path(png_path).read_bytes()
    boundary = uuid.uuid4().hex
    name = Path(png_path).stem + ".jpg"
    body = (
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
         f'filename="{name}"\r\nContent-Type: image/jpeg\r\n\r\n').encode()
        + data + f"\r\n--{boundary}--\r\n".encode())
    headers = {"API-KEY": key, "Ai-Plat-Form": "openapi",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(config.PIXVERSE_BASE + "/openapi/v2/image/upload",
                                 data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        j = json.loads(r.read().decode())
    if j.get("ErrCode") == 0 and j.get("Resp"):
        return {"img_id": j["Resp"].get("img_id"), "img_url": j["Resp"].get("img_url")}
    raise RuntimeError(f"pixverse upload failed: {json.dumps(j)[:200]}")


# ------------------------------------------------------------------ kie.ai (ENGINE-C)
def kie_call(path: str, payload=None, method="GET", timeout=60):
    h = {"Authorization": f"Bearer {config.KIE_API_KEY}",
         "Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "Mozilla/5.0"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(config.KIE_BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def kie_credit() -> float | None:
    st, body = kie_call("/api/v1/chat/credit")
    if st == 200 and isinstance(body, dict):
        return body.get("data")
    return None


def engine_c_submit(image_url: str, motion: str, model: str = "bytedance/seedance-v1-lite",
                    resolution: str = "720p", duration: str = "5") -> str:
    payload = {"model": model,
               "input": {"prompt": motion[:1200], "image_url": image_url,
                          "aspect_ratio": "9:16", "resolution": resolution,
                          "duration": duration}}
    st, body = kie_call("/api/v1/jobs/createTask", payload, "POST", timeout=120)
    if st == 200 and isinstance(body, dict) and body.get("code") == 200:
        return str(body["data"]["taskId"])
    raise RuntimeError(f"kie createTask failed http={st} {json.dumps(body)[:200]}")


def engine_c_poll(task_id: str, max_seconds: int = 1500) -> str:
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        st, body = kie_call(f"/api/v1/jobs/record/{task_id}")
        if st == 200 and isinstance(body, dict) and body.get("code") == 200:
            d = body.get("data") or {}
            flag = d.get("successFlag")
            if flag == 1:
                return d["response"]["resultUrl"]
            if flag in (2, 3):
                raise RuntimeError(f"kie task failed: {json.dumps(d)[:300]}")
        time.sleep(20)
    raise RuntimeError("kie poll timeout")


# ------------------------------------------------------------------ ENGINE-A wrapper
def engine_a_shot(keyframe: str, motion: str, out_path: str, status: dict,
                  shot_id: str, deadline: float) -> bool:
    conf = config.zai_conf()
    if not conf["apiKey"]:
        return False
    try:
        # bounded submit backoff: never block a whole round on 429s
        budget = max(30.0, min(240.0, deadline - time.time()))
        task_id = zai.video_submit(conf, keyframe, motion,
                                   backoff_budget=budget)
        _remember(status, shot_id, {"engine": "A", "task_id": task_id, "state": "running"})
        url = zai.video_poll(conf, task_id, max_seconds=max(300, int(deadline - time.time())))
        zai.video_download(url, out_path)
        _remember(status, shot_id, {"engine": "A", "state": "ok",
                                    "file": str(out_path), "done_at": time.time()})
        return True
    except Exception as e:
        print(f"[engineA:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _remember(status, shot_id, {"engine": "A", "state": "failed", "err": str(e)[:200]})
        return False


def engine_c_shot(keyframe: str, motion: str, out_path: str, status: dict,
                  shot_id: str, deadline: float) -> bool:
    if not config.ENABLE_ENGINE_C or not config.KIE_API_KEY:
        return False
    credit = kie_credit()
    if credit is None or credit < 0.1:
        return False
    try:
        host = pv_upload_image(keyframe)
        model = "bytedance/seedance-v1-lite"
        task_id = engine_c_submit(host["img_url"], motion, model=model,
                                  resolution="720p", duration="5")
        _remember(status, shot_id, {"engine": "C", "task_id": task_id, "state": "running"})
        url = engine_c_poll(task_id, max_seconds=max(300, int(deadline - time.time())))
        _download(url, out_path)
        _remember(status, shot_id, {"engine": "C", "state": "ok",
                                    "file": str(out_path), "done_at": time.time()})
        return True
    except Exception as e:
        print(f"[engineC:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _remember(status, shot_id, {"engine": "C", "state": "failed", "err": str(e)[:200]})
        return False


# ------------------------------------------------------------------ ENGINE-E (free HF Wan2.2)
def engine_hf_shot(keyframe: str, motion: str, out_path: str, status: dict,
                   shot_id: str, deadline: float) -> bool:
    """Animate one keyframe via the free Wan2.2 i2v Space (owner HF token).

    Raw output is 480x832@16fps ~3.5s; normalized here to 1080x1920@30 so the
    assembler sees a uniform supply. Returns True on success.
    """
    if not config.ENABLE_ENGINE_E or not _hf_available():
        return False
    tok = config.hf_token()
    if not tok:
        return False
    out = Path(out_path)
    if out.exists() and out.stat().st_size > 200000:
        return True
    try:
        from gradio_client import Client, handle_file
        client = Client(config.WAN_SPACE, token=tok, verbose=False,
                        download_files=False)
        res = client.predict(input_image=handle_file(keyframe), prompt=motion,
                             steps=6, negative_prompt=_NEG_PROMPT,
                             duration_seconds=3.5,
                             guidance_scale=5.0, guidance_scale_2=3.5,
                             seed=0, randomize_seed=True,
                             api_name="/generate_video")
        raw = _extract_video_bytes(res)
        if raw is None:
            raise RuntimeError(f"unexpected result shape {str(res)[:120]}")
        _normalize_to_master(raw, out)
        if out.stat().st_size > 150000:
            _remember(status, shot_id, {"engine": "E", "state": "ok",
                                        "file": str(out), "done_at": time.time()})
            return True
        out.unlink(missing_ok=True)
        _remember(status, shot_id, {"engine": "E", "state": "failed",
                                    "err": "output too small"})
        return False
    except Exception as e:
        print(f"[engineE:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _note_hf_quota_error(e)
        _remember(status, shot_id, {"engine": "E", "state": "failed",
                                    "err": str(e)[:200]})
        return False


# ------------------------------------------------------------------ ENGINE-F (free HF Wan2.2-Fast, base64 return)
_AGGRESSIVE = ("spring", "leap", "leaps", "dash", "dashes", "run", "runs",
               "chase", "chases", "charge", "charges", "jump", "jumps",
               "scramble", "bolt", "bolts", "sprint", "spin", "spins",
               "pounce", "tumble", "crash", "explode", "burst")


def _motion_is_calm(motion: str) -> bool:
    m = motion.lower()
    return not any(w in m for w in _AGGRESSIVE)


_NEG_PROMPT = ("blurry, distorted, deformed, static frame, low quality, "
               "watermark, text, extra limbs, morphing artifacts")


def engine_f_shot(keyframe: str, motion: str, out_path: str, status: dict,
                  shot_id: str, deadline: float) -> bool:
    """Animate via free Wan2.2-Fast distilled Space (base64 result path).

    Only accepts CALM motion prompts (the distilled 6-step model degrades
    identity on aggressive movement). Same normalization as ENGINE-E.
    """
    if not config.ENABLE_ENGINE_F or not _hf_available():
        return False
    if not _motion_is_calm(motion):
        return False
    tok = config.hf_token()
    if not tok:
        return False
    out = Path(out_path)
    if out.exists() and out.stat().st_size > 200000:
        return True
    try:
        from gradio_client import Client
        client = Client(config.WANFAST_SPACE, token=tok, verbose=False,
                        download_files=False)
        b64 = ("data:image/png;base64,"
               + base64.b64encode(Path(keyframe).read_bytes()).decode())
        res = client.predict(image_b64=b64, prompt=motion, steps=6,
                             negative_prompt=_NEG_PROMPT,
                             duration_seconds=4.0,
                             guidance_scale=4.0, guidance_scale_2=3.0,
                             seed=0, randomize_seed=True,
                             api_name="/generate_video")
        raw = _extract_video_bytes(res)
        if raw is None:
            raise RuntimeError(f"unexpected result shape {str(res)[:120]}")
        _normalize_to_master(raw, out)
        if out.stat().st_size > 150000:
            _remember(status, shot_id, {"engine": "F", "state": "ok",
                                        "file": str(out), "done_at": time.time()})
            return True
        out.unlink(missing_ok=True)
        _remember(status, shot_id, {"engine": "F", "state": "failed",
                                    "err": "output too small"})
        return False
    except Exception as e:
        print(f"[engineF:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _note_hf_quota_error(e)
        _remember(status, shot_id, {"engine": "F", "state": "failed",
                                    "err": str(e)[:200]})
        return False


def _extract_video_bytes(res) -> bytes | None:
    """Pull mp4 bytes out of a gradio result: base64 data-URL, local file
    path, or http(s) URL."""
    cand = res
    if isinstance(res, (list, tuple)) and res:
        cand = res[0]
    if isinstance(cand, dict):
        cand = cand.get("video") or cand.get("filepath") or next(
            iter(cand.values()), None)
    if cand is None:
        return None
    if isinstance(cand, str):
        if cand.startswith("data:"):
            return base64.b64decode(cand.split(",", 1)[1])
        if cand.startswith("http"):
            req = urllib.request.Request(cand, headers={
                "User-Agent": "Mozilla/5.0",
                "Authorization": f"Bearer {config.hf_token()}"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        p = Path(cand)
        if p.exists():
            return p.read_bytes()
    return None


def _normalize_to_master(raw_bytes: bytes, out: Path):
    """Raw space output (480x832@16) -> 1080x1920@30 h264 fragment."""
    tmp = out.with_suffix(".raw.mp4")
    tmp.write_bytes(raw_bytes)
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
         "-vf", f"scale={config.W}:{config.H}:flags=lanczos,setsar=1,fps={config.FPS}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-an",
         str(out) + ".norm.mp4"], check=True, timeout=300)
    Path(str(out) + ".norm.mp4").replace(out)
    tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ ENGINE-K (local Ken Burns, unlimited)
def engine_k_shot(keyframe: str, motion: str, out_path: str, status: dict,
                  shot_id: str, deadline: float) -> bool:
    """Local cinematic fallback: animated camera over the keyframe.

    Not AI-video, but genuinely non-static: alternating zoom in/out and
    lateral drift (shot_id hash picks the move) rendered at 1080x1920@30.
    Used only when every network engine is unavailable, so a slot is never
    missed; the episode QC rubric records it honestly. ~5s render each.
    """
    if not config.ENABLE_ENGINE_K:
        return False
    out = Path(out_path)
    if out.exists() and out.stat().st_size > 200000:
        return True
    import subprocess
    D = 150  # 5s @ 30fps
    moves = [
        ("'1+0.10*on/{d}'", "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"),
        ("'1.10-0.10*on/{d}'", "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"),
        ("'1.06+0.04*on/{d}'",
         f"'iw/2-(iw/zoom/2)+{int(config.W*0.012)}*on/{{d}}'",
         "'ih/2-(ih/zoom/2)'"),
        ("'1.09-0.05*on/{d}'",
         f"'iw/2-(iw/zoom/2)-{int(config.W*0.012)}*on/{{d}}'",
         "'ih/2-(ih/zoom/2)'"),
    ]
    z, x, y = moves[abs(hash(shot_id)) % len(moves)]
    d = D - 1
    vf = (f"scale=1620:2880:flags=lanczos,"
          f"zoompan=z={z.format(d=d)}:x={x.format(d=d)}:y={y.format(d=d)}"
          f":d={D}:s={config.W}x{config.H}:fps={config.FPS},"
          f"eq=saturation=1.05,format=yuv420p,setsar=1")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", keyframe, "-vf", vf,
             "-frames:v", str(D), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "17", "-an", str(out)],
            check=True, timeout=600)
        if out.stat().st_size > 150000:
            _remember(status, shot_id, {"engine": "K", "state": "ok",
                                        "file": str(out), "done_at": time.time()})
            return True
        out.unlink(missing_ok=True)
        _remember(status, shot_id, {"engine": "K", "state": "failed",
                                    "err": "output too small"})
        return False
    except Exception as e:
        print(f"[engineK:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _remember(status, shot_id, {"engine": "K", "state": "failed",
                                    "err": str(e)[:200]})
        return False


def _download(url: str, out_path: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _remember(status: dict, shot_id: str, update: dict):
    with LOCK:
        cur = status.get(shot_id) or {}
        cur.update(update)
        status[shot_id] = cur
        p = Path(config.ASSETS) / "_engine_status_tmp.json"
        p.write_text(json.dumps(status, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ parallel driver
def produce_shots(jobs: list[dict], ep_dir: Path, deadline: float, max_workers: int = 2) -> dict:
    """jobs: [{shot_id, keyframe, motion, out}] -> status dict.

    Runs ENGINE-A workers in parallel; ENGINE-C absorbs shots when A is
    saturated (simple heuristic: every 3rd queued shot tries C first if
    credits allow). Stops submitting new tasks past the deadline.
    """
    status_path = ep_dir / "engine_status.json"
    status = {}
    if status_path.exists():
        status = json.loads(status_path.read_text())
    pending = [j for j in jobs
               if not (Path(j["out"]).exists() and Path(j["out"]).stat().st_size > 200000)]
    print(f"[produce] {len(pending)} shots to produce (of {len(jobs)})", flush=True)
    if not pending:
        return status

    from concurrent.futures import ThreadPoolExecutor
    counter = {"n": 0}

    def work(job):
        if time.time() > deadline:
            return
        with LOCK:
            counter["n"] += 1
            idx = counter["n"]
        done = False
        # ENGINE-E first: free, proven EP004 quality, no submission throttle.
        if idx % 4 != 0:  # every 4th shot leaves room for C if credits exist
            done = engine_hf_shot(job["keyframe"], job["motion"], job["out"], status,
                                  job["shot_id"], deadline)
        # ENGINE-F: free Wan2.2-Fast, ~50s/clip, calm motion only.
        if not done and time.time() < deadline:
            done = engine_f_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_a_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_hf_shot(job["keyframe"], job["motion"], job["out"], status,
                                  job["shot_id"], deadline)
        # ENGINE-K: local animated-camera fallback so a slot is never missed
        # (only reached when every network engine failed).
        if not done and time.time() < deadline:
            done = engine_k_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_c_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(work, pending))
    status_path.write_text(json.dumps(status, indent=1, ensure_ascii=False))
    return status
