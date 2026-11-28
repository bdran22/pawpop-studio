#!/usr/bin/env python3
"""PAWPOP producer tick — one command = one durable production step.

Order:
  1. Restore EP005 work assets from GitHub release if the sandbox was recycled.
  2. zai_driver: patient serial submits + parallel polls (rate-limit aware).
  3. studio.pipeline --run: VO/keyframes gaps, HF engines (E/F), completion.
  4. gh_sync.py: push all work assets to GitHub (durability).
  5. Print ONE status line (used by cron replies).

Never uploads anything (SLOT PUBLISHER owns uploads). Never fabricates.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/z/my-project/pawpop-studio")
ASSETS = ROOT / "work/assets/ep005"
ENV = dict(**__import__("os").environ,
           PAWPOP_VO_PROVIDER="edge",
           PAWPOP_ENABLE_ENGINE_K="1")


def run(cmd, timeout, **kw):
    return subprocess.run(cmd, cwd=str(ROOT), timeout=timeout,
                          capture_output=True, text=True, env=ENV, **kw)


def counts():
    vo = len(list((ASSETS / "vo").glob("*"))) if (ASSETS / "vo").exists() else 0
    kf = len(list((ASSETS / "keyframes").glob("*.png"))) if (ASSETS / "keyframes").exists() else 0
    cl = len(list((ASSETS / "clips").glob("*.mp4"))) if (ASSETS / "clips").exists() else 0
    return vo, kf, cl


def main():
    t0 = time.time()
    vo, kf, cl = counts()
    restored = ""
    if kf == 0 and vo == 0:
        r = run([sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.');"
                 "from studio.pipeline import restore_assets_from_github;"
                 "from studio import config;"
                 "import shutil, pathlib;"
                 "d = config.ASSETS/'ep005';"
                 "print('restore:', restore_assets_from_github('EP005'))"],
                timeout=560)
        restored = f" restore={r.stdout.strip()[-40:]}" if r.returncode == 0 else ""
        # flatten nested dir if the archive was double-wrapped
        nested = ASSETS / "ep005"
        if nested.exists():
            for p in nested.iterdir():
                shutil_move(p)
            nested.rmdir()
        vo, kf, cl = counts()

    # zai patient driver (submits are rate limited; polls are free)
    zout = run([sys.executable, "-m", "studio.zai_driver", "--budget-min", "7"],
               timeout=500)
    # pipeline (HF E/F engines, VO/KF gaps, auto-finish)
    pout = run([sys.executable, "-m", "studio.pipeline", "--run",
                "--produce-budget-min", "4"], timeout=400)

    # durability push
    sync = run([sys.executable, "/home/z/my-project/scripts/gh_sync.py"],
               timeout=560)
    sync_line = (sync.stdout.strip().splitlines() or [""])[-1][:60]

    vo, kf, cl = counts()
    st = {}
    try:
        st = json.loads((ROOT / "state/state.json").read_text()).get("episodes", {}).get("EP005", {})
    except Exception:
        pass
    line = (f"PRODUCER vo={vo} kf={kf} clips={cl}/38 "
            f"status={st.get('status', 'producing')} "
            f"zai={'ok' if zout.returncode == 0 else 'rc' + str(zout.returncode)} "
            f"sync='[{sync_line}]'{restored} ({time.time()-t0:.0f}s)")
    print(line)
    # verbose tail for the log
    for tag, out in (("ZAI", zout), ("PIPE", pout)):
        tail = [ln for ln in (out.stdout or "").strip().splitlines() if ln][-3:]
        for ln in tail:
            print(f"  {tag} {ln[:110]}")


def shutil_move(p):
    import shutil
    dest = ASSETS / p.name
    if not dest.exists():
        shutil.move(str(p), str(dest))


if __name__ == "__main__":
    main()
