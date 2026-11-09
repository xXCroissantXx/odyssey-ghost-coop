"""Find the game camera (read-only): its position and orientation.

Idea: stand still and swing the camera around. The camera then orbits the player at a
roughly constant distance while the player does not move. Any float triple in memory
that behaves like that is a camera position candidate. Next to a camera's position,
engines store its orientation as three unit vectors at right angles; one of them points
from the camera at the player (third-person cameras look at the character).
"""
import json
import math
import time
from collections import defaultdict

import numpy as np

from .memscan import Process

CHUNK = 64 << 20
PAGE = 4096


def triples_near(proc, regions, center, radius=15.0, min_dist=0.5):
    """Addresses of float triples within `radius` m of `center` (but not on it)."""
    px, py, pz = (np.float32(c) for c in center)
    out = []
    for base, size in regions:
        for start in range(0, size, CHUNK):
            n = min(CHUNK, size - start)
            buf = proc.read_array(base + start, n)
            if buf is None:
                continue
            f = buf.view(np.float32)
            with np.errstate(invalid="ignore"):
                i = np.nonzero(np.abs(f[:-2] - px) < radius)[0]
                if not i.size:
                    continue
                ok = (np.abs(f[i + 1] - py) < radius) & (np.abs(f[i + 2] - pz) < radius)
                i = i[ok]
                d = np.sqrt((f[i] - px) ** 2 + (f[i + 1] - py) ** 2 + (f[i + 2] - pz) ** 2)
                i = i[(d >= min_dist) & (d <= radius)]
            out.append((base + start + 4 * i).astype(np.int64))
    return np.concatenate(out) if out else np.array([], dtype=np.int64)


