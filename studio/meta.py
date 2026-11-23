"""SEO metadata builder — reads the real timeline to emit true chapter
timestamps. Never fabricated. Owner directive 2026-11-22: no AI/synthetic
wording in public metadata, no synthetic-content disclosure flag (stylized
cartoon animation is outside YouTube's realistic-synthetic requirement).
Music is original synthesis — no third-party credit needed.
"""
import json


def _srt_time(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build(story: dict, tl: dict, channel_handle: str) -> dict:
    seg_start = {}
    for i, s in enumerate(tl["slots"]):
        if s.get("vo") and s["vo"][0] not in seg_start:
            seg_start[s["vo"][0]] = tl["starts"][i]

    lines = []
    for seg, label in (story.get("meta", {}).get("chapter_labels") or [])[:12]:
        if seg in seg_start:
            lines.append(f"{_srt_time(seg_start[seg])} {label}")

    desc = story["meta"]["description"].strip()
    if lines:
        desc += "\n\nCHAPTERS\n" + "\n".join(lines)
    desc += (f"\n\nWATCH NEXT\nSubscribe to PAWPOP STORIES for more animated "
             f"animal stories:\nhttps://www.youtube.com/{channel_handle}\n\n"
             "Every Paw Has a Story!\n\n#dogandcat #funnydog #animatedstory "
             "#puppystory #cartoonanimals\n\nMusic: original score composed "
             "for PAWPOP STORIES.")

    return {
        "episode": story["ep"],
        "title": story["title"][:100],
        "description": desc[:5000],
        "tags": (story.get("meta", {}).get("tags") or [])[:30],
        "categoryId": "15",
        "category_label": "Pets & Animals",
        "madeForKids": False,
        "privacyDefault": "private",
        "duration_seconds": round(tl["total"], 1),
        "source": f"Studio pipeline {story['ep']} — chapters from real timeline",
    }
