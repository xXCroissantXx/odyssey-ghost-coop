"""Where a player's position comes from: the running game, or a bot for tests.

Bots:
  CircleBot  - walks a circle around a fixed point (no game needed)
  OrbitBot   - walks a circle around YOUR live position (a friend running around you)
  FollowBot  - replays your own live path a few seconds late, with a side offset
               (a friend trailing you through the same terrain)
  ReplayBot  - plays back a recording made with `record` (a real walk, looped)
"""
import collections
import ctypes
import ctypes.wintypes as wt
import json
import math
import struct
import time
from collections import Counter
from pathlib import Path

from .memscan import Process, find_pid, k32

GAME_EXE = "ACOdyssey.exe"
REATTACH_AFTER = 2.0  # seconds without a position before checking for a restarted game

# Places the game reports while you are NOT in the world (seen on build 17083392):
# every loading screen parks the player at one fixed point, and the main menu puts its
# background character near the world origin at exactly sea level. (centre, xy radius, z, z tolerance)
LIMBO = [((-94.7, 33.8), 2.0, -4.3, 0.2),   # loading screens (fast travel, loading a save)
         ((-47.0, -15.5), 30.0, 0.0, 0.01)]  # main menu background


def in_limbo(pos):
    return any(math.hypot(pos[0] - c[0], pos[1] - c[1]) <= r and abs(pos[2] - z) <= dz for c, r, z, dz in LIMBO)

k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


def exe_path(proc):
    buf = ctypes.create_unicode_buffer(1024)
    n = wt.DWORD(len(buf))
    if not k32.QueryFullProcessImageNameW(proc.h, 0, buf, ctypes.byref(n)):
        return None
    return Path(buf.value)


def build_id(path):
    """A short id for the exact game build: PE timestamp + file size. Two players with
    different ids are on different game versions and pointer paths will not match."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        ts = struct.unpack_from("<I", head, e_lfanew + 8)[0]
        return f"{ts:x}-{Path(path).stat().st_size}"
    except (OSError, struct.error):
        return None


class GameSource:
    """Reads (x, y, z) from the running game. The game keeps many copies of the
    player's position, and some get freed or reused as you move, so this reads
    several copies and returns the value most of them agree on.
    If the game is closed and started again, it reattaches by itself (pointer paths only).
    Axes (checked in-game): x east, y north, z up."""

    MAX_COPIES = 32
    MAX_PATHS = 48
    REVOTE = 1.0  # seconds between full re-checks of every path / copy

    def __init__(self, cfg_path, wait=False):
        self.cfg_path = Path(cfg_path)
        self.paths, self.addrs, self.proc = [], [], None
        paths_file = self.cfg_path.parent / "paths.json"
        if paths_file.exists():
            from . import pointers
            self._pointers = pointers
            for p in json.loads(paths_file.read_text())["paths"][: self.MAX_PATHS]:
                self.paths.append({"module_offset": int(p["module_offset"], 16), "offsets": [int(o, 16) for o in p["offsets"]]})
        self.last_ok = time.time()
        self.cached, self.voted_at = [], 0.0
        self.attaches = 0
        self.state = "no-game"  # "ok" in the world, "loading" on loading screens / menus
        if not self._attach():
            if not wait:
                raise SystemExit(f"{GAME_EXE} is not running")
            print(f"waiting for {GAME_EXE}...")
        if not self.paths:
            cfg = json.loads(self.cfg_path.read_text())
            if self.proc and cfg.get("pid") != self.proc.pid:
                print("warning: the game restarted since the scan; the addresses are probably stale, run `pointers`")
            addrs = cfg.get("addresses") or [cfg["address"]]
            self.addrs = [int(a, 16) for a in addrs[: self.MAX_COPIES]]

    def _attach(self):
        pid = find_pid(GAME_EXE)
        if pid is None:
            return False
        if self.proc is not None and self.proc.pid == pid:
            return True
        if self.proc is not None:
            self.proc.close()
        self.proc = Process(pid)
        self.build = build_id(exe_path(self.proc)) if exe_path(self.proc) else None
        if self.paths:
            self.mod_base = self._pointers.main_module(self.proc)[0]
        self.attaches += 1
        return True

    def _addresses(self):
        if not self.paths:
            return self.addrs
        out = (self._pointers.resolve(self.proc, self.mod_base, p) for p in self.paths)
        return [a for a in out if a is not None]

    @staticmethod
    def _valid(v):
        return v is not None and all(math.isfinite(c) and abs(c) < 1e5 for c in v) and any(c != 0 for c in v)

    def position(self):
        pos = self._read() if self.proc else None
        now = time.time()
        if pos is not None and in_limbo(pos):
            self.last_ok = now  # the game is there, just not in the world
            self.state = "loading"
            return None
        self.state = "ok" if pos is not None else "no-game"
        if pos is not None:
            self.last_ok = now
        elif now - self.last_ok > REATTACH_AFTER and self.paths:
            self.last_ok = now  # check at most every REATTACH_AFTER seconds
            try:
                self._attach()
            except OSError:
                pass  # game still starting up
        return pos

    def _read(self):
        # fast path: re-read the copies that agreed at the last full vote
        if len(self.cached) >= 2 and time.time() - self.voted_at < self.REVOTE:
            vals = [self.proc.read_floats(a, 3) for a in self.cached]
            if all(self._valid(v) for v in vals) and max(math.dist(v, vals[0]) for v in vals) < 0.05:
                return vals[0]
        self.voted_at = time.time()
        counts, sample, agree = Counter(), {}, {}
        try:
            addrs = self._addresses()
        except OSError:
            return None
        for a in addrs:
            v = self.proc.read_floats(a, 3)
            if self._valid(v):
                key = tuple(round(c, 1) for c in v)
                counts[key] += 1
                sample.setdefault(key, v)
                agree.setdefault(key, []).append(a)
        if not counts:
            self.cached = []
            return None
        best, n = counts.most_common(1)[0]
        self.cached = agree[best][:3]
        return sample[best] if n >= 2 or len(addrs) == 1 else None


class CircleBot:
    """Walks a circle around a fixed point. Needs no game."""
    build = "bot"

    def __init__(self, seed=0, radius=40.0, center=(0.0, 0.0, 10.0), speed=0.3):
        self.seed, self.r, self.c, self.speed = seed, radius, center, speed
        self.t0 = time.time()

    def position(self):
        a = (time.time() - self.t0) * self.speed + self.seed * 2.1
        return [self.c[0] + self.r * math.cos(a), self.c[1] + self.r * math.sin(a), self.c[2]]


FakeSource = CircleBot  # older name


class OrbitBot:
    """Walks a circle of `radius` metres around another source's live position."""

    def __init__(self, around, radius=25.0, speed=0.25):
        self.around, self.r, self.speed = around, radius, speed
        self.build = getattr(around, "build", None)
        self.t0 = time.time()

    def position(self):
        c = self.around.position()
        if c is None:
            return None
        a = (time.time() - self.t0) * self.speed
        return [c[0] + self.r * math.cos(a), c[1] + self.r * math.sin(a), c[2]]


