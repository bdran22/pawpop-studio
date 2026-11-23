#!/usr/bin/env python3
"""Upload GitHub Actions secrets via the REST API (requires PyNaCl for the
libsodium sealed box GitHub expects). Values are read from the local sandbox
config — never printed.
"""
import base64
import json
import sys
from pathlib import Path

from nacl import encoding, public

from studio import config


def encrypt(secret: str, key_b64: str) -> str:
    pk = public.PublicKey(key_b64.encode(), encoding.Base64Encoder())
    box = public.SealedBox(pk)
    return base64.b64encode(box.encrypt(secret.encode())).decode()


def main(repo_full: str, token: str):
    import urllib.request

    def api(path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=data, method=method or ("POST" if data else "GET"),
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "pawpop-bootstrap"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    st, = [None]
    key = api(f"/repos/{repo_full}/actions/secrets/public-key")
    z = config.zai_conf()
    secrets = {
        "ZAI_BASE_URL": z.get("baseUrl") or "",
        "ZAI_API_KEY": z.get("apiKey") or "",
        "ZAI_CHAT_ID": z.get("chatId") or "",
        "ZAI_USER_ID": z.get("userId") or "",
        "ZAI_TOKEN": z.get("token") or "",
        "KIE_API_KEY": config.KIE_API_KEY,
        "PIXVERSE_API_KEY": config.PIXVERSE_API_KEY,
        "YT_CLIENT_ID": config.YT_CLIENT_ID,
        "YT_CLIENT_SECRET": config.YT_CLIENT_SECRET,
        "YT_REFRESH_TOKEN": config.YT_REFRESH_TOKEN,
        "YT_CHANNEL_ID": config.YT_CHANNEL_ID,
    }
    missing = [k for k, v in secrets.items() if not v]
    if missing:
        print("MISSING (not uploaded):", missing)
    for name, value in secrets.items():
        if not value:
            continue
        api(f"/repos/{repo_full}/actions/secrets/{name}",
            {"encrypted_value": encrypt(value, key["key"]),
             "key_id": key["key_id"]}, method="PUT")
        print("secret set:", name)
    print("DONE secrets")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
