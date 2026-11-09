"""Full-screen, click-through, transparent overlay that draws each friend as an animated
figure in the game view, using the live game camera (read-only).

The figure is an original, generic hooded traveller made of solid shapes: a cloak, a
body, limbs and a hood, in the player's colours. The whole layer is semi-transparent, so
friends read as "ghosts": the overlay cannot know what is in front of them, so they show
through walls by design. Friends further than FIGURE_DIST only get a marker.

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
GHOST_ALPHA = 0.72  # whole overlay opacity
FIGURE_DIST = 150.0  # metres; beyond this only a marker is drawn
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x80000, 0x20, 0x80, 0x8

LEGS = [("l_hip", "l_knee"), ("l_knee", "l_foot"), ("r_hip", "r_knee"), ("r_knee", "r_foot")]
ARMS = [("l_shoulder", "l_elbow"), ("l_elbow", "l_hand"), ("r_shoulder", "r_elbow"), ("r_elbow", "r_hand")]
CLOAK = ["cloak_l_top", "cloak_r_top", "cloak_r_hem", "cloak_mid_hem", "cloak_l_hem"]
TORSO = ["l_shoulder", "r_shoulder", "r_hip", "l_hip"]
HOOD = ["hood_l", "hood_peak", "hood_r", "hood_back"]


def click_through(widget):
    u = ctypes.windll.user32
    hwnd = u.GetParent(widget.winfo_id()) or widget.winfo_id()
    style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


def shade(hex_color, f):
    """Darken (f < 1) or lighten (f > 1) a #rrggbb colour."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if f <= 1:
        r, g, b = (int(c * f) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * (f - 1)) for c in (r, g, b))
    return f"#{max(r, 4):02x}{max(g, 4):02x}{max(b, 4):02x}"  # never the transparent key


def edge_marker(sx, sy, w, h, margin=40):
    """Clamp an off-screen point to the screen edge along the ray from the centre."""
    cx, cy = w / 2, h / 2
    dx, dy = sx - cx, sy - cy
    if dx == 0 and dy == 0:
        return cx, cy
    k = min((w / 2 - margin) / abs(dx) if dx else 1e9, (h / 2 - margin) / abs(dy) if dy else 1e9)
    return cx + dx * k, cy + dy * k


def figure_shapes(cam, joints, width, height):
    """Projected parts of one figure (pure, for tests). None if any part is behind the camera."""
    pts = {n: project(cam, p, width, height) for n, p in joints.items()}
    if any(v is None for v in pts.values()):
        return None
    head = pts["head"]
    px_per_m = cam["P"][1, 1] * height / 2 / head[2]
    xy = {n: v[:2] for n, v in pts.items()}
    return {
        "lines": [(xy[a], xy[b]) for a, b in BONES],
        "legs": [(xy[a], xy[b]) for a, b in LEGS],
        "arms": [(xy[a], xy[b]) for a, b in ARMS],
        "hands": [xy["l_hand"], xy["r_hand"]],
        "cloak": [xy[n] for n in CLOAK],
        "torso": [xy[n] for n in TORSO],
        "hood": [xy[n] for n in HOOD],
        "head": (head[0], head[1], max(2.0, HEAD_R * px_per_m)),
        "limb_w": max(2.0, 0.11 * px_per_m),
        "depth": head[2],
    }


