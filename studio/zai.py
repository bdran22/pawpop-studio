"""Z.ai REST client — chat (story authoring), TTS (narration), images
(keyframes), video generation (ENGINE-A cogvideox-3).

Headers mirror the official SDK exactly. Credentials are never logged.
All calls are 429-aware: submit retries with growing backoff, polls keep
waiting while the task is alive.
"""
import base64
import json
import os
import random
import time
import urllib.error
import urllib.request

from . import config


class ZaiError(RuntimeError):
    pass


def _headers(conf, with_type=True):
    h = {"Authorization": f"Bearer {conf['apiKey']}", "X-Z-AI-From": "Z"}
    if with_type:
        h["Content-Type"] = "application/json"
    for k, hk in (("chatId", "X-Chat-Id"), ("userId", "X-User-Id"), ("token", "X-Token")):
        if conf.get(k):
            h[hk] = conf[k]
    return h


def _http(conf, path, payload=None, timeout=120, method=None):
    url = conf["baseUrl"] + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(conf, with_type=data is not None),
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode(errors="replace")
        except Exception:
            body = ""
        return e.code, body.encode() if body else b""
    except Exception as e:  # network layer
        return -1, str(e).encode()


def _http_json(conf, path, payload=None, timeout=120):
    st, raw = _http(conf, path, payload, timeout)
    try:
        return st, json.loads(raw.decode())
    except Exception:
        return st, {"raw": raw[:300].decode(errors="replace")}


def find_first(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (str, int, float)):
            return obj[key]
        for v in obj.values():
            r = find_first(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_first(v, key)
            if r is not None:
                return r
    return None


# ------------------------------------------------------------------ chat (LLM)
def chat(prompt: str, system: str | None = None, max_tokens: int = 8000,
         temperature: float = 0.8, retries: int = 4) -> str:
    conf = config.zai_conf()
    if not conf["apiKey"]:
        raise ZaiError("Z.ai config missing")
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    payload = {"model": "glm-4.6", "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    backoffs = (5, 30, 90, 240)
    last = ""
    for attempt in range(retries):
        st, body = _http_json(conf, "/chat/completions", payload, timeout=300)
        if st == 200 and isinstance(body, dict):
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
            if content:
                return content
            last = f"no content: {json.dumps(body)[:200]}"
        elif st == 429:
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            print(f"[zai.chat] 429, backoff {wait}s", flush=True)
            time.sleep(wait)
            continue
        else:
            last = f"http={st} body={json.dumps(body)[:200]}"
            time.sleep(10)
    raise ZaiError(f"chat failed: {last}")


# ------------------------------------------------------------------ TTS
def tts(text: str, out_path, voice: str | None = None, speed: float = 1.0,
        retries: int = 5) -> str:
    """Generate narration wav. Returns out_path on success."""
    conf = config.zai_conf()
    if not conf["apiKey"]:
        raise ZaiError("Z.ai config missing")
    payload = {"input": text[:900], "voice": voice or config.ZAI_TTS_VOICE,
               "speed": speed, "response_format": "wav"}
    backoffs = (15, 45, 120, 300, 600)
    for attempt in range(retries):
        st, raw = _http(conf, "/audio/tts", payload, timeout=180)
        ctype = ""
        if st == 200 and raw[:4] in (b"RIFF", b"OggS", b"\xff\xfb", b"ID3\x03") :
            out_path = str(out_path)
            with open(out_path, "wb") as f:
                f.write(raw)
            return out_path
        if st == 200:  # maybe json error wrapper
            try:
                j = json.loads(raw.decode())
                ctype = json.dumps(j)[:200]
            except Exception:
                ctype = raw[:80].decode(errors="replace")
        if st == 429 or "429" in str(ctype):
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            wait += random.randint(0, 15)
            print(f"[zai.tts] 429, backoff {wait}s ({len(text)} chars)", flush=True)
            time.sleep(wait)
            continue
        # other error: short retry
        print(f"[zai.tts] http={st} {ctype}", flush=True)
        time.sleep(20)
    raise ZaiError(f"tts failed after {retries} attempts")


# ------------------------------------------------------------------ images (keyframes)
def image(prompt: str, out_path, size: str = "1080x1920", retries: int = 4) -> str:
    conf = config.zai_conf()
    payload = {"model": "cogview-4-250304", "prompt": prompt[:1500], "size": size,
               "response_format": "b64_json"}
    backoffs = (10, 60, 180, 420)
    for attempt in range(retries):
        st, body = _http_json(conf, "/images/generations", payload, timeout=240)
        if st == 200 and isinstance(body, dict):
            b64 = find_first(body, "b64_json") or find_first(body, "url")
            if b64 and str(b64).startswith("data:"):
                raw = base64.b64decode(str(b64).split(",", 1)[1])
            elif b64:
                req = urllib.request.Request(str(b64), headers={"User-Agent": "pawpop/1.0"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    raw = r.read()
            else:
                raw = None
            if raw and len(raw) > 20000:
                with open(out_path, "wb") as f:
                    f.write(raw)
                return str(out_path)
        if st == 429:
            time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
            continue
        time.sleep(15)
    raise ZaiError(f"image failed http={st}")


# ------------------------------------------------------------------ ENGINE-A video
def video_submit(conf, keyframe_path: str, motion: str) -> str:
    """Submit image-to-video task, return task id. 429-aware."""
    with open(keyframe_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    dataurl = "data:image/jpeg;base64," + b64
    payload = {"model": config.ZAI_VIDEO_MODEL, "image_url": dataurl,
               "prompt": motion[:1200], "size": f"{config.W}x{config.H}",
               "duration": 10, "quality": "speed", "fps": config.FPS}
    backoffs = (0, 120, 300, 600, 900)
    last = ""
    for attempt, wait in enumerate(backoffs):
        if wait:
            print(f"[engineA] 429 backoff {wait}s", flush=True)
            time.sleep(wait)
        st, body = _http_json(conf, "/video/generation", payload, timeout=120)
        if st == 200 and find_first(body, "id") is not None:
            return str(find_first(body, "id"))
        last = f"http={st} body={json.dumps(body)[:200]}"
        if st != 429 and attempt >= 1:
            break
    raise ZaiError(f"video submit failed: {last}")


def video_poll(conf, task_id: str, max_seconds: int = 2400) -> str:
    """Poll async task, return video url."""
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        time.sleep(20)
        st, body = _http_json(conf, f"/async-result?id={task_id}", timeout=60)
        if st == 429:
            time.sleep(75)
            continue
        state = str(body.get("task_status") or "").upper() if isinstance(body, dict) else ""
        vurl = find_first(body, "url") or find_first(body, "video_url") if isinstance(body, dict) else None
        if state == "SUCCESS" and vurl:
            return str(vurl)
        if state in ("FAIL", "FAILED", "ERROR"):
            raise ZaiError(f"video task failed: {json.dumps(body)[:300]}")
    raise ZaiError("video poll timeout")


def video_download(url: str, out_path) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "pawpop/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(out_path) < 200000:
        raise ZaiError("video download too small")
    return str(out_path)
