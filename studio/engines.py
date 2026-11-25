"""Multi-engine video segment production.

ENGINE-A  Z.ai cogvideox-3  (10s 1080x1920, keyframe as first frame, sandbox-only)
ENGINE-C  kie.ai jobs API   (backup: seedance image-to-video, costs credits)
ENGINE-D  PixVerse          (image hosting for ENGINE-C + optional video engine)
ENGINE-E  free Wan2.2 14B i2v HuggingFace Space (gradio_client, owner token;
          ~3.5s 480x832@16 -> normalized 1080x1920@30, reachable everywhere)

Strategy per shot: ENGINE-E first (free + fast + no throttle), then
ENGINE-A, then ENGINE-C while credits remain. All state lands in
assets/<ep>/engine_status.json so any later run can resume orphans.
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
        task_id = zai.video_submit(conf, keyframe, motion)
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
    if not config.ENABLE_ENGINE_E:
        return False
    tok = config.hf_token()
    if not tok:
        return False
    out = Path(out_path)
    if out.exists() and out.stat().st_size > 200000:
        return True
    try:
        from gradio_client import Client, handle_file
        client = Client(config.WAN_SPACE, token=tok, verbose=False)
        res = client.predict(input_image=handle_file(keyframe), prompt=motion,
                             api_name="/generate_video")
        raw = res[0]
        tmp = out.with_suffix(".raw.mp4")
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
             "-vf", f"scale={config.W}:{config.H}:flags=lanczos,setsar=1,fps={config.FPS}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-an",
             str(tmp)], check=True, timeout=300)
        if tmp.stat().st_size > 150000:
            tmp.replace(out)
            _remember(status, shot_id, {"engine": "E", "state": "ok",
                                        "file": str(out), "done_at": time.time()})
            return True
        tmp.unlink(missing_ok=True)
        _remember(status, shot_id, {"engine": "E", "state": "failed",
                                    "err": "output too small"})
        return False
    except Exception as e:
        print(f"[engineE:{shot_id}] FAIL {str(e)[:180]}", flush=True)
        _remember(status, shot_id, {"engine": "E", "state": "failed",
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
        # ENGINE-E first: free, fast (~46s/clip), no submission throttle.
        if idx % 4 != 0:  # every 4th shot leaves room for C if credits exist
            done = engine_hf_shot(job["keyframe"], job["motion"], job["out"], status,
                                  job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_a_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_hf_shot(job["keyframe"], job["motion"], job["out"], status,
                                  job["shot_id"], deadline)
        if not done and time.time() < deadline:
            done = engine_c_shot(job["keyframe"], job["motion"], job["out"], status,
                                 job["shot_id"], deadline)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(work, pending))
    status_path.write_text(json.dumps(status, indent=1, ensure_ascii=False))
    return status
