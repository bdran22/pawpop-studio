#!/usr/bin/env python3
"""EP004 ENGINE-E batch: free Wan2.2 14B i2v via Hugging Face Space (gradio_client).

Owner directive 2026-11-25: use a FREE engine, full authority granted. This
space is public/free (no key), reachable from sandbox AND GitHub runners, and
produces real animated motion (QC motion gate verified: mean_diff 7.5).

For each missing keyframe:
  1. call the space with the episode's motion prompt (same TASKS as ENGINE-A)
  2. normalize the 480x832@16fps result to 1080x1920@30fps (lanczos, sar 1)
  3. mark engine_a_status.json state=ok engine='wan22-hf-free'

Resumable: skips existing clips. Retries each kf up to 2 times on failure.
"""
import json, os, subprocess, sys, time

EP = "/home/z/my-project/download/PAWPOP/EP004_The_Package_Was_for_Luna"
KF = os.path.join(EP, "keyframes")
CLIPS = os.path.join(EP, "clips")
STATUS = os.path.join(EP, "engine_a_status.json")
SPACE = os.environ.get("WAN_SPACE", "Upsampler/wan-2-2-14b-image-to-video")

sys.path.insert(0, "/home/z/my-project/scripts")


def need_list() -> list:
    st = json.load(open(STATUS))
    return [k for k, v in st.items() if v.get("state") != "ok"]


def main():
    from gradio_client import Client, handle_file
    import ep004_engine_a as batch
    tasks = dict(batch.TASKS)
    todo = [k for k in need_list() if k in tasks]
    tok_path = "/home/z/my-project/PAWPOP_STUDIO/secrets/hf_token.txt"
    tok = ""
    if os.path.exists(tok_path):
        tok = open(tok_path).read().strip()
    print(f"[ENGINE-E] {len(todo)} clips to animate on {SPACE}: {todo}", flush=True)
    print(f"[ENGINE-E] HF auth: {'yes (bdran22 token, higher ZeroGPU quota)' if tok else 'NO - anonymous quota'}", flush=True)
    if not todo:
        return
    c = Client(SPACE, token=tok or None, verbose=False)
    st = json.load(open(STATUS))
    for kf in todo:
        out = os.path.join(CLIPS, kf + ".mp4")
        if os.path.exists(out) and os.path.getsize(out) > 200000:
            st[kf] = {"state": "ok", "engine": "wan22-hf-free", "file": out}
            json.dump(st, open(STATUS, "w"), indent=1)
            continue
        prompt = tasks[kf]
        ok = False
        for attempt in (1, 2):
            t0 = time.time()
            try:
                res = c.predict(
                    input_image=handle_file(os.path.join(KF, kf + ".png")),
                    prompt=prompt, api_name="/generate_video")
                raw = res[0]
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", raw,
                     "-vf", "scale=1080:1920:flags=lanczos,setsar=1,fps=30",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                     "-an", out], check=True, timeout=300)
                sz = os.path.getsize(out)
                if sz > 150000:
                    st[kf] = {"state": "ok", "engine": "wan22-hf-free", "file": out,
                              "note": f"raw 480x832@16 upscaled, {sz/1e6:.1f}MB, {time.time()-t0:.0f}s"}
                    json.dump(st, open(STATUS, "w"), indent=1)
                    print(f"[ENGINE-E] OK {kf} ({sz/1e6:.1f} MB, {time.time()-t0:.0f}s)", flush=True)
                    ok = True
                    break
            except Exception as e:
                print(f"[ENGINE-E] attempt {attempt} FAIL {kf}: {repr(e)[:160]}", flush=True)
                time.sleep(20)
        if not ok:
            st[kf] = {"state": "engine_e_failed", "engine": "wan22-hf-free"}
            json.dump(st, open(STATUS, "w"), indent=1)
    remaining = need_list()
    print(f"[ENGINE-E] done. still missing: {len(remaining)} {remaining}", flush=True)


if __name__ == "__main__":
    main()
