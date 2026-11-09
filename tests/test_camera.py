"""Bot test for the camera scan: a dummy process holds a still 'player' and a camera that
orbits it, stored engine-style as a 3x4 row block (right, forward, up rows padded to 4
floats) followed by its position. The scan must find the position and the forward row."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop.camera import run_scan  # noqa: E402
from odyssey_coop.memscan import Process  # noqa: E402

DUMMY = r"""
import ctypes, math, sys, threading, time
class Cam(ctypes.Structure):
    _fields_ = [("pad", ctypes.c_float * 7), ("rot", ctypes.c_float * 12), ("pos", ctypes.c_float * 3)]
player = (ctypes.c_float * 3)(-3400.25, 3950.5, 300.75)
cam = Cam()
def spin():
    t0 = time.time()
    while True:
        a = (time.time() - t0) * 0.8
        px, py, pz = player
        cx, cy, cz = px - 4.0 * math.cos(a), py - 4.0 * math.sin(a), pz + 1.6
        f = [px - cx, py - cy, pz + 0.0 - cz]; n = math.sqrt(sum(v * v for v in f)); f = [v / n for v in f]
        r = [f[1], -f[0], 0.0]; rn = math.hypot(r[0], r[1]); r = [v / rn for v in r]
        u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
        for i, row in enumerate((r, f, u)):
            for j in range(3):
                cam.rot[i * 4 + j] = row[j]
            cam.rot[i * 4 + 3] = 0.0
        cam.pos[0], cam.pos[1], cam.pos[2] = cx, cy, cz
        time.sleep(0.01)
threading.Thread(target=spin, daemon=True).start()
print(ctypes.addressof(player), ctypes.addressof(cam) + Cam.pos.offset, ctypes.addressof(cam) + Cam.rot.offset, flush=True)
sys.stdin.readline()
"""


class FakeGame:
    def __init__(self, proc, addr):
        self.proc, self.addr = proc, addr

    def position(self):
        return self.proc.read_floats(self.addr, 3)


class CameraBot(unittest.TestCase):
    def test_finds_orbiting_camera_and_forward(self):
        p = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-I", "-c", DUMMY],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            player_addr, cam_pos_addr, rot_addr = map(int, p.stdout.readline().split())
            proc = Process(p.pid)
            game = FakeGame(proc, player_addr)
            with tempfile.TemporaryDirectory() as d:
                go = Path(d) / "go"
                go.touch()
                time.sleep(0.2)
                res = run_scan(game, list(proc.regions()), go, lambda m: None, rotate_seconds=3, window_seconds=2)
            cands = {c["addr"]: c for c in res["candidates"]}
            self.assertIn(cam_pos_addr, cands, f"camera not among {len(cands)} candidates")
            c = cands[cam_pos_addr]
            self.assertAlmostEqual(c["dist"], (4.0 ** 2 + 1.6 ** 2) ** 0.5, delta=0.05)
            want = {"offset": rot_addr - cam_pos_addr, "stride": 4, "layout": "rows", "forward_index": 1, "forward_sign": 1}
            self.assertIn(want, c["orientation"], c["orientation"])
            self.assertLess(res["player_moved"], 0.01)
            proc.close()
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
