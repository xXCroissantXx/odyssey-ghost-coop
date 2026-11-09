"""Where a player's position comes from: the running game, or a fake bot player for tests."""
import json
import math
import time
from collections import Counter
from pathlib import Path

from .memscan import Process, find_pid

GAME_EXE = "ACOdyssey.exe"


class GameSource:
    """Reads (x, y, z) from the running game. The game keeps many copies of the
    player's position, and some get freed or reused as you move, so this reads
    several saved copies and returns the value most of them agree on.
    Axes (checked in-game): x east, y north, z up."""

    MAX_COPIES = 32

    MAX_PATHS = 48

    def __init__(self, cfg_path):
        cfg_path = Path(cfg_path)
        pid = find_pid(GAME_EXE)
        if pid is None:
            raise SystemExit(f"{GAME_EXE} is not running")
        self.proc = Process(pid)
        self.paths, self.addrs = [], []
        paths_file = cfg_path.parent / "paths.json"
        if paths_file.exists():
            # pointer paths survive restarts: resolve them on every read
            from . import pointers
            self._pointers = pointers
            self.mod_base = pointers.main_module(self.proc)[0]
            for p in json.loads(paths_file.read_text())["paths"][: self.MAX_PATHS]:
                self.paths.append({"module_offset": int(p["module_offset"], 16), "offsets": [int(o, 16) for o in p["offsets"]]})
        if not self.paths:
            cfg = json.loads(cfg_path.read_text())
            if cfg.get("pid") != pid:
                print("warning: the game restarted since the scan; the addresses are probably stale, rerun `scan`")
            addrs = cfg.get("addresses") or [cfg["address"]]
            self.addrs = [int(a, 16) for a in addrs[: self.MAX_COPIES]]

    def _addresses(self):
        if not self.paths:
            return self.addrs
        out = (self._pointers.resolve(self.proc, self.mod_base, p) for p in self.paths)
        return [a for a in out if a is not None]

    @staticmethod
    def _valid(v):
        return v is not None and all(math.isfinite(c) and abs(c) < 1e5 for c in v) and any(c != 0 for c in v)

    def position(self):
        counts, sample = Counter(), {}
        addrs = self._addresses()
        for a in addrs:
            v = self.proc.read_floats(a, 3)
            if self._valid(v):
                key = tuple(round(c, 1) for c in v)
                counts[key] += 1
                sample.setdefault(key, v)
        if not counts:
            return None
        best, n = counts.most_common(1)[0]
        return sample[best] if n >= 2 or len(addrs) == 1 else None


class FakeSource:
    """A bot that walks in a circle, so networking and the overlay can be tested without the game."""

    def __init__(self, seed=0, radius=40.0, center=(0.0, 0.0, 10.0), speed=0.3):
        self.seed, self.r, self.c, self.speed = seed, radius, center, speed
        self.t0 = time.time()

    def position(self):
        a = (time.time() - self.t0) * self.speed + self.seed * 2.1
        return [self.c[0] + self.r * math.cos(a), self.c[1] + self.r * math.sin(a), self.c[2]]
