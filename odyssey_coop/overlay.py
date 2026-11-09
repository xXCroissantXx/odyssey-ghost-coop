"""Always-on-top minimap showing where your friends are relative to you (north up).
Run the game in borderless-window mode so this stays visible over it.

Friends glide between network updates instead of jumping, turn grey with "lag" when
nothing has arrived for a second, and show a warning when their game build differs."""
import math
import tkinter as tk

from .net import STALE

SIZE = 240
PAD = 18
COLORS = ["#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#e57373"]
SMOOTH = 0.35  # share of the remaining distance covered each frame
SNAP = 60.0  # jumps larger than this (fast travel) are not animated


class Smoother:
    """Per-player display positions that ease toward the latest network position."""

    def __init__(self):
        self.shown = {}

    def update(self, targets):
        for name in list(self.shown):
            if name not in targets:
                del self.shown[name]
        for name, t in targets.items():
            s = self.shown.get(name)
            if s is None or math.dist(s, t) > SNAP:
                self.shown[name] = list(t)
            else:
                self.shown[name] = [s[k] + (t[k] - s[k]) * SMOOTH for k in range(3)]
        return self.shown


def layout(me, others, size=SIZE, flip_x=False, flip_y=False):
    """Screen positions (x, y, label) for other players around `me` (pure, for tests)."""
    mid = size / 2
    dists = [math.hypot(p[0] - me[0], p[1] - me[1]) for p in others.values()]
    scale = (mid - PAD) / max(50.0, max(dists, default=0) * 1.15)
    out = {}
    for name, p in others.items():
        dx, dy = p[0] - me[0], p[1] - me[1]
        if flip_x:
            dx = -dx
        if flip_y:
            dy = -dy
        dz = p[2] - me[2]
        label = f"{name} {math.hypot(dx, dy):.0f}m" + (f" {'▲' if dz > 0 else '▼'}{abs(dz):.0f}" if abs(dz) > 3 else "")
        out[name] = (mid + dx * scale, mid - dy * scale, label)
    return out


class Minimap:
    def __init__(self, peer, flip_x=False, flip_y=False):
        self.peer, self.fx, self.fy = peer, flip_x, flip_y
        self.smooth = Smoother()
        self.root = tk.Tk()
        self.root.title("Odyssey co-op")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.root.geometry(f"{SIZE}x{SIZE}+20+20")
        self.c = tk.Canvas(self.root, width=SIZE, height=SIZE, bg="#111", highlightthickness=0)
        self.c.pack()
        self.c.bind("<ButtonPress-1>", self._grab)
        self.c.bind("<B1-Motion>", self._drag)
        self.c.bind("<Double-Button-1>", lambda e: self.root.destroy())

    def _grab(self, e):
        self._off = (e.x, e.y)

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")

    def draw(self):
        c, mid = self.c, SIZE / 2
        c.delete("all")
        c.create_oval(4, 4, SIZE - 4, SIZE - 4, outline="#555")
        c.create_text(mid, 12, text="N", fill="#aaa", font=("Segoe UI", 9, "bold"))
        with self.peer.lock:
            mine = self.peer.players.get(self.peer.assigned)
            me = list(mine["pos"]) if mine else None
        meta = self.peer.snapshot(meta=True)
        shown = self.smooth.update({n: m["pos"] for n, m in meta.items()})
        c.create_oval(mid - 5, mid - 5, mid + 5, mid + 5, fill="white", outline="")
        if me is None:
            c.create_text(mid, SIZE - 14, text="no position from game", fill="#e57373")
        else:
            spots = layout(me, shown, flip_x=self.fx, flip_y=self.fy)
            for i, name in enumerate(sorted(spots)):
                x, y, label = spots[name]
                m = meta[name]
                waiting = m["age"] >= STALE or m["state"] == "loading"
                col = "#777" if waiting else COLORS[i % len(COLORS)]
                if m["state"] == "loading":
                    label += " (loading)"
                elif m["age"] >= STALE:
                    label += f" (lag {m['age']:.0f}s)"
                if not m["build_ok"]:
                    label = "⚠ " + label + " (other game version)"
                c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=col, outline="")
                c.create_text(x, y - 13, text=label, fill=col, font=("Segoe UI", 8))
            if not spots:
                c.create_text(mid, SIZE - 14, text="waiting for friends…", fill="#888")
        self.root.after(50, self.draw)

    def run(self):
        self.draw()
        self.root.mainloop()
