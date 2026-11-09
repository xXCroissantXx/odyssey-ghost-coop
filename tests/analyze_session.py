"""Summarise local/session.jsonl from ingame_session.py: gaps in your position (loading
screens, menus, game closed), teleports (fast travel), re-attaches, and whether the bots
kept up."""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(path=ROOT / "local" / "session.jsonl"):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not rows:
        print("empty session")
        return
    t0 = rows[0]["t"]
    print(f"{len(rows)} samples over {rows[-1]['t'] - t0:.0f}s")
    gap_start, last_me, last_t = None, None, None
    dist = 0.0
    for r in rows:
        ts = r["t"] - t0
        if r["me"] is None:
            if gap_start is None:
                gap_start = ts
        else:
            if gap_start is not None:
                print(f"  {gap_start:6.0f}s  no position for {ts - gap_start:.1f}s (loading / menu / game closed?)")
                gap_start = None
            if last_me is not None:
                step = math.dist(r["me"], last_me)
                if step > 100:
                    print(f"  {ts:6.0f}s  jumped {step:.0f} m in {ts - last_t:.1f}s (fast travel?)")
                else:
                    dist += step
            last_me, last_t = r["me"], ts
        if not r["alive"]:
            print(f"  {ts:6.0f}s  a network thread died!")
    if gap_start is not None:
        print(f"  {gap_start:6.0f}s  no position until the end")
    print(f"distance moved (excluding jumps): {dist:.0f} m; game attached {rows[-1]['attaches']} time(s)")
    names = sorted({n for r in rows for n in r["bots"]})
    last = rows[-1]
    if last.get("overlay"):
        o = last["overlay"]
        print(f"world overlay: {o['frames']} frames drawn, camera missing in {o['no_camera']}")
        from collections import Counter
        anims = Counter(s for r in rows if r.get("anim") for s in r["anim"].values())
        print("animation states seen:", dict(anims))
    for bot in names:
        have = [r for r in rows if r["me"] and bot in r["bots"]]
        fresh = sum(1 for r in have if r["bots"][bot]["age"] < 1.0)
        with_me = sum(1 for r in rows if r["me"])
        d = [math.dist(r["bots"][bot]["pos"][:2], r["me"][:2]) for r in have]
        med = sorted(d)[len(d) // 2] if d else float("nan")
        print(f"{bot}: visible in {len(have)}/{with_me} samples with a position, fresh {fresh}, median distance {med:.1f} m")


if __name__ == "__main__":
    main(*sys.argv[1:])
