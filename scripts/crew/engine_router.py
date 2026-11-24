#!/usr/bin/env python3
"""PAWPOP CREW — Animator Unit engine router.

Selects the animation engine for each clip, professionally:
  ENGINE-A  z-ai cogvideox-3   sandbox-only, primary (best quality/cost)
  ENGINE-C  kie.ai (grok image-to-video, proven schema)  needs >= KIE_MIN credits
  ENGINE-D  PixVerse          needs positive balance
If no engine is available the router BLOCKS the task and escalates to the
showrunner. The crew NEVER falls back to static images — that is the owner's
absolute rule (zero-static scenes).

Balance checks are real API calls, cached for ROUTER_TTL seconds, and results
are never fabricated: if a check fails the engine is treated as unavailable.
"""
import json, os, time, urllib.request, urllib.error

SECRETS = "/home/z/my-project/PAWPOP_STUDIO/secrets"
ROUTER_TTL = 900  # 15 min cache for balance checks
KIE_MIN_CREDITS = float(os.environ.get("KIE_MIN_CREDITS", "15"))  # cheapest proven clip = 14.4

_cache = {"t": 0.0, "data": {}}


def _read(name: str) -> str:
    with open(os.path.join(SECRETS, name)) as f:
        return f.read().strip()


def _get(url: str, headers: dict, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return -1, repr(e)[:200]


def _refresh_cache():
    now = time.time()
    if now - _cache["t"] < ROUTER_TTL:
        return
    d = {}
    # kie.ai credit
    try:
        st, body = _get("https://api.kie.ai/api/v1/chat/credit",
                        {"Authorization": f"Bearer {_read('kie_ai_key.txt')}"})
        d["kie_credit"] = float(json.loads(body)["data"]) if st == 200 else None
        d["kie_http"] = st
    except Exception as e:
        d["kie_credit"], d["kie_http"] = None, repr(e)[:80]
    # PixVerse balance (platform endpoint family; 403/blocked -> unavailable)
    try:
        st, body = _get("https://app-api.pixverse.ai/openapi/v1/credit",
                        {"API-KEY": _read("pixverse_key.txt")})
        try:
            d["pixverse_credit"] = float(json.loads(body).get("Resp", {}).get("credit", 0) or 0)
        except Exception:
            d["pixverse_credit"] = None
        d["pixverse_http"] = st
    except Exception as e:
        d["pixverse_credit"], d["pixverse_http"] = None, repr(e)[:80]
    _cache["t"] = now
    _cache["data"] = d


def available_engines(where: str = "sandbox") -> dict:
    """Real availability snapshot. where=sandbox|runner (GitHub Actions)."""
    _refresh_cache()
    d = dict(_cache["data"])
    d["engine_a"] = where == "sandbox"  # z-ai internal API unreachable from runners (verified)
    d["engine_c"] = d.get("kie_credit") is not None and d["kie_credit"] >= KIE_MIN_CREDITS
    d["engine_d"] = (d.get("pixverse_credit") or 0) > 0
    return d


def select(where: str = "sandbox", prefer: str = None) -> str | None:
    """Pick the engine for the next clip: A > C > D, honor prefer override."""
    av = available_engines(where)
    order = [prefer] if prefer in ("engine_a", "engine_c", "engine_d") else ["engine_a", "engine_c", "engine_d"]
    for e in order:
        if av.get(e):
            return e
    return None


def escalation_message(ep_dir: str) -> str:
    """Honest owner-facing escalation when the Animator Unit is capacity-blocked."""
    av = available_engines()
    return (
        f"[Animator Unit BLOCKED] {os.path.basename(ep_dir)}: no animation capacity. "
        f"engine_a(sandbox)={av['engine_a']}, kie credit={av.get('kie_credit')} "
        f"(needs >= {KIE_MIN_CREDITS}/clip), pixverse credit={av.get('pixverse_credit')}. "
        f"Crew will NOT substitute static images. Options: top up kie.ai "
        f"(~14.4 credits per 6s 480p clip ~= $0.07) or wait for z-ai quota reset."
    )


if __name__ == "__main__":
    import sys
    where = sys.argv[1] if len(sys.argv) > 1 else "sandbox"
    print(json.dumps(available_engines(where), indent=1))
