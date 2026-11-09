"""Bot test for the pointer scan: a dummy process builds a 3-level pointer chain from a
static (module-level) variable to a position, and the scan must find and resolve it."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey_coop import pointers  # noqa: E402
from odyssey_coop.memscan import Process  # noqa: E402

# The dummy's own exe image can't hold our static pointer (it's python.exe), so the test
# treats the dummy's heap block holding the root pointer as the "module".
DUMMY = r"""
import ctypes, sys
class Pos(ctypes.Structure): _fields_ = [("pad", ctypes.c_byte * 0x50), ("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]
class Mid(ctypes.Structure): _fields_ = [("pad", ctypes.c_byte * 0x28), ("pos", ctypes.POINTER(Pos))]
class Root(ctypes.Structure): _fields_ = [("pad", ctypes.c_byte * 0x18), ("mid", ctypes.POINTER(Mid))]
pos = Pos(); pos.x, pos.y, pos.z = 101.25, -202.5, 33.75
mid = Mid(); mid.pos = ctypes.pointer(pos)
root = Root(); root.mid = ctypes.pointer(mid)
static = ctypes.c_void_p(ctypes.addressof(root))
print(ctypes.addressof(pos) + 0x50, ctypes.addressof(static), flush=True)
sys.stdin.readline()
"""


class PointerBot(unittest.TestCase):
    def test_finds_chain(self):
        p = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-I", "-c", DUMMY],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            pos_addr, static_addr = map(int, p.stdout.readline().split())
            proc = Process(p.pid)
            fake_base, fake_size = static_addr & ~0xFFF, 0x1000
            orig = pointers.main_module
            pointers.main_module = lambda _proc: (fake_base, fake_size)
            try:
                base, paths = pointers.find_paths(proc, [pos_addr], depth=4, log=lambda *_: None)
            finally:
                pointers.main_module = orig
            want = {"module_offset": static_addr - fake_base, "offsets": [0x18, 0x28, 0x50]}
            self.assertIn(want, paths)
            self.assertEqual(pointers.resolve(proc, base, want), pos_addr)
            self.assertEqual(proc.read_floats(pos_addr, 3), [101.25, -202.5, 33.75])
            proc.close()
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
