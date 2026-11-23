"""Story JSON authoring via LLM.

The story JSON is the single source of truth for an episode: narration
lines, shots (keyframe + motion prompts), chapters (music moods), SFX,
and SEO metadata. EP004 is committed as an authored example; EP005+ are
generated at run time and validated against the schema below. Invalid
stories fail the run honestly (never silently patched).
"""
import json
import re

from . import config, zai

SCHEMA_HINT = """
{
 "ep": "EP005",
 "title": "<EN SEO title, <= 95 chars>",
 "style_prefix": "Pixar-style 3D cartoon, cinematic lighting, <locked character sheet: names, colors, accessories>",
 "characters": ["..."],
 "vo": {"seg01": "<narrator line ~20 words>", "...": "44 lines total seg01..seg44"},
 "shots": [
   {"id": "S01", "kf": "K01_hook", "seg": "seg01", "reuse": 0,
    "kf_prompt": "<keyframe scene description, no character names only - describe looks>",
    "motion": "<image-to-video motion prompt: who moves how, camera, light>",
    "kind": "trim|pingpong", "zoom": 1.0, "trans": "cut|fade"}
 ],
 "chapters": [{"t": 0, "mode": "warm|comedy|dramatic|bright|rain|lullaby|outro", "intensity": 0.6}],
 "sfx": [{"kind": "squeak|whoosh|woof|boop|knock|purr|ding|chime|thud|sparkle", "at": 12.5}],
 "meta": {"description": "<200+ word SEO description>",
          "tags": ["...", "..."],
          "chapter_labels": [["seg01", "Opening label"], ...]}
}
"""

PROMPT = """You are the head writer of PAWPOP STORIES, a Pixar-quality 3D animated
animal-story channel (episodes are 8+ minutes, 44 narration segments, vertical 9:16).

Write a NEW episode: {ep}.

RULES
- Funny, warm, family-friendly animal story with a clear arc: hook -> problem ->
  escalating comedy -> surprising turn -> heartwarming resolution + outro CTA.
- Exactly 44 narration lines keyed seg01..seg44. seg34 is always the outro:
  "Pawpop. Every paw has a story. See you on the next adventure. Subscribe so you never miss one."
- seg35..seg44 continue the cozy epilogue after the outro (montage of the ending).
- Each narration line: 15-30 words, British storyteller voice, punchy and rhythmic.
- 37 distinct keyframes K01..K37 (ids like K01_hook, K02_dawn...). Reuse keyframes
  with "reuse" flag so 44 shots cover 37 keyframes (shots S01..S44).
- Every shot: kf_prompt (full scene, locked character looks repeated), motion
  (what moves: tails, ears, paws, camera push-in, light), kind (trim for <=9s,
  pingpong for longer), zoom 1.0-1.35, trans mostly "cut" with a few "fade".
- Music chapters: 8-10 entries covering the whole runtime by feel (t in seconds
  assuming ~11s per shot): warm/comedy/dramatic/bright/rain/lullaby/outro.
- SFX: 15-25 cues with "at" seconds matching the story beats.
- SEO meta: description 200+ words (no AI wording, professional YouTube SEO),
  15-25 tags, 10 chapter labels mapped to seg numbers.

Return ONLY the JSON object, no markdown fences.

JSON SCHEMA
{schema}"""


def validate(story: dict) -> list[str]:
    errs = []
    authored = bool(story.get("timeline_override"))
    vo = story.get("vo") or {}
    shots = story.get("shots") or []
    if len(vo) < 40:
        errs.append(f"vo has {len(vo)} lines, need 44")
    for i in range(1, 45):
        k = f"seg{i:02d}"
        if k not in vo or len(str(vo[k]).split()) < 8:
            errs.append(f"missing/short {k}")
    if len(shots) < 40:
        errs.append(f"only {len(shots)} shots, need 44")
    covered = {s.get("seg") for s in shots}
    for i in range(1, 45):
        if f"seg{i:02d}" not in covered:
            errs.append(f"seg{i:02d} not covered by any shot")
    for s in shots:
        if not s.get("kf") or not s.get("motion"):
            errs.append(f"shot {s.get('id')} missing kf/motion")
        # kf_prompt only mandatory for generated (non-authored) episodes
        if not authored and not s.get("kf_prompt"):
            errs.append(f"shot {s.get('id')} missing kf_prompt")
    if not story.get("chapters"):
        errs.append("no chapters")
    if not (story.get("meta") or {}).get("description"):
        errs.append("no meta.description")
    return errs


def generate(ep: str) -> dict:
    raw = zai.chat(PROMPT.format(ep=ep, schema=SCHEMA_HINT), max_tokens=16000)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("LLM returned no JSON object")
    story = json.loads(m.group(0))
    story["ep"] = ep
    errs = validate(story)
    if errs:
        raise ValueError("story invalid: " + "; ".join(errs[:8]))
    # persist: the story is the single source of truth for all later cycles
    out = config.REPO_ROOT / "stories" / f"{ep.lower()}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(story, indent=1, ensure_ascii=False))
    print(f"[storygen] wrote {out} ({len(story.get('shots', []))} shots)", flush=True)
    return story
