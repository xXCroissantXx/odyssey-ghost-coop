"""In-game session with bot friends (needs the game running and local/paths.json).

You host from your real game. Up to three bots join over UDP like real friends would (4 players):
  "Shadow" - follows your real path 3 s behind, 3 m to your right
  "Orbit"  - circles you at 12 m
  "Walker" - replays your previous recorded walk, moved to start 6 m east of you
The minimap shows them. With local/camera_paths.json each one is also drawn as an animated
figure in the game view.

Every 0.5 s the session logs to local/session.jsonl. Your path is recorded to local/walk.jsonl
(the previous one is kept as local/walk-prev.jsonl and is what Walker replays).
Double-click the minimap to stop.
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop.net import Peer  # noqa: E402
from odyssey_coop.overlay import Minimap  # noqa: E402
from odyssey_coop.sources import FollowBot, GameSource, OrbitBot, ReplayBot  # noqa: E402

LOCAL = ROOT / "local"
PORT = 27015


def main():
    cfg = LOCAL / "position.json"
    game = GameSource(cfg, wait=True)
    host = Peer("you", game, port=PORT)
    # each bot reads the game through its own GameSource, like a separate program would
    peers = [host,
             Peer("Shadow", FollowBot(GameSource(cfg, wait=True), delay=3.0, side=3.0), host="127.0.0.1", port=PORT),
             Peer("Orbit", OrbitBot(GameSource(cfg, wait=True), radius=12.0), host="127.0.0.1", port=PORT)]
    walk, prev = LOCAL / "walk.jsonl", LOCAL / "walk-prev.jsonl"
    here = game.position()
    if walk.exists() and walk.stat().st_size and here:
        walk.replace(prev)
        first = json.loads(prev.read_text().splitlines()[0])[1:4]
        bot = ReplayBot(prev, offset=(here[0] - first[0] + 6.0, here[1] - first[1], here[2] - first[2]))
        bot.build = game.build
        peers.append(Peer("Walker", bot, host="127.0.0.1", port=PORT))
    for p in peers:
        p.start()

    mm = Minimap(host)
    world = None
    if (LOCAL / "camera_paths.json").exists():
        from odyssey_coop.camera import CameraSource
        from odyssey_coop.world_overlay import WorldOverlay
        world = WorldOverlay(mm.root, host, CameraSource(game, LOCAL / "camera_paths.json"))
        world.start()

    stop = threading.Event()

    def log():
        with open(LOCAL / "session.jsonl", "w") as f, open(walk, "w") as rec:
            while not stop.is_set():
                now = time.time()
                with host.lock:
                    me = host.players.get(host.assigned)
                    mine = me["pos"] if me and now - me["seen"] < 1.0 and me["state"] == "ok" else None
                meta = host.snapshot(meta=True)
                f.write(json.dumps({
                    "t": round(now, 2), "me": mine, "attaches": game.attaches, "game_state": game.state,
                    "bots": {n: {"pos": [round(c, 2) for c in m["pos"]], "age": round(m["age"], 2), "state": m["state"]}
                             for n, m in meta.items()},
                    "alive": all(p.alive() for p in peers),
                    "overlay": dict(world.stats) if world else None,
                    "anim": {n: a.state for n, a in world.anim.items()} if world else None}) + "\n")
                f.flush()
                if mine:
                    rec.write(json.dumps([round(now, 3)] + [round(c, 3) for c in mine]) + "\n")
                time.sleep(0.5)

    threading.Thread(target=log, daemon=True).start()
    print(f"session running with {len(peers) - 1} bots; double-click the minimap to stop", flush=True)
    try:
        mm.run()
    finally:
        stop.set()
        for p in peers:
            p.stop()
        print("session ended", flush=True)


if __name__ == "__main__":
    main()
