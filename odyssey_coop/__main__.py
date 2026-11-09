"""Odyssey ghost co-op.

  python -m odyssey_coop scan                       find your player's position in the running game
  python -m odyssey_coop watch                      print the live position from the saved address
  python -m odyssey_coop host --name Alec           host a session (UDP port 27015)
  python -m odyssey_coop join HOST --name Sam       join a friend's session
  python -m odyssey_coop record --seconds 60        record your real walk for a replay bot
  add --bot circle|orbit|follow|replay to host/join to play as a bot; --no-overlay prints to the console
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

LOCAL = Path(__file__).resolve().parent.parent / "local"
CFG = LOCAL / "position.json"

SCAN_HELP = """
Commands (do the action in-game first, then type the command):
  s   snapshot - stand still, then take the first snapshot
  u   you went UP (climbed a wall / ledge, at least ~1 m)
  d   you went DOWN (dropped off a ledge)
  =   you did not move at all since the last command
  w   walk check - walk a few metres on flat ground, keeps candidates whose x/y moved
  l   list candidates (shows x, y, z)
  save N   save candidate N from the list
  q   quit
Loop u / = / d / = until fewer than ~20 candidates are left, then w, then l and save.
"""


def cmd_scan(args):
    from .memscan import Process, Scanner, find_pid
    from .sources import GAME_EXE

    pid = args.pid or find_pid(GAME_EXE)
    if pid is None:
        raise SystemExit(f"{GAME_EXE} is not running. Start the game, load your save, then run scan again.")
    proc = Process(pid)
    sc = Scanner(proc, Path(tempfile.gettempdir()) / "odyssey_coop_snapshot")
    print(f"attached read-only to pid {pid}")
    print(SCAN_HELP)
    listed = []
    while True:
        try:
            line = input(f"[{sc.count() if sc.regions else 0:,} candidates] > ").strip().split()
        except EOFError:
            break
        if not line:
            continue
        c = line[0]
        if c == "q":
            break
        try:
            listed = scan_step(c, line, proc, sc, pid, listed)
        except Exception as e:  # a bad command must never throw away the candidates
            print(f"error: {e!r}")


def scan_step(c, line, proc, sc, pid, listed):
        if c == "s":
            print(f"snapshot: {sc.snapshot() / 2**30:.1f} GiB read")
        elif not sc.regions:
            print("take a snapshot first (s)")
        elif c in ("u", "d", "="):
            sc.filter({"u": "up", "d": "down", "=": "same"}[c], lo=0.5, hi=60.0)
        elif c == "w":
            before = sc.read_xy()
            input("now walk a few metres on flat ground, then press Enter")
            sc.walk_filter(before, sc.read_xy())
        elif c == "dump" and len(line) == 2:
            print(f"dumped {sc.dump(line[1])} addresses to {line[1]}")
        elif c == "l":
            listed = sc.addresses(limit=40)
            for n, a in enumerate(listed):
                v = proc.read_floats(a - 8, 3)
                print(f"  {n:2}  0x{a - 8:X}  " + ("unreadable" if v is None else "x={:.2f} y={:.2f} z={:.2f}".format(*v)))
        elif c == "save" and len(line) == 2 and listed:
            a = listed[int(line[1])] - 8
            LOCAL.mkdir(exist_ok=True)
            CFG.write_text(json.dumps({"address": f"{a:X}", "pid": pid, "saved": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
            print(f"saved 0x{a:X} to {CFG}")
        else:
            print(SCAN_HELP)
        return listed


def cmd_pointers(args):
    """Find pointer paths from the exe's static data to the saved position copies."""
    from collections import Counter
    from . import pointers
    from .sources import GameSource
    src = GameSource(CFG)
    here = src.position()
    if here is None:
        raise SystemExit("saved addresses no longer give a position; rerun scan first")
    good = [a for a in src._addresses() if src.proc.read_floats(a, 3) == here]
    print(f"{len(good)} saved copies hold the current position {[round(c, 2) for c in here]}", flush=True)
    base, paths = pointers.find_paths(src.proc, good, depth=args.depth, log=lambda m: print(m, flush=True))
    ok = []
    for pth in paths:
        a = pointers.resolve(src.proc, base, pth)
        if a is not None and src.proc.read_floats(a, 3) == here:
            ok.append(pth)
    ok.sort(key=lambda p: (len(p["offsets"]), p["module_offset"]))
    print(f"{len(paths)} paths found, {len(ok)} resolve to the position right now", flush=True)
    by_len = Counter(len(p["offsets"]) for p in ok)
    print("by length:", dict(sorted(by_len.items())), flush=True)
    out = LOCAL / "paths.json"
    out.write_text(json.dumps({"paths": [{"module_offset": hex(p["module_offset"]), "offsets": [hex(o) for o in p["offsets"]]} for p in ok],
                               "found": time.strftime("%Y-%m-%d %H:%M"), "pid": src.proc.pid}, indent=1))
    print(f"saved to {out}", flush=True)


