"""PAWPOP STUDIO — central config (GitHub Actions / local sandbox compatible).

All credentials come from environment variables (GitHub Actions secrets).
On the local sandbox, /etc/.z-ai-config is used as a fallback so the same
code runs in both places. Credentials are never printed.
"""
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.environ.get("PAWPOP_WORK", REPO_ROOT / "work"))
ASSETS = WORK_DIR / "assets"
STATE_PATH = REPO_ROOT / "state" / "state.json"

# ------------------------------------------------------------------ channel
CHANNEL_HANDLE = "@pawpopstorieskids"
DUBAI_UTC_OFFSET = 4  # Asia/Dubai, no DST
PUBLISH_HOURS_DUBAI = (9, 21)          # two slots per day, every 12 hours
QC_GATE = 8.5                          # honest quality gate
TARGET_MIN_SECONDS = 480               # >= 8 min unlocks mid-roll ads
FPS = 30
W, H = 1080, 1920
FADE_DUR = 0.35

# production budget inside a 6h GitHub job (minutes)
PRODUCE_BUDGET_MIN = int(os.environ.get("PAWPOP_PRODUCE_BUDGET_MIN", "265"))
ASSEMBLE_BUDGET_MIN = 55

# ------------------------------------------------------------------ Z.ai (ENGINE-A + TTS + LLM + images)
def zai_conf() -> dict:
    """Z.ai REST config: env first, /etc/.z-ai-config fallback (sandbox)."""
    conf = {
        "baseUrl": os.environ.get("ZAI_BASE_URL", "").rstrip("/") or None,
        "apiKey": os.environ.get("ZAI_API_KEY") or None,
        "chatId": os.environ.get("ZAI_CHAT_ID") or None,
        "userId": os.environ.get("ZAI_USER_ID") or None,
        "token": os.environ.get("ZAI_TOKEN") or None,
    }
    if not conf["apiKey"]:
        p = Path("/etc/.z-ai-config")
        if p.exists():
            c = json.loads(p.read_text())
            conf = {
                "baseUrl": c["baseUrl"].rstrip("/"),
                "apiKey": c["apiKey"],
                "chatId": c.get("chatId"),
                "userId": c.get("userId"),
                "token": c.get("token"),
            }
    return conf


ZAI_VIDEO_MODEL = "cogvideox-3"
ZAI_TTS_VOICE = os.environ.get("PAWPOP_TTS_VOICE", "jam")

# ------------------------------------------------------------------ kie.ai (ENGINE-C)
KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
KIE_BASE = "https://api.kie.ai"

# ------------------------------------------------------------------ PixVerse (image hosting / ENGINE-D)
PIXVERSE_API_KEY = os.environ.get("PIXVERSE_API_KEY", "")
PIXVERSE_BASE = "https://app-api.pixverse.ai"

# ------------------------------------------------------------------ YouTube
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
YT_CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "")

# ------------------------------------------------------------------ engine switches
ENABLE_ENGINE_C = os.environ.get("PAWPOP_ENABLE_ENGINE_C", "1") == "1"
ENABLE_ENGINE_D = os.environ.get("PAWPOP_ENABLE_ENGINE_D", "0") == "1"


def ensure_dirs():
    for p in (WORK_DIR, ASSETS):
        p.mkdir(parents=True, exist_ok=True)
