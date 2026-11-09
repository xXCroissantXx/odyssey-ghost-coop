"""Always-on-top minimap showing where your friends are relative to you (north up).
Run the game in borderless-window mode so this stays visible over it."""
import math
import tkinter as tk

SIZE = 240
PAD = 18
COLORS = ["#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#e57373"]


class Minimap:
    def __init__(self, peer, flip_x=False, flip_y=False):
        self.peer, self.fx, self.fy = peer, flip_x, flip_y
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
        me = self.peer.players.get(self.peer.name, {}).get("pos")
        others = self.peer.snapshot()
        c.create_oval(mid - 5, mid - 5, mid + 5, mid + 5, fill="white", outline="")
        if me is None:
            c.create_text(mid, SIZE - 14, text="no position from game", fill="#e57373")
        else:
            dists = [math.hypot(p[0] - me[0], p[1] - me[1]) for p in others.values()]
            scale = (mid - PAD) / max(50.0, max(dists, default=0) * 1.15)
            for i, (name, p) in enumerate(sorted(others.items())):
                dx, dy = p[0] - me[0], p[1] - me[1]
                if self.fx:
                    dx = -dx
                if self.fy:
                    dy = -dy
                x, y = mid + dx * scale, mid - dy * scale
                col = COLORS[i % len(COLORS)]
                c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=col, outline="")
                dz = p[2] - me[2]
                label = f"{name} {math.hypot(dx, dy):.0f}m" + (f" {'▲' if dz > 0 else '▼'}{abs(dz):.0f}" if abs(dz) > 3 else "")
                c.create_text(x, y - 13, text=label, fill=col, font=("Segoe UI", 8))
            if not others:
                c.create_text(mid, SIZE - 14, text="waiting for friends…", fill="#888")
        self.root.after(50, self.draw)

    def run(self):
        self.draw()
        self.root.mainloop()
