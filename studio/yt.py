"""YouTube upload — OAuth from environment secrets, resumable upload,
scheduled publish (publishAt in Dubai slot time).

Ported from the studio yt_api.py. Tokens are never printed.
"""
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
DUBAI_TZ = datetime.timezone(datetime.timedelta(hours=config.DUBAI_UTC_OFFSET))

_cache = {"access_token": None, "expires_at": 0}


class YtError(RuntimeError):
    pass


def refresh_access_token() -> str:
    data = urllib.parse.urlencode({
        "client_id": config.YT_CLIENT_ID,
        "client_secret": config.YT_CLIENT_SECRET,
        "refresh_token": config.YT_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise YtError(f"token refresh failed HTTP {e.code}: {e.read().decode()[:200]}")
    _cache["access_token"] = resp["access_token"]
    _cache["expires_at"] = time.time() + int(resp.get("expires_in", 3600)) - 120
    return resp["access_token"]


def access_token() -> str:
    if not _cache["access_token"] or time.time() > _cache["expires_at"]:
        return refresh_access_token()
    return _cache["access_token"]


def next_slot_utc(now: datetime.datetime | None = None) -> datetime.datetime:
    """Next Dubai publish slot (09:00 or 21:00) strictly in the future."""
    now = now or datetime.datetime.now(DUBAI_TZ)
    for add_days in (0, 1):
        for hour in config.PUBLISH_HOURS_DUBAI:
            cand = (now + datetime.timedelta(days=add_days)).replace(
                hour=hour, minute=0, second=0, microsecond=0)
            if cand - now > datetime.timedelta(minutes=45):
                return cand
    raise AssertionError


def upload(video_path: str, meta: dict, publish_at_dubai: datetime.datetime) -> str:
    """Resumable upload; returns video id."""
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta.get("description", "")[:5000],
            "tags": meta.get("tags", [])[:30],
            "categoryId": meta.get("categoryId", "15"),
        },
        "status": {
            "privacyStatus": "private",  # required while publishAt is set
            "selfDeclaredMadeForKids": bool(meta.get("madeForKids", False)),
            "publishAt": publish_at_dubai.astimezone(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    size = __import__("os").path.getsize(video_path)
    token = access_token()
    init_url = (YT_API.replace("/youtube/v3", "/upload/youtube/v3")
                + "/videos?uploadType=resumable&part=snippet,status")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": "video/mp4",
    }
    req = urllib.request.Request(init_url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        location = r.headers.get("Location")
        if not location:
            raise YtError("no resumable session Location returned")
    print(f"resumable session opened: {size} bytes | publishAt Dubai "
          f"{publish_at_dubai.strftime('%Y-%m-%d %H:%M')}", flush=True)
    put = urllib.request.Request(location, data=open(video_path, "rb"), method="PUT",
                                 headers={"Authorization": f"Bearer {access_token()}",
                                          "Content-Type": "video/mp4",
                                          "Content-Length": str(size)})
    with urllib.request.urlopen(put, timeout=5400) as r:
        result = json.loads(r.read().decode())
    vid = result.get("id")
    if not vid:
        raise YtError(f"upload returned no id: {json.dumps(result)[:200]}")
    return vid


def token_probe() -> dict:
    """Verify the OAuth credentials actually refresh (the only check the
    upload-only scope allows; reading channels/videos needs readonly)."""
    token = refresh_access_token()
    return {"ok": bool(token), "scope": "youtube.upload (refresh ok)"}


def channel_stats() -> dict:
    token = access_token()
    req = urllib.request.Request(
        YT_API + "/channels?part=snippet,statistics&mine=true",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read().decode())
    items = j.get("items") or []
    if not items:
        return {}
    c = items[0]
    return {"id": c["id"], "title": c["snippet"].get("title"),
            "subscribers": c["statistics"].get("subscriberCount"),
            "views": c["statistics"].get("viewCount"),
            "videos": c["statistics"].get("videoCount")}
