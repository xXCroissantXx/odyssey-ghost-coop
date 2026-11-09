"""In-game session with bot friends (needs the game running and local/paths.json).

You host from your real game. Two bots join over UDP like real friends would:
  "Shadow" - follows your real path 3 s behind, 3 m to your right
  "Orbit"  - circles you at 25 m
The minimap shows both. Every 0.5 s the session logs your position, what the bots look like
from the host, and how often the game was re-attached, to local/session.jsonl. Your path
is also recorded to local/walk.jsonl, for `--bot replay` later. Double-click the minimap to stop.
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
from odyssey_coop.sources import FollowBot, GameSource, OrbitBot  # noqa: E402

LOCAL = ROOT / "local"
PORT = 27015


def main():
    game = GameSource(LOCAL / "position.json", wait=True)
    host = Peer("you", game, port=PORT)
    # each bot reads the game through its own GameSource, like a separate program would
    shadow = Peer("Shadow", FollowBot(GameSource(LOCAL / "position.json", wait=True), delay=3.0, side=3.0), host="127.0.0.1", port=PORT)
    orbit = Peer("Orbit", OrbitBot(GameSource(LOCAL / "position.json", wait=True), radius=25.0), host="127.0.0.1", port=PORT)
    for p in (host, shadow, orbit):
        p.start()
    stop = threading.Event()

    def log():
        with open(LOCAL / "session.jsonl", "w") as f, open(LOCAL / "walk.jsonl", "w") as walk:
            while not stop.is_set():
                now = time.time()
                with host.lock:
                    me = host.players.get(host.assigned)
                    mine = me["pos"] if me and now - me["seen"] < 1.0 else None
                meta = host.snapshot(meta=True)
                f.write(json.dumps({"t": round(now, 2), "me": mine, "attaches": game.attaches,
                                    "bots": {n: {"pos": [round(c, 2) for c in m["pos"]], "age": round(m["age"], 2)} for n, m in meta.items()},
                                    "alive": host.alive() and shadow.alive() and orbit.alive()}) + "\n")
                f.flush()
                if mine:
                    walk.write(json.dumps([round(now, 3)] + [round(c, 3) for c in mine]) + "\n")
                time.sleep(0.5)

    threading.Thread(target=log, daemon=True).start()
    print("session running; double-click the minimap to stop", flush=True)
    try:
        Minimap(host).run()
    finally:
        stop.set()
        for p in (host, shadow, orbit):
            p.stop()
        print("session ended", flush=True)


if __name__ == "__main__":
    main()