class PageReader:
    """Reads many scattered addresses quickly by fetching each 4 KiB page once."""

    def __init__(self, proc, addrs, span=12):
        self.proc = proc
        self.addrs = np.asarray(addrs, dtype=np.int64)
        self.span = span
        pages = defaultdict(list)
        for k, a in enumerate(self.addrs):
            pages[int(a) // PAGE].append(k)
            if (int(a) + span - 1) // PAGE != int(a) // PAGE:  # straddles a page
                pages[(int(a) + span - 1) // PAGE].append(k)
        self.pages = sorted(pages)

    def read(self):
        """bytes of `span` length at every address (None if unreadable)."""
        cache = {}
        for pg in self.pages:
            b = self.proc.read_array(pg * PAGE, PAGE)
            cache[pg] = None if b is None else b.tobytes()
        out = []
        for a in self.addrs:
            a = int(a)
            p0, off = a // PAGE, a % PAGE
            if cache.get(p0) is None:
                out.append(None)
                continue
            data = cache[p0][off:off + self.span]
            if len(data) < self.span:
                nxt = cache.get(p0 + 1)
                if nxt is None:
                    out.append(None)
                    continue
                data += nxt[: self.span - len(data)]
            out.append(data)
        return out


def find_camera_positions(samples_player, samples_cand, addrs):
    """Rank candidates that orbit a still player. samples_cand: [T][N] of (x,y,z) or None."""
    P = np.array(samples_player, dtype=np.float64)  # T x 3
    player_moved = float(np.linalg.norm(P.max(0) - P.min(0)))
    results = []
    T = len(samples_cand)
    for k, a in enumerate(addrs):
        pts = [samples_cand[t][k] for t in range(T)]
        if any(p is None for p in pts):
            continue
        C = np.array(pts, dtype=np.float64)
        if not np.all(np.isfinite(C)):
            continue
        d = np.linalg.norm(C - P, axis=1)
        motion = float(np.linalg.norm(C.max(0) - C.min(0)))
        if motion < 1.0 or d.mean() < 1.0 or d.mean() > 12.0 or d.std() > 0.6:
            continue
        results.append({"addr": int(a), "motion": round(motion, 2), "dist": round(float(d.mean()), 2),
                        "dist_std": round(float(d.std()), 3)})
    results.sort(key=lambda r: (-r["motion"], r["dist_std"]))
    return player_moved, results


def run_scan(game, regions, go_flag, log, rotate_seconds=20.0, window_seconds=15.0, top=30):
    """Full camera scan. game: GameSource. Waits for `go_flag` (a Path) to exist before
    sampling, so the player can start swinging the camera when told."""
    here = game.position()
    if here is None:
        raise SystemExit("no player position; load into the world first")
    t = time.time()
    addrs = triples_near(game.proc, regions, here)
    log(f"phase 1: {len(addrs):,} float triples within 15 m of the player ({time.time() - t:.0f}s). waiting for go")
    while not go_flag.exists():
        time.sleep(0.2)
    reader = PageReader(game.proc, addrs)
    P, C = [], []
    t_end = time.time() + rotate_seconds
    while time.time() < t_end:
        p = game.position()
        raw = reader.read()
        if p is not None:
            P.append(p)
            C.append([None if r is None else np.frombuffer(r, dtype=np.float32).tolist() for r in raw])
        time.sleep(0.1)
    moved, cams = find_camera_positions(P, C, addrs)
    log(f"phase 2: {len(P)} samples, player moved {moved:.2f} m, {len(cams)} orbiting candidates")
    if not cams:
        return {"error": "no candidate orbited the player", "player_moved": moved}
    cams = cams[:top]
    WIN = 256  # bytes either side of the camera position
    wreader = PageReader(game.proc, [c["addr"] - WIN for c in cams], span=2 * WIN + 12)
    W, P2 = [], []
    t_end = time.time() + window_seconds
    while time.time() < t_end:
        p = game.position()
        raw = wreader.read()
        if p is not None and all(r is not None for r in raw):
            P2.append(p)
            W.append([np.frombuffer(r, dtype=np.float32).copy() for r in raw])
        time.sleep(0.1)
    for k, c in enumerate(cams):
        wins = [W[t][k] for t in range(len(W))]
        cam_pos = [w[WIN // 4: WIN // 4 + 3].tolist() for w in wins]
        c["orientation"] = find_orientation(wins, None, P2, cam_pos, -WIN)
    with_orient = [c for c in cams if c["orientation"]]
    log(f"phase 3: {len(W)} samples; {len(with_orient)} of the top {len(cams)} have an orientation block next to them")
    return {"player_moved": moved, "candidates": cams}


# --- reading the camera block and projecting --------------------------------------
# Layout found on build 17083392 (offsets from the camera-position address):
#   +0   camera position (x, y, z)
#   +32  view matrix, 4x4 row-major, row-vector convention (columns = right, up, -forward)
#   +96  projection, 4x4: [0][0] = fx, [1][1] = fy, [2][3] = -1 (reverse-Z, near 0.1)
BLOCK = 160


def read_block(proc, addr):
    """{"pos", "V", "P"} if addr looks like a live camera block, else None."""
    b = proc.read_array(addr, BLOCK)
    if b is None:
        return None
    f = b.view(np.float32).astype(np.float64)
    pos, V, P = f[0:3], f[8:24].reshape(4, 4), f[24:40].reshape(4, 4)
    if not (np.all(np.isfinite(f[:40])) and P[0, 0] > 0.1 and P[1, 1] > 0.1 and abs(P[2, 3] + 1) < 1e-3
            and abs(V[3, 3] - 1) < 1e-3):
        return None
    R = V[:3, :3]
    if np.abs(R @ R.T - np.eye(3)).max() > 0.01:
        return None
    # the view matrix must agree with the stored camera position
    if np.abs(-(pos @ R) - V[3, :3]).max() > 1.0:
        return None
    return {"pos": pos, "V": V, "P": P}


def project(cam, world, width, height):
    """Screen pixel (x, y) and depth in metres of a world point, or None if behind the camera."""
    v = np.append(np.asarray(world, dtype=np.float64), 1.0) @ cam["V"]
    depth = -v[2]
    if depth < 0.2:
        return None
    ndc_x = cam["P"][0, 0] * v[0] / depth
    ndc_y = cam["P"][1, 1] * v[1] / depth
    return ((ndc_x + 1) / 2 * width, (1 - ndc_y) / 2 * height, depth)


def fov_degrees(cam):
    return math.degrees(2 * math.atan(1 / cam["P"][1, 1]))


class CameraSource:
    """Live camera from pointer paths in local/camera_paths.json (majority of valid blocks)."""

    MAX_PATHS = 24
    REVOTE = 1.0  # seconds between full re-checks of every path

    def __init__(self, game, paths_file):
        """game: a GameSource (its proc and mod_base follow game restarts)."""
        from . import pointers
        self._pointers = pointers
        self.game = game
        self.cached, self.voted_at = None, 0.0
        data = json.loads(paths_file.read_text())
        self.paths = [{"module_offset": int(p["module_offset"], 16), "offsets": [int(o, 16) for o in p["offsets"]]}
                      for p in data["paths"][: self.MAX_PATHS]]

    def read(self):
        proc, base = self.game.proc, getattr(self.game, "mod_base", None)
        if proc is None or base is None:
            return None
        if self.cached and time.time() - self.voted_at < self.REVOTE:
            cam = read_block(proc, self.cached)  # fast path: one read per frame
            if cam is not None:
                return cam
        self.voted_at = time.time()
        votes, sample, addr_of = {}, {}, {}
        for p in self.paths:
            a = self._pointers.resolve(proc, base, p)
            cam = read_block(proc, a) if a else None
            if cam is not None:
                k = tuple(np.round(cam["pos"], 1))
                votes[k] = votes.get(k, 0) + 1
                sample.setdefault(k, cam)
                addr_of.setdefault(k, a)
        if not votes:
            self.cached = None
            return None
        best = max(votes, key=votes.get)
        self.cached = addr_of[best]
        return sample[best]


def find_orientation(windows, cam_offset, player_samples, cam_samples, window_base_off):
    """Search a byte window (as float32 arrays per sample) for a 3x3 orthonormal block whose
    rows or columns include a vector pointing from camera to player.
    windows: [T] float32 arrays of the same length; window_base_off: byte offset of
    window[0] relative to the camera position address."""
    T = len(windows)
    n = len(windows[0])
    to_player = []
    for t in range(T):
        v = np.array(player_samples[t]) - np.array(cam_samples[t])
        to_player.append(v / (np.linalg.norm(v) or 1))
    found = []
    for start in range(0, n - 11):
        for stride in (3, 4):
            if start + 2 * stride + 3 > n:
                continue
            for layout in ("rows", "cols"):
                ok_all, best = True, None
                for t in range(T):
                    w = windows[t].astype(np.float64)
                    if layout == "rows":
                        M = np.array([w[start + r * stride: start + r * stride + 3] for r in range(3)])
                    else:
                        M = np.array([[w[start + r * stride + c] for r in range(3)] for c in range(3)])
                    if not np.all(np.isfinite(M)) or np.abs(M @ M.T - np.eye(3)).max() > 0.02:
                        ok_all = False
                        break
                    dots = M @ to_player[t]
                    if best is None:
                        j = int(np.argmax(np.abs(dots)))
                        best = (j, 1 if dots[j] > 0 else -1)
                    if best[1] * dots[best[0]] < 0.85:
                        ok_all = False
                        break
                if ok_all and best:
                    found.append({"offset": window_base_off + 4 * start, "stride": stride, "layout": layout,
                                  "forward_index": best[0], "forward_sign": best[1]})
    return found
