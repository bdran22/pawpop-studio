#!/usr/bin/env python3
"""Convert the sandbox EP004 production (timeline + VO text) into the repo's
stories/ep004.json with an authored timeline_override, so the GitHub pipeline
renders exactly the studio-authored plan.

Run inside the sandbox:  python3 scripts/import_ep004.py
"""
import json
import sys
from pathlib import Path

SANDBOX = Path("/home/z/my-project/scripts")
EP = Path("/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna")
OUT = Path(__file__).resolve().parent.parent / "stories" / "ep004.json"

sys.path.insert(0, str(SANDBOX))

sys.modules.setdefault("ep003_audio", __import__("importlib").import_module("ep003_audio"))

# VO texts live inside ep004_assemble.py -> import without running it
import ast

src = ast.parse((SANDBOX / "ep004_assemble.py").read_text())
vo_texts = {}
for node in ast.walk(src):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if getattr(t, "id", "") == "VO_TEXTS" and isinstance(node.value, ast.Dict):
                vo_texts = ast.literal_eval(node.value)

tl = json.loads((EP / "ep004_timeline.json").read_text())

# exact motion prompts exported from ep004_engine_a.TASKS
motions = {}
mp = Path(__file__).resolve().parent / "ep004_motions.json"
if mp.exists():
    motions = json.loads(mp.read_text())

shots = []
prev_kf = None
for s in tl["slots"]:
    kf = s["kf"]
    shots.append({
        "id": s["id"], "kf": kf, "seg": s["vo"][0],
        "reuse": s.get("reuse", 0),
        "kf_prompt": "",  # keyframes already exist for EP004
        "motion": motions.get(kf, "Gentle cinematic character motion, warm light, subtle camera."),
        "trans": s.get("trans", "cut"),
    })
    prev_kf = kf

story = {
    "ep": "EP004",
    "title": "The Package Was for Luna | Funny Dog and Cat Surprise Story",
    "style_prefix": ("Pixar-style 3D cartoon: Biscuit, a cream French Bulldog puppy with a "
                     "blue collar; Luna, a tiny fluffy white kitten with big green eyes; "
                     "Max, a golden retriever; Maple Street porch setting"),
    "characters": ["Biscuit the cream French Bulldog puppy", "Luna the tiny white kitten",
                   "Max the golden retriever"],
    "vo": vo_texts,
    "shots": shots,
    "chapters": tl.get("chapters", []),
    "sfx": tl.get("sfx", []),
    "timeline_override": [
        {k: s[k] for k in ("id", "kf", "dur", "vo", "trans", "reuse", "flow")}
        for s in tl["slots"]
    ],
    "meta": {
        "description": (
            "A giant mystery box lands on Biscuit's porch - taller than the mailbox, bigger "
            "than any dream. Biscuit is CERTAIN it is his gift, and he will guard it, claim "
            "it, and out-cute anyone who disagrees... especially Luna, the tiny white kitten "
            "who sits on the box like she owns it. Then the box SCRITCHES from the inside, a "
            "purr rolls out, and one single whisker changes everything: the biggest surprise "
            "on Maple Street was never meant for Biscuit. What happens next turns one giant "
            "box into three thrones - and one grumpy morning into the warmest ending on the "
            "street. A funny dog and cat surprise story about sharing, kindness and the best "
            "kind of teamwork - told in cinematic animation with a warm British storyteller "
            "voice. Perfect for dog lovers, cat lovers and everyone who enjoys funny animal "
            "stories with a happy ending. MEET THE CHARACTERS - Biscuit: a cream French "
            "Bulldog puppy with a blue collar and a heart of gold. Luna: a tiny fluffy white "
            "kitten with big green eyes and royal standards. Max: the neighborhood golden "
            "retriever and chief moral-support officer."),
        "tags": ["dog and cat story", "funny dog cartoon", "animated dog story",
                 "animal surprise story", "puppy and kitten story", "mystery box story",
                 "cat tower cartoon", "funny animal stories", "heartwarming animal story",
                 "french bulldog cartoon", "golden retriever cartoon", "cute animal cartoon",
                 "animal friendship animation", "dog stories", "pet stories for families",
                 "animated pets", "puppy story", "cartoon animals", "funny puppy video",
                 "pawpop stories"],
        "chapter_labels": [
            ["seg01", "The Biggest Box on Maple Street"],
            ["seg06", "The Squeak Inside the Box"],
            ["seg10", "The Name on the Label"],
            ["seg16", "The Claim War"],
            ["seg19", "The Purring Monster"],
            ["seg25", "One Single Whisker"],
            ["seg26", "The Tower Rises"],
            ["seg29", "The Slowest Sad Walk"],
            ["seg33", "Room for One More"],
            ["seg39", "The Tower Has a Knight"],
            ["seg34", "Every Paw Has a Story"],
        ],
    },
}

# seeded VO: point pipeline at sandbox-generated wavs via seed package
OUT.write_text(json.dumps(story, indent=1, ensure_ascii=False))
print("wrote", OUT)
print("vo lines:", len(vo_texts), "| shots:", len(shots),
      "| override slots:", len(story["timeline_override"]),
      "| motions found:", len(motions))
