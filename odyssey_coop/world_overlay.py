"""Full-screen, click-through, transparent overlay that draws each friend as an animated
figure in the game view, using the live game camera (read-only).

Needs the game in borderless-window mode at the screen's resolution. Clicks and keys go
straight through to the game.
"""
import ctypes
import math
import time
import tkinter as tk

from .camera import project
from .figure import BONES, HEAD_R, Animator
from .net import STALE

KEY = "#010203"  # transparent colour key
PALETTE = ["#4fc3f7", "#ffb74d", "#ba68c8", "#81c784"]  # up to 4 players get distinct colours
MAX_DRAW_DIST = 400.0  # metres; further friends only get an edge marker
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x80000, 0x20, 0x80, 0x8


def click_through(widget):
    u = ctypes.windll.user32
    hwnd = u.GetParent(widget.winfo_id()) or widget.winfo_id()
    style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


def edge_marker(sx, sy, w, h, margin=40):
    """Clamp an off-screen point to the screen edge along the ray from the centre."""
    cx, cy = w / 2, h / 2
    dx, dy = sx - cx, sy - cy
    if dx == 0 and dy == 0:
        return cx, cy
    k = min((w / 2 - margin) / abs(dx) if dx else 1e9, (h / 2 - margin) / abs(dy) if dy else 1e9)
    return cx + dx * k, cy + dy * k


def figure_shapes(cam, joints, width, height):
    """Projected line segments + head circle for one figure (pure, for tests).
    Returns None if the figure is behind the camera."""
    pts = {n: project(cam, p, width, height) for n, p in joints.items()}
    if any(v is None for v in pts.values()):
        return None
    head = pts["head"]
    fy = cam["P"][1, 1]
    px_per_m = fy * height / 2 / head[2]
    lines = [(pts[a][:2], pts[b][:2]) for a, b in BONES]
    return {"lines": lines, "head": (head[0], head[1], max(2.0, HEAD_R * px_per_m)),
            "width": max(2.0, 0.07 * px_per_m), "depth": head[2]}


class WorldOverlay:
    def __init__(self, master, peer, camera, game=None):
        self.peer, self.camera = peer, camera
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.w, self.h = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{self.w}x{self.h}+0+0")
        self.win.attributes("-topmost", True)
        self.win.config(bg=KEY)
        self.win.attributes("-transparentcolor", KEY)
        self.c = tk.Canvas(self.win, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        self.c.pack()
        self.win.update_idletasks()
        click_through(self.win)
        self.anim = {}
        self.colors = {}
        self.stats = {"frames": 0, "no_camera": 0}

    def color(self, name):
        if name not in self.colors:
            self.colors[name] = PALETTE[len(self.colors) % len(PALETTE)]
        return self.colors[name]

    def draw(self):
        c = self.c
        c.delete("all")
        self.stats["frames"] += 1
        cam = self.camera.read()
        meta = self.peer.snapshot(meta=True)
        now = time.time()
        for name in list(self.anim):
            if name not in meta:
                del self.anim[name]
        if cam is None:
            self.stats["no_camera"] += 1
        for name, m in sorted(meta.items()):
            a = self.anim.setdefault(name, Animator())
            a.update(m["pos"], now)
            if cam is None:
                continue
            greyed = m["age"] >= STALE or m["state"] == "loading"
            col = "#888888" if greyed else self.color(name)
            dist = math.dist(cam["pos"], m["pos"])
            tag = f"{name}  {dist:.0f}m" + ("  (loading)" if m["state"] == "loading" else "  (lag)" if m["age"] >= STALE else "")
            if not m["build_ok"]:
                tag = "⚠ " + tag + "  (other game version)"
            shapes = figure_shapes(cam, a.joints(), self.w, self.h) if dist < MAX_DRAW_DIST else None
            on_screen = shapes and 0 <= shapes["head"][0] <= self.w and 0 <= shapes["head"][1] <= self.h
            if on_screen:
                for (x0, y0), (x1, y1) in shapes["lines"]:
                    c.create_line(x0, y0, x1, y1, fill=col, width=shapes["width"], capstyle="round")
                hx, hy, hr = shapes["head"]
                c.create_oval(hx - hr, hy - hr, hx + hr, hy + hr, fill=col, outline="")
                c.create_text(hx, hy - hr - 12, text=tag, fill=col, font=("Segoe UI", 10, "bold"))
            else:
                p = project(cam, [m["pos"][0], m["pos"][1], m["pos"][2] + 1.6], self.w, self.h)
                if p is None:  # behind: mirror so the marker points the right way
                    v = [m["pos"][k] - cam["pos"][k] for k in range(3)]
                    behind = [cam["pos"][k] - v[k] for k in range(3)]
                    q = project(cam, behind, self.w, self.h)
                    sx, sy = (self.w - q[0], self.h) if q else (self.w / 2, self.h)
                else:
                    sx, sy = p[0], p[1]
                ex, ey = edge_marker(sx, sy, self.w, self.h)
                c.create_oval(ex - 8, ey - 8, ex + 8, ey + 8, fill=col, outline="")
                c.create_text(ex, ey - 18, text=tag, fill=col, font=("Segoe UI", 10, "bold"))
        self.win.after(33, self.draw)

    def start(self):
        self.draw()