class FollowBot:
    """Replays another source's path `delay` seconds late, `side` metres to its right."""

    def __init__(self, leader, delay=3.0, side=3.0):
        self.leader, self.delay, self.side = leader, delay, side
        self.build = getattr(leader, "build", None)
        self.trail = collections.deque()  # (t, pos)
        self.heading = (0.0, 1.0)

    def position(self):
        now = time.time()
        p = self.leader.position()
        if p is not None:
            self.trail.append((now, list(p)))
        while len(self.trail) > 1 and self.trail[1][0] <= now - self.delay:
            old = self.trail.popleft()
            dx, dy = self.trail[0][1][0] - old[1][0], self.trail[0][1][1] - old[1][1]
            if math.hypot(dx, dy) > 0.05:
                n = math.hypot(dx, dy)
                self.heading = (dx / n, dy / n)
        if not self.trail or self.trail[0][0] > now - self.delay:
            return None
        x, y, z = self.trail[0][1]
        hx, hy = self.heading
        return [x + hy * self.side, y - hx * self.side, z]  # right of the walking direction


class ReplayBot:
    """Plays back a JSON Lines recording of [t, x, y, z] rows in a loop."""
    build = None

    def __init__(self, path, offset=(0.0, 0.0, 0.0)):
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        if len(rows) < 2:
            raise SystemExit(f"{path}: recording needs at least 2 samples")
        t0 = rows[0][0]
        self.rows = [(r[0] - t0, r[1:4]) for r in rows]
        self.length = self.rows[-1][0]
        self.offset = offset
        self.t0 = time.time()
        self.i = 0

    def position(self):
        t = (time.time() - self.t0) % self.length if self.length > 0 else 0
        if t < self.rows[self.i][0]:
            self.i = 0
        while self.i + 1 < len(self.rows) and self.rows[self.i + 1][0] <= t:
            self.i += 1
        (ta, a), (tb, b) = self.rows[self.i], self.rows[min(self.i + 1, len(self.rows) - 1)]
        f = 0.0 if tb <= ta else (t - ta) / (tb - ta)
        return [a[k] + (b[k] - a[k]) * f + self.offset[k] for k in range(3)]
