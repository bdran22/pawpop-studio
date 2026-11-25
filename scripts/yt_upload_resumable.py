#!/usr/bin/env python3
"""Chunked resumable YouTube uploader with session-resume across interruptions.

Usage:
  python3 yt_upload_resumable.py --video FILE --meta META --publish-at "2026-11-25 21:00"

- Opens a resumable session, streams the file in 8 MiB chunks.
- On any network error: queries the session (PUT Content-Range: bytes */SIZE)
  to learn the accepted offset and continues from there (any number of times).
- Session URI + offset persisted to /tmp/yt_up/<hash>.json so even a NEW
  process can resume an interrupted upload.
"""
import argparse, hashlib, json, os, sys, time, urllib.request, urllib.error

sys.path.insert(0, "/home/z/my-project/scripts")
import yt_api as yt

CHUNK = 8 * 1024 * 1024  # 8 MiB


def to_rfc3339(s):
    return yt.to_rfc3339_utc(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--privacy", default="private")
    ap.add_argument("--publish-at", dest="publish_at", default=None)
    args = ap.parse_args()

    meta = json.load(open(args.meta, encoding="utf-8"))
    size = os.path.getsize(args.video)
    vhash = hashlib.md5(args.video.encode()).hexdigest()[:10]
    state_path = f"/tmp/yt_up_{vhash}.json"
    token = yt.get_access_token()["access_token"]

    location, offset = None, 0
    if os.path.exists(state_path):
        st = json.load(open(state_path))
        location, offset = st.get("location"), st.get("offset", 0)
        print(f"resuming session: offset {offset}/{size} ({100*offset/size:.1f}%)")

    if not location:
        publish_at = to_rfc3339(args.publish_at) if args.publish_at else None
        body = {"snippet": {
                    "title": meta["title"][:100],
                    "description": meta.get("description", "")[:5000],
                    "tags": meta.get("tags", [])[:30],
                    "categoryId": meta.get("categoryId", "24")},
                "status": {
                    "privacyStatus": "private" if publish_at else args.privacy,
                    "selfDeclaredMadeForKids": bool(meta.get("madeForKids", False)),
                    **({"containsSyntheticMedia": True} if meta.get("syntheticContent") else {}),
                    **({"publishAt": publish_at} if publish_at else {})}}
        init = (yt.YT_API.replace("/youtube/v3", "/upload/youtube/v3")
                + "/videos?uploadType=resumable&part=snippet,status")
        req = urllib.request.Request(
            init, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Length": str(size),
                     "X-Upload-Content-Type": "video/mp4"})
        with urllib.request.urlopen(req, timeout=120) as r:
            location = r.headers.get("Location")
        if not location:
            raise SystemExit("no resumable session location")
        json.dump({"location": location, "offset": 0}, open(state_path, "w"))
        print(f"session opened: {size} bytes | publishAt: {args.publish_at}")

    f = open(args.video, "rb")
    attempts = 0
    while offset < size:
        try:
            end = min(offset + CHUNK, size)
            f.seek(offset)
            data = f.read(end - offset)
            req = urllib.request.Request(
                location, data=data, method="PUT",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "video/mp4",
                         "Content-Length": str(end - offset),
                         "Content-Range": f"bytes {offset}-{end-1}/{size}"})
            with urllib.request.urlopen(req, timeout=600) as r:
                code, body = r.status, r.read()
            if code == 200 or code == 201:
                offset = end
                json.dump({"location": location, "offset": offset}, open(state_path, "w"))
                print(f"{offset}/{size} ({100*offset/size:.1f}%)", flush=True)
                if offset >= size:
                    result = json.loads(body.decode())
            else:
                raise IOError(f"unexpected status {code}")
        except urllib.error.HTTPError as he:
            if he.code == 308:
                # normal intermediate-chunk ack: server confirmed received bytes
                rng = he.headers.get("Range")
                if rng:
                    newoff = int(rng.split("-")[1]) + 1
                    if newoff > offset:
                        offset = newoff
                        json.dump({"location": location, "offset": offset},
                                  open(state_path, "w"))
                        print(f"{offset}/{size} ({100*offset/size:.1f}%)", flush=True)
                continue
            attempts += 1
            if attempts > 40:
                raise SystemExit(f"too many failures: {he}")
            wait = min(30, 2 * attempts)
            print(f"interruption ({repr(he)[:80]}); probing session in {wait}s", flush=True)
            time.sleep(wait)
            preq = urllib.request.Request(location, data=b"", method="PUT",
                                          headers={"Authorization": f"Bearer {token}",
                                                   "Content-Range": f"bytes */{size}"})
            try:
                with urllib.request.urlopen(preq, timeout=120) as r:
                    rng = r.headers.get("Range", "bytes=0-0")
                    offset = int(rng.split("-")[1]) + 1
            except urllib.error.HTTPError as he2:
                if he2.code == 308:
                    rng = he2.headers.get("Range", "bytes=0-0")
                    offset = int(rng.split("-")[1]) + 1
                else:
                    raise
            print(f"resume from {offset}", flush=True)
        except (urllib.error.URLError, IOError, OSError) as e:
            attempts += 1
            if attempts > 40:
                raise SystemExit(f"too many failures: {e}")
            wait = min(30, 2 * attempts)
            print(f"network error ({repr(e)[:80]}); retry from {offset} in {wait}s", flush=True)
            time.sleep(wait)

    vid = result.get("id", "?")
    st = result.get("status", {})
    print("UPLOAD COMPLETE")
    print("video id :", vid)
    print("status   :", st.get("privacyStatus"), "| uploadStatus:", st.get("uploadStatus"))
    if os.path.exists(state_path):
        os.remove(state_path)
    return vid


if __name__ == "__main__":
    main()
