"""Bot tests for the in-view figure: animation states from movement, the skeleton's
shape and facing, projection through a camera built like the game's, and edge markers."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop.camera import project, read_block  # noqa: E402
from odyssey_coop.figure import HEAD_H, Animator  # noqa: E402
from odyssey_coop.world_overlay import edge_marker, figure_shapes  # noqa: E402


def make_cam(pos, yaw_deg=0.0, fy=2.1371, aspect=16 / 9):
    """A camera like the game's: looking along `yaw` (0 = north), level, row-vector view matrix."""
    yaw = math.radians(yaw_deg)
    fwd = np.array([math.sin(yaw), math.cos(yaw), 0.0])
    right = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
    up = np.array([0.0, 0.0, 1.0])
    R = np.stack([right, up, -fwd], axis=1)  # columns: right, up, -forward
    V = np.eye(4)
    V[:3, :3] = R
    V[3, :3] = -np.asarray(pos) @ R
    P = np.zeros((4, 4))
    P[0, 0], P[1, 1], P[2, 3], P[3, 2] = fy / aspect, fy, -1.0, -0.1
    return {"pos": np.asarray(pos, dtype=float), "V": V, "P": P}


def drive(a, path, dt=0.05):
    t = 0.0
    for p in path:
        a.update(p, t)
        t += dt
    return a


class Animation(unittest.TestCase):
    def test_states_from_movement(self):
        cases = {
            "idle": [[0, 0, 0]] * 40,
            "walk": [[0, 1.5 * i * 0.05, 0] for i in range(40)],
            "run": [[0, 6.0 * i * 0.05, 0] for i in range(40)],
            "ride": [[0, 14.0 * i * 0.05, 0] for i in range(40)],
            "climb": [[0, 0, 1.2 * i * 0.05] for i in range(40)],
            "fall": [[0, 0, -8.0 * i * 0.05] for i in range(40)],
        }
        for want, path in cases.items():
            self.assertEqual(drive(Animator(), path).state, want, want)

    def test_fast_travel_does_not_animate(self):
        a = drive(Animator(), [[0, 0, 0]] * 10 + [[5000, 5000, 0]] * 10)
        self.assertEqual(a.state, "idle")

    def test_facing_follows_movement(self):
        a = drive(Animator(), [[1.5 * i * 0.05, 0, 0] for i in range(60)])  # walking east
        self.assertAlmostEqual(a.facing, math.pi / 2, delta=0.1)
        j = a.joints()
        self.assertGreater(j["head"][0] - a.pos[0], -0.05)  # head over the body
        self.assertAlmostEqual(j["l_hip"][1] - j["r_hip"][1], 0.22, delta=0.02)  # left hip is north when facing east

    def test_skeleton_proportions(self):
        a = drive(Animator(), [[10, 20, 5]] * 5)
        j = a.joints()
        self.assertAlmostEqual(j["head"][2] - 5, HEAD_H, delta=0.05)
        for foot in ("l_foot", "r_foot"):
            self.assertAlmostEqual(j[foot][2], 5.0, delta=0.06)
        for k in j.values():
            self.assertLess(math.dist(k[:2], (10, 20)), 1.0)

    def test_walk_cycle_moves_legs(self):
        a = Animator()
        feet = []
        for i in range(60):
            a.update([0, 1.5 * i * 0.05, 0], i * 0.05)
            j = a.joints()
            feet.append(j["l_foot"][1] - j["r_foot"][1])
        self.assertGreater(max(feet) - min(feet), 0.4, "feet must swing past each other")


class Projection(unittest.TestCase):
    def test_project_like_game(self):
        cam = make_cam([0, -5, 1.7])
        x, y, d = project(cam, [0, 0, 1.7], 1920, 1080)
        self.assertAlmostEqual(x, 960, delta=0.5)
        self.assertAlmostEqual(y, 540, delta=0.5)
        self.assertAlmostEqual(d, 5.0, delta=1e-6)
        x, _, _ = project(cam, [1, 0, 1.7], 1920, 1080)
        self.assertGreater(x, 960, "east is right when looking north")
        self.assertIsNone(project(cam, [0, -10, 1.7], 1920, 1080), "behind the camera")

    def test_read_block_accepts_game_layout(self):
        cam = make_cam([-3136.0, 4684.7, 401.2], yaw_deg=32)
        block = np.zeros(40, dtype=np.float32)
        block[0:3] = cam["pos"]
        block[8:24] = cam["V"].reshape(-1)
        block[24:40] = cam["P"].reshape(-1)

        class FakeProc:
            def read_array(self, addr, n):
                return np.frombuffer(block.tobytes() + b"\0" * (n - block.nbytes), dtype=np.uint8)

        got = read_block(FakeProc(), 0)
        self.assertIsNotNone(got)
        block[9] = 5.0  # break orthonormality
        self.assertIsNone(read_block(FakeProc(), 0))

    def test_figure_shapes_scale_with_distance(self):
        cam = make_cam([0, -5, 1.7])
        near = figure_shapes(cam, drive(Animator(), [[0, 0, 0]] * 3).joints(), 1920, 1080)
        far = figure_shapes(cam, drive(Animator(), [[0, 40, 0]] * 3).joints(), 1920, 1080)
        self.assertGreater(near["head"][2], far["head"][2] * 4)
        self.assertEqual(len(near["lines"]), 12)
        behind = figure_shapes(cam, drive(Animator(), [[0, -20, 0]] * 3).joints(), 1920, 1080)
        self.assertIsNone(behind)

    def test_edge_marker_stays_on_screen(self):
        for sx, sy in [(5000, 540), (-300, -900), (960, 3000), (961, 541)]:
            x, y = edge_marker(sx, sy, 1920, 1080)
            self.assertTrue(0 <= x <= 1920 and 0 <= y <= 1080, (sx, sy, x, y))


if __name__ == "__main__":
    unittest.main(verbosity=2)
