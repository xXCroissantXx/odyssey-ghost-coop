"""Pointer-path scan (read-only), like a Cheat Engine pointer scan.

Heap addresses change every launch, but a path that starts at a fixed offset inside
the game's own module and follows pointers usually leads to the same object each time:

    a = read_u64(module_base + module_offset)
    for off in offsets[:-1]: a = read_u64(a + off)
    position = a + offsets[-1]

We walk backwards from known position addresses: find every qword that points just
below a target, then every qword that points just below *those* locations, and so on,
until a pointer is found that lives in the exe's static data.
"""
import ctypes
import ctypes.wintypes as wt
import time

import numpy as np

from .memscan import MBI, MEM_COMMIT, PAGE_GUARD, USER_SPACE_END, k32

MEM_IMAGE = 0x1000000
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
WRITABLE = {0x04, 0x08, 0x40, 0x80}
CHUNK = 64 << 20

psapi = ctypes.WinDLL("psapi", use_last_error=True)
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleInformation.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wt.DWORD), ("EntryPoint", ctypes.c_void_p)]


def main_module(proc):
    """(base, size) of the process's exe module."""
    mods = (ctypes.c_void_p * 1)()
    need = wt.DWORD()
    if not psapi.EnumProcessModulesEx(proc.h, mods, ctypes.sizeof(mods), ctypes.byref(need), 0x03):
        raise OSError(f"EnumProcessModulesEx failed: {ctypes.get_last_error()}")
    info = MODULEINFO()
    psapi.GetModuleInformation(proc.h, mods[0], ctypes.byref(info), ctypes.sizeof(info))
    return info.lpBaseOfDll, info.SizeOfImage


def scan_regions(proc, mod_base, mod_size):
    """Readable regions that can hold pointers: private heap memory plus the exe's writable data."""
    out, addr, mbi = [], 0, MBI()
    while addr < USER_SPACE_END:
        if not k32.VirtualQueryEx(proc.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base, size = mbi.BaseAddress, mbi.RegionSize
        if mbi.State == MEM_COMMIT and not mbi.Protect & PAGE_GUARD:
            overlaps_exe = base < mod_base + mod_size and mod_base < base + size
            if mbi.Protect in WRITABLE and (mbi.Type == 0x20000 or overlaps_exe):
                out.append((base, size))
        addr = base + size
    return out


def pointers_into(proc, regions, targets, maxoff, mod_base, mod_size):
    """Every 8-byte-aligned qword v with t - maxoff <= v <= t for some target t.
    Returns arrays (location, target, offset t - v, in_exe)."""
    targets = np.unique(np.asarray(targets, dtype=np.uint64))
    lo, hi = targets[0] - np.uint64(maxoff), targets[-1]
    locs, tgts, offs, static = [], [], [], []
    for base, size in regions:
        for start in range(0, size, CHUNK):
            n = min(CHUNK, size - start)
            buf = proc.read_array(base + start, n)
            if buf is None:
                continue
            v = buf.view(np.uint64)
            cand = np.nonzero((v >= lo) & (v <= hi))[0]
            if not cand.size:
                continue
            vv = v[cand]
            j = np.searchsorted(targets, vv, side="left")
            ok = j < targets.size
            j, cand, vv = j[ok], cand[ok], vv[ok]
            d = targets[j] - vv
            ok = d <= np.uint64(maxoff)
            if not ok.any():
                continue
            loc = np.uint64(base + start) + cand[ok].astype(np.uint64) * np.uint64(8)
            locs.append(loc)
            tgts.append(targets[j[ok]])
            offs.append(d[ok])
            static.append((loc >= np.uint64(mod_base)) & (loc < np.uint64(mod_base + mod_size)))
    if not locs:
        e = np.array([], dtype=np.uint64)
        return e, e, e, np.array([], dtype=bool)
    return np.concatenate(locs), np.concatenate(tgts), np.concatenate(offs), np.concatenate(static)


def find_paths(proc, positions, depth=5, maxoff=(0x1000, 0x800, 0x800, 0x800, 0x800), cap=50000, log=print):
    mod_base, mod_size = main_module(proc)
    regions = scan_regions(proc, mod_base, mod_size)
    log(f"exe at 0x{mod_base:X} ({mod_size / 2**20:.0f} MiB); {len(regions)} regions to search")
    links = []  # per level: {location: (target, offset)}
    targets = [int(p) for p in positions]
    paths = []
    for lvl in range(depth):
        t0 = time.time()
        loc, tgt, off, st = pointers_into(proc, regions, targets, maxoff[min(lvl, len(maxoff) - 1)], mod_base, mod_size)
        links.append({int(l): (int(t), int(o)) for l, t, o in zip(loc, tgt, off)})
        for l in loc[st]:
            l = int(l)
            offsets, node = [], l
            for k in range(lvl, -1, -1):
                node, o = links[k][node]
                offsets.append(o)
            paths.append({"module_offset": l - mod_base, "offsets": offsets})
        heap = np.unique(loc[~st])
        log(f"level {lvl + 1}: {loc.size:,} pointers ({int(st.sum())} static) in {time.time() - t0:.0f}s; total paths {len(paths)}")
        if not heap.size:
            break
        if heap.size > cap:
            heap = heap[np.linspace(0, heap.size - 1, cap).astype(np.int64)]
        targets = heap
    return mod_base, paths


def _read_ptr(proc, addr):
    buf = proc.read_array(addr, 8)
    if buf is None:
        return None
    a = int(buf.view(np.uint64)[0])
    return a if 0x10000 <= a < USER_SPACE_END else None


def resolve(proc, mod_base, path):
    """Follow a path; returns the position address or None if any link is broken."""
    a = _read_ptr(proc, mod_base + path["module_offset"])
    for off in path["offsets"][:-1]:
        if a is None:
            return None
        a = _read_ptr(proc, a + off)
    return None if a is None else a + path["offsets"][-1]