def cmd_check_paths(args):
    """After a game restart: resolve every saved path, find the position most of them
    agree on, and keep only the paths that still lead there."""
    from collections import Counter
    from . import pointers
    from .memscan import Process, find_pid
    from .sources import GAME_EXE
    pid = find_pid(GAME_EXE)
    if pid is None:
        raise SystemExit(f"{GAME_EXE} is not running")
    proc = Process(pid)
    base = pointers.main_module(proc)[0]
    f = LOCAL / "paths.json"
    data = json.loads(f.read_text())
    res = []
    for p in data["paths"]:
        pth = {"module_offset": int(p["module_offset"], 16), "offsets": [int(o, 16) for o in p["offsets"]]}
        a = pointers.resolve(proc, base, pth)
        v = proc.read_floats(a, 3) if a else None
        ok = v is not None and all(abs(c) < 1e5 for c in v) and any(c != 0 for c in v) and all(c == c for c in v)
        res.append((p, tuple(round(c, 1) for c in v) if ok else None))
    votes = Counter(v for _, v in res if v)
    print(f"{len(res)} paths: {sum(1 for _, v in res if v)} resolve to a plausible position", flush=True)
    for v, n in votes.most_common(5):
        print(f"  {n:5} paths agree on {v}", flush=True)
    if not votes:
        raise SystemExit("no path survived; rerun scan + pointers")
    best = votes.most_common(1)[0][0]
    keep = [p for p, v in res if v == best]
    if args.prune:
        data["paths"] = keep
        data["checked"] = time.strftime("%Y-%m-%d %H:%M")
        f.write_text(json.dumps(data, indent=1))
        print(f"kept {len(keep)} paths that agree", flush=True)


def cmd_camscan(args):
    """Find the camera: stand still, then swing the camera around when told."""
    from . import pointers
    from .camera import run_scan
    from .sources import GameSource
    game = GameSource(CFG)
    base, size = pointers.main_module(game.proc)
    regions = [(b, s) for b, s in pointers.scan_regions(game.proc, base, size)]
    go = LOCAL / "camscan.go"
    go.unlink(missing_ok=True)
    res = run_scan(game, regions, go, lambda m: print(m, flush=True),
                   rotate_seconds=args.rotate, window_seconds=args.window)
    go.unlink(missing_ok=True)
    out = LOCAL / "camscan.json"
    out.write_text(json.dumps(res, indent=1))
    for c in res.get("candidates", [])[:10]:
        print(f"  0x{c['addr']:X} motion {c['motion']} m, distance {c['dist']} m (std {c['dist_std']}), "
              f"orientation blocks {len(c['orientation'])}", flush=True)
    print(f"saved {out}", flush=True)


def cmd_watch(args):
    from .sources import GameSource
    src = GameSource(CFG)
    while True:
        p = src.position()
        print("no position" if p is None else "x={:9.2f}  y={:9.2f}  z={:8.2f}".format(*p), end="\r")
        time.sleep(0.1)


