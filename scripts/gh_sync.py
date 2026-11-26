#!/usr/bin/env python3
"""PAWPOP sandbox -> GitHub durability sync (upload-first policy).

Packages the sandbox EP004 production assets (animated clips, keyframes,
VO wavs, timeline + statuses) and uploads them as a single release asset
`ep004_latest.tar.gz` on the repo's `sandbox-archive` release. Uploads only
when the asset manifest changed, so idle runs cost nothing.

Secrets are read from PAWPOP_STUDIO/secrets/ and never printed.
"""
import hashlib
import json
import os
import tarfile
import time
import urllib.request
from pathlib import Path

SEC = Path("/home/z/my-project/PAWPOP_STUDIO/secrets")
SRC = Path("/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna")
EP5 = Path("/home/z/my-project/pawpop-studio/work/assets/ep005")
REPO = "bdran22/pawpop-studio"
MARKER = Path("/home/z/my-project/.gh_sync_state.json")
MARKER5 = Path("/home/z/my-project/.gh_sync_state_ep005.json")
TAG = "sandbox-archive"
ASSET_NAME = "ep004_latest.tar.gz"
ASSET5_NAME = "ep005_latest.tar.gz"

INCLUDE = ["clips", "keyframes", "audio", "render"]  # render: scene mp4s, durability


def manifest() -> str:
    h = hashlib.sha256()
    for sub in ("clips", "keyframes", "audio"):
        d = SRC / sub
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file():
                h.update(f"{f.name}:{f.stat().st_size}".encode())
    tl = SRC / "ep004_timeline.json"
    if tl.exists():
        h.update(tl.read_bytes()[:4096])
    return h.hexdigest()


def api(path: str, payload=None, method=None, token=None, raw=None, ctype=None):
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "pawpop-sync"}
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = ctype or "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path if not path.startswith("https://") else path,
                                 data=data, headers=headers, method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=300) as r:
        body = r.read()
    return json.loads(body) if body and not ctype else (body or b"")


def manifest5() -> str:
    h = hashlib.sha256()
    if not EP5.exists():
        return "none"
    for f in sorted(EP5.rglob("*")):
        if f.is_file() and f.stat().st_size < 200_000_000:
            h.update(f"{f.relative_to(EP5)}:{f.stat().st_size}".encode())
    return h.hexdigest()


def get_release(token):
    try:
        return api(f"/repos/{REPO}/releases/tags/{TAG}", token=token)
    except Exception:
        return api(f"/repos/{REPO}/releases",
                   {"tag_name": TAG, "name": TAG,
                    "body": "Sandbox-produced EP004/EP005 assets (durability archive)"}, token=token)


def upload_asset(rel, token, name, path):
    size = path.stat().st_size
    up = rel["upload_url"].split("{")[0]
    for a in api(f"/repos/{REPO}/releases/{rel['id']}/assets?per_page=100", token=token):
        if a.get("name") == name:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/assets/{a['id']}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "pawpop-sync"},
                method="DELETE"), timeout=120)
            break
    req = urllib.request.Request(
        f"{up}?name={name}", data=path.read_bytes(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/gzip",
                 "Content-Length": str(size), "User-Agent": "pawpop-sync"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read().decode())


def main():
    token = (SEC / "github_pat.txt").read_text().strip()
    m = manifest()
    ep004_changed = not (MARKER.exists() and json.loads(MARKER.read_text()).get("manifest") == m)
    t0 = time.time()
    if ep004_changed:
        tar = Path("/tmp/ep004_latest.tar.gz")
        with tarfile.open(tar, "w:gz") as tf:
            for sub in ("clips", "keyframes", "audio"):
                d = SRC / sub
                if d.exists():
                    tf.add(d, arcname=f"ep004/{sub}")
            for f in ("ep004_timeline.json", "engine_a_status.json", "kie_status.json",
                      "engine_d_status.json", "upload_metadata.json", "pending_tasks.json"):
                p = SRC / f
                if p.exists():
                    tf.add(p, arcname=f"ep004/{f}")
        size = tar.stat().st_size
        rel = get_release(token)
        out = upload_asset(rel, token, ASSET_NAME, tar)
        MARKER.write_text(json.dumps({"manifest": m, "asset_id": out.get("id"),
                                      "size": size, "at": time.time()}))
        print(f"sync: uploaded {ASSET_NAME} {size/1e6:.1f}MB in {time.time()-t0:.0f}s "
              f"(clips+keyframes+audio+timeline)")
    else:
        print("sync ep004: unchanged, skip")

    # ---- EP005 work assets durability (VO/keyframes/clips survive snapshot wipes)
    m5 = manifest5()
    if m5 != "none":
        if MARKER5.exists() and json.loads(MARKER5.read_text()).get("manifest") == m5:
            print("sync ep005: unchanged, skip")
            return
        t5 = time.time()
        tar5 = Path("/tmp/ep005_latest.tar.gz")
        with tarfile.open(tar5, "w:gz") as tf:
            tf.add(EP5, arcname="ep005")
        rel = get_release(token)
        out5 = upload_asset(rel, token, ASSET5_NAME, tar5)
        MARKER5.write_text(json.dumps({"manifest": m5, "asset_id": out5.get("id"),
                                       "size": tar5.stat().st_size, "at": time.time()}))
        print(f"sync: uploaded {ASSET5_NAME} {tar5.stat().st_size/1e6:.1f}MB "
              f"in {time.time()-t5:.0f}s (vo+keyframes+clips+reports)")


if __name__ == "__main__":
    main()
