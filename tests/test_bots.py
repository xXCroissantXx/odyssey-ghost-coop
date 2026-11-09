"""Bot tests: no game needed.

1. memscan finds a float triple that a dummy process moves up and down (the same
   flow a player follows in-game).
2. a host and two fake players see each other over UDP on localhost.
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop.memscan import Process, Scanner  # noqa: E402
from odyssey_coop.net import Peer  # noqa: E402
from odyssey_coop.sources import FakeSource  # noqa: E402

DUMMY = r"""
import ctypes, sys
pos = (ctypes.c_float * 3)(123.5, -456.25, 20.37)
print(ctypes.addressof(pos), flush=True)
for line in sys.stdin:
    c = line.strip()
    if c == "up": pos[2] += 3.13
    elif c == "down": pos[2] -= 2.21
    elif c == "walk": pos[0] += 4.0; pos[1] += 1.5
    print("ok", flush=True)
"""


class MemscanBot(unittest.TestCase):
    def test_finds_position(self):
        p = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-I", "-c", DUMMY], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            true_addr = int(p.stdout.readline())

            def act(c):
                p.stdin.write(c + "\n"); p.stdin.flush(); p.stdout.readline()

            proc = Process(p.pid)
            sc = Scanner(proc, Path(tempfile.mkdtemp()) / "snap")
            sc.snapshot()
            for step, mode in [("up", "up"), ("", "same"), ("down", "down"), ("", "same"), ("up", "up"), ("", "same")]:
                if step:
                    act(step)
                sc.filter(mode, lo=0.5, hi=60.0)
            z_addr = true_addr + 8
            self.assertIn(z_addr, sc.addresses(), f"{sc.count()} candidates, true z address missing")
            before = proc.read_floats(z_addr - 8, 3)
            act("walk")
            after = proc.read_floats(z_addr - 8, 3)
            self.assertEqual([round(after[0] - before[0], 2), round(after[1] - before[1], 2)], [4.0, 1.5])
            print(f"\n  memscan: {sc.count()} candidate(s) left, true address among them")
            proc.close()
        finally:
            p.kill()


class NetBots(unittest.TestCase):
    def test_three_players_see_each_other(self):
        port = 27115
        host = Peer("host", FakeSource(0), port=port)
        a = Peer("bot-a", FakeSource(1), host="127.0.0.1", port=port)
        b = Peer("bot-b", FakeSource(2), host="127.0.0.1", port=port)
        for x in (host, a, b):
            x.start()
        time.sleep(1.5)
        try:
            self.assertEqual(set(host.snapshot()), {"bot-a", "bot-b"})
            self.assertEqual(set(a.snapshot()), {"host", "bot-b"})
            self.assertEqual(set(b.snapshot()), {"host", "bot-a"})
            # positions arriving at a client track the sender's live position (within one tick of movement)
            live = b.source.position()
            seen = a.snapshot()["bot-b"]
            self.assertLess(abs(live[0] - seen[0]) + abs(live[1] - seen[1]), 2.0)
            # a silent player drops out after the timeout
            b.stop()
            import odyssey_coop.net as net
            old = net.TIMEOUT
            net.TIMEOUT = 0.5
            time.sleep(1.0)
            self.assertNotIn("bot-b", a.snapshot())
            net.TIMEOUT = old
        finally:
            for x in (host, a, b):
                x.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