def cmd_record(args):
    """Record your real movement to a file that a ReplayBot can walk later."""
    from .sources import GameSource
    src = GameSource(CFG)
    out = Path(args.out)
    n, t_end = 0, time.time() + args.seconds
    with out.open("w") as f:
        print(f"recording {args.seconds:.0f}s to {out}; move around in the game", flush=True)
        while time.time() < t_end:
            p = src.position()
            if p is not None:
                f.write(json.dumps([round(time.time(), 3)] + [round(c, 3) for c in p]) + "\n")
                n += 1
            time.sleep(0.05)
    print(f"saved {n} samples", flush=True)


def make_source(args):
    from .sources import CircleBot, FollowBot, GameSource, OrbitBot, ReplayBot
    bot = "circle" if args.fake else args.bot
    if bot == "circle":
        return CircleBot(seed=args.seed)
    if bot == "replay":
        if not args.replay:
            raise SystemExit("--bot replay needs --replay FILE (make one with `record`)")
        return ReplayBot(args.replay, offset=(args.side, 0.0, 0.0))
    game = GameSource(CFG, wait=True)
    if bot == "orbit":
        return OrbitBot(game, radius=args.radius)
    if bot == "follow":
        return FollowBot(game, delay=args.delay, side=args.side)
    return game


def run_peer(args, host):
    from .net import Peer
    src = make_source(args)
    peer = Peer(args.name, src, host=host, port=args.port)
    peer.start()
    print(("hosting on UDP port %d" % args.port) if host is None else f"joining {host}:{args.port}", "as", args.name)
    t_end = time.time() + args.duration if args.duration else None
    try:
        if args.no_overlay:
            while t_end is None or time.time() < t_end:
                snap = peer.snapshot()
                print(json.dumps({"t": round(time.time(), 2), "others": {n: [round(c, 1) for c in p] for n, p in snap.items()}}), flush=True)
                time.sleep(1.0)
        else:
            from .overlay import Minimap
            Minimap(peer, args.flip_x, args.flip_y).run()
    except KeyboardInterrupt:
        pass
    finally:
        peer.stop()


def main():
    ap = argparse.ArgumentParser(prog="odyssey_coop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--pid", type=int)
    sub.add_parser("watch")
    pp = sub.add_parser("pointers")
    pp.add_argument("--depth", type=int, default=5)
    cp = sub.add_parser("check-paths")
    cp.add_argument("--prune", action="store_true")
    for name in ("host", "join"):
        p = sub.add_parser(name)
        if name == "join":
            p.add_argument("host")
        p.add_argument("--name", required=True)
        p.add_argument("--port", type=int, default=27015)
        p.add_argument("--fake", action="store_true", help="same as --bot circle")
        p.add_argument("--bot", choices=["circle", "orbit", "follow", "replay"],
                       help="play as a bot: circle (no game), orbit / follow (around your live game position), replay (a recording)")
        p.add_argument("--replay", help="recording file for --bot replay")
        p.add_argument("--radius", type=float, default=25.0, help="orbit radius in metres")
        p.add_argument("--delay", type=float, default=3.0, help="follow delay in seconds")
        p.add_argument("--side", type=float, default=3.0, help="follow / replay side offset in metres")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--no-overlay", action="store_true")
        p.add_argument("--duration", type=float, default=0, help="stop after N seconds (tests)")
        p.add_argument("--flip-x", action="store_true")
        p.add_argument("--flip-y", action="store_true")
    cs = sub.add_parser("camscan")
    cs.add_argument("--rotate", type=float, default=20)
    cs.add_argument("--window", type=float, default=15)
    r = sub.add_parser("record")
    r.add_argument("--out", default=str(LOCAL / "walk.jsonl"))
    r.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    if a.cmd == "camscan":
        cmd_camscan(a)
    elif a.cmd == "record":
        LOCAL.mkdir(exist_ok=True)
        cmd_record(a)
    elif a.cmd == "scan":
        cmd_scan(a)
    elif a.cmd == "check-paths":
        cmd_check_paths(a)
    elif a.cmd == "pointers":
        cmd_pointers(a)
    elif a.cmd == "watch":
        cmd_watch(a)
    else:
        run_peer(a, None if a.cmd == "host" else a.host)


if __name__ == "__main__":
    main()