def draw_figure(c, s, col):
    """Draw one figure: dark outline pass, then cloak, legs, torso, arms, head and hood."""
    dark, trim, light = shade(col, 0.35), shade(col, 0.6), shade(col, 1.25)
    w = s["limb_w"]
    ow = w + max(2.0, w * 0.35)
    flat = lambda pts: [v for p in pts for v in p]  # noqa: E731
    # outline pass (makes the shape readable on bright scenes)
    c.create_polygon(flat(s["cloak"]), fill=dark, outline=dark, width=max(2, w * 0.3))
    for (a, b) in s["legs"] + s["arms"]:
        c.create_line(*a, *b, fill=dark, width=ow, capstyle="round")
    # body
    c.create_polygon(flat(s["cloak"]), fill=trim, outline="")
    for (a, b) in s["legs"]:
        c.create_line(*a, *b, fill=trim, width=w, capstyle="round")
    c.create_polygon(flat(s["torso"]), fill=col, outline=dark, width=max(1, w * 0.2))
    for (a, b) in s["arms"]:
        c.create_line(*a, *b, fill=col, width=w * 0.85, capstyle="round")
    for hx, hy in s["hands"]:
        r = w * 0.45
        c.create_oval(hx - r, hy - r, hx + r, hy + r, fill=light, outline="")
    hx, hy, hr = s["head"]
    c.create_polygon(flat(s["hood"]), fill=trim, outline=dark, width=max(1, w * 0.2), smooth=True)
    c.create_oval(hx - hr, hy - hr, hx + hr, hy + hr, fill=light, outline=dark, width=max(1, hr * 0.15))


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
        self.win.attributes("-alpha", GHOST_ALPHA)
        self.c = tk.Canvas(self.win, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        self.c.pack()
        self.win.update_idletasks()
        click_through(self.win)
        self.anim = {}
        self.colors = {}
        self.stats = {"frames": 0, "no_camera": 0, "fps": 0.0}
        self._fps_t, self._fps_n = time.time(), 0

    def color(self, name):
        if name not in self.colors:
            self.colors[name] = PALETTE[len(self.colors) % len(PALETTE)]
        return self.colors[name]

    def draw(self):
        t_start = time.time()
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
        order = []
        for name, m in meta.items():
            a = self.anim.setdefault(name, Animator())
            a.update(m["pos"], now)
            if cam is not None:
                order.append((math.dist(cam["pos"], m["pos"]), name, m, a))
        for dist, name, m, a in sorted(order, reverse=True):  # far to near
            greyed = m["age"] >= STALE or m["state"] == "loading"
            col = "#8a8a8a" if greyed else self.color(name)
            tag = f"{name}  {dist:.0f}m" + ("  (loading)" if m["state"] == "loading" else "  (lag)" if m["age"] >= STALE else "")
            if not m["build_ok"]:
                tag = "⚠ " + tag + "  (other game version)"
            s = figure_shapes(cam, a.joints(), self.w, self.h) if dist < FIGURE_DIST else None
            if s and 0 <= s["head"][0] <= self.w and 0 <= s["head"][1] <= self.h:
                draw_figure(c, s, col)
                hx, hy, hr = s["head"]
                c.create_text(hx + 1, hy - hr * 1.6 - 11, text=tag, fill="#050505", font=("Segoe UI", 10, "bold"))
                c.create_text(hx, hy - hr * 1.6 - 12, text=tag, fill=col, font=("Segoe UI", 10, "bold"))
                continue
            p = project(cam, [m["pos"][0], m["pos"][1], m["pos"][2] + 1.6], self.w, self.h)
            if p is not None and 0 <= p[0] <= self.w and 0 <= p[1] <= self.h:
                ex, ey = p[0], p[1]  # far but on screen: a diamond where they are
            else:
                if p is None:  # behind: mirror so the marker points the right way
                    v = [m["pos"][k] - cam["pos"][k] for k in range(3)]
                    q = project(cam, [cam["pos"][k] - v[k] for k in range(3)], self.w, self.h)
                    sx, sy = (self.w - q[0], self.h) if q else (self.w / 2, self.h)
                else:
                    sx, sy = p[0], p[1]
                ex, ey = edge_marker(sx, sy, self.w, self.h)
            c.create_polygon(ex, ey - 10, ex + 8, ey, ex, ey + 10, ex - 8, ey, fill=col, outline=shade(col, 0.35), width=2)
            c.create_text(ex, ey - 20, text=tag, fill=col, font=("Segoe UI", 10, "bold"))
        self._fps_n += 1
        if now - self._fps_t >= 2.0:
            self.stats["fps"] = round(self._fps_n / (now - self._fps_t), 1)
            self._fps_t, self._fps_n = now, 0
        spent = (time.time() - t_start) * 1000
        self.win.after(max(1, int(33 - spent)), self.draw)

    def start(self):
        # Tk timers follow the Windows timer tick (15.6 ms by default), which rounded every
        # frame wait up and capped the overlay at ~24 fps. Ask for 1 ms ticks while we run.
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            self.win.bind("<Destroy>", lambda e: ctypes.windll.winmm.timeEndPeriod(1) if e.widget is self.win else None)
        except (AttributeError, OSError):
            pass
        self.draw()
