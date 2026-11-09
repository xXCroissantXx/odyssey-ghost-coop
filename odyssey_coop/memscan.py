"""Read-only access to another process's memory on Windows.

Only PROCESS_VM_READ / PROCESS_QUERY_INFORMATION are requested: nothing is
written, injected or patched, and the game's exe / DRM is never touched.
"""
import ctypes
import ctypes.wintypes as wt
import csv
import io
import shutil
import subprocess
from pathlib import Path

import numpy as np

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
WRITABLE = {0x04, 0x08, 0x40, 0x80}  # RW, WRITECOPY, EXECUTE_RW, EXECUTE_WRITECOPY
USER_SPACE_END = 0x7FFF_FFFF_FFFF


class MBI(ctypes.Structure):  # MEMORY_BASIC_INFORMATION (64-bit)
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD),
                ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD)]


def find_pid(image_name):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    for row in csv.reader(io.StringIO(out)):
        if len(row) > 1 and row[0].lower() == image_name.lower():
            return int(row[1])
    return None


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess({pid}) failed: error {ctypes.get_last_error()}")

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def regions(self, max_size=1 << 30):
        """Committed, private, writable regions: where heap objects like the player live."""
        addr, mbi = 0, MBI()
        while addr < USER_SPACE_END:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress, mbi.RegionSize
            if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and mbi.Protect in WRITABLE
                    and not mbi.Protect & PAGE_GUARD and size <= max_size):
                yield base, size
            addr = base + size

    def read_array(self, addr, size):
        buf = np.empty(size, dtype=np.uint8)
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf.ctypes.data_as(ctypes.c_void_p),
                                     size, ctypes.byref(n)) or n.value != size:
            return None
        return buf

    def read_floats(self, addr, count):
        a = self.read_array(addr, 4 * count)
        return None if a is None else a.view(np.float32).tolist()


class Scanner:
    """Narrow down float addresses by how they change between snapshots
    (the same idea as a Cheat Engine 'unknown initial value' scan)."""

    LIMIT = 1e5  # world coordinates are far below this

    def __init__(self, proc, workdir):
        self.proc = proc
        self.workdir = Path(workdir)
        self.regions = []
        self.cands = None  # None = every float in the first snapshot

    def snapshot(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True)
        self.regions, total = [], 0
        for base, size in self.proc.regions():
            data = self.proc.read_array(base, size)
            if data is None:
                continue
            data.view(np.float32).tofile(self.workdir / f"{base:x}.f32")
            self.regions.append((base, size))
            total += size
        self.cands = None
        return total

    def filter(self, mode, lo=0.05, hi=1000.0):
        """mode: 'up', 'down', 'changed' (by lo..hi) or 'same'."""
        new = {}
        for base, size in self.regions:
            if self.cands is not None and base not in self.cands:
                continue
            cur = self.proc.read_array(base, size)
            if cur is None:
                continue
            cur = cur.view(np.float32)
            if self.cands is None:
                idx, old = None, np.fromfile(self.workdir / f"{base:x}.f32", dtype=np.float32)
                vals = cur
            else:
                idx, old = self.cands[base]
                vals = cur[idx]
            with np.errstate(invalid="ignore", over="ignore"):
                d = vals - old
                ok = np.isfinite(vals) & np.isfinite(old) & (np.abs(vals) < self.LIMIT)
                # flags and counters flip between whole numbers; a character standing on
                # terrain almost never sits at an exact integer height
                ok &= vals != np.round(vals)
                if mode == "up":
                    ok &= (d >= lo) & (d <= hi)
                elif mode == "down":
                    ok &= (-d >= lo) & (-d <= hi)
                elif mode == "changed":
                    ok &= (np.abs(d) >= lo) & (np.abs(d) <= hi)
                elif mode == "same":
                    ok &= d == 0
                else:
                    raise ValueError(mode)
            sel = np.nonzero(ok)[0]
            if sel.size:
                keep = sel.astype(np.int64) if idx is None else idx[sel]
                new[base] = (keep, vals[sel].copy())
        if self.cands is None:
            shutil.rmtree(self.workdir, ignore_errors=True)
        self.cands = new
        return self.count()

    def count(self):
        if self.cands is None:
            return sum(s for _, s in self.regions) // 4
        return int(sum(len(i) for i, _ in self.cands.values()))

    def walk_filter(self, before_xy, after_xy, lo=0.5, hi=200.0):
        """Keep candidates whose two preceding floats (x, y) moved by lo..hi in total.
        before/after: {base: float32[N, 2]} from read_xy()."""
        new = {}
        for base, (idx, vals) in self.cands.items():
            if base not in before_xy or base not in after_xy:
                continue
            with np.errstate(invalid="ignore", over="ignore"):
                moved = np.abs(after_xy[base] - before_xy[base]).sum(axis=1)
                ok = np.isfinite(moved) & (moved >= lo) & (moved <= hi)
            sel = np.nonzero(ok)[0]
            if sel.size:
                new[base] = (idx[sel], vals[sel])
        self.cands = new
        return self.count()

    def read_xy(self):
        """For every candidate z, the floats at z-8 and z-4."""
        out = {}
        for base, size in self.regions:
            if base not in self.cands:
                continue
            cur = self.proc.read_array(base, size)
            if cur is None:
                continue
            f = cur.view(np.float32)
            idx = self.cands[base][0]
            ok = idx >= 2
            xy = np.full((len(idx), 2), np.nan, dtype=np.float32)
            xy[ok, 0] = f[idx[ok] - 2]
            xy[ok, 1] = f[idx[ok] - 1]
            out[base] = xy
        return out

    def dump(self, path):
        addrs = np.array(self.addresses(), dtype=np.int64)
        np.save(path, addrs)
        return len(addrs)

    def addresses(self, limit=None):
        out = []
        for base, (idx, _) in sorted(self.cands.items()):
            out.extend(int(base + 4 * i) for i in idx)
            if limit and len(out) >= limit:
                return out[:limit]
        return out
