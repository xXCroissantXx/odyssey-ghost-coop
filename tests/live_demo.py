"""Live check with the real game: you host, and a bot friend circles 30 m around where
you are standing. The minimap appears top-left; double-click it to close."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop.net import Peer  # noqa: E402
from odyssey_coop.overlay import Minimap  # noqa: E402
from odyssey_coop.sources import FakeSource, GameSource  # noqa: E402

game = GameSource(ROOT / "local" / "position.json")
start = game.position()
if start is None:
    raise SystemExit("no position from the game; rerun the scan")
host = Peer("you", game, port=27015)
bot = Peer("bot-friend", FakeSource(radius=30.0, center=tuple(start), speed=0.15), host="127.0.0.1", port=27015)
host.start()
bot.start()
print("demo running at", [round(c, 1) for c in start], flush=True)
try:
    Minimap(host).run()
finally:
    host.stop()
    bot.stop()
