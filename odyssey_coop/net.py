"""Tiny UDP position sync. One player hosts; everyone else sends to the host,
and the host relays the full player list back 20 times a second."""
import json
import socket
import threading
import time

PROTO = 1
TICK = 0.05
TIMEOUT = 5.0
MAX_PACKET = 8192


class Peer:
    def __init__(self, name, source, host=None, port=27015):
        self.name, self.source = name, source
        self.is_host = host is None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.is_host:
            self.sock.bind(("0.0.0.0", port))
            self.server = None
        else:
            self.sock.bind(("0.0.0.0", 0))
            self.server = (socket.gethostbyname(host), port)
        self.sock.settimeout(0.2)
        self.players = {}  # name -> {"pos": [x,y,z], "seen": t}
        self.clients = {}  # host only: addr -> name
        self.lock = threading.Lock()
        self.running = False

    # --- state -------------------------------------------------------
    def snapshot(self):
        """Other players' latest positions, dropping anyone silent for TIMEOUT seconds."""
        now = time.time()
        with self.lock:
            return {n: p["pos"] for n, p in self.players.items()
                    if n != self.name and now - p["seen"] < TIMEOUT}

    def _update(self, name, pos):
        if isinstance(name, str) and isinstance(pos, list) and len(pos) == 3 and all(isinstance(c, (int, float)) for c in pos):
            with self.lock:
                self.players[name[:32]] = {"pos": [float(c) for c in pos], "seen": time.time()}

    # --- loops -------------------------------------------------------
    def start(self):
        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _send_loop(self):
        while self.running:
            pos = self.source.position()
            if pos is not None:
                self._update(self.name, pos)
            if self.is_host:
                with self.lock:
                    now = time.time()
                    players = [{"name": n, "pos": p["pos"]} for n, p in self.players.items() if now - p["seen"] < TIMEOUT]
                    targets = list(self.clients)
                msg = json.dumps({"v": PROTO, "players": players}).encode()
                for addr in targets:
                    self.sock.sendto(msg, addr)
            elif pos is not None:
                self.sock.sendto(json.dumps({"v": PROTO, "name": self.name, "pos": pos}).encode(), self.server)
            time.sleep(TICK)

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(MAX_PACKET)
                msg = json.loads(data)
            except (socket.timeout, ValueError, ConnectionResetError):
                continue
            if not isinstance(msg, dict) or msg.get("v") != PROTO:
                continue
            if self.is_host and "name" in msg:
                with self.lock:
                    self.clients[addr] = msg["name"]
                self._update(msg["name"], msg.get("pos"))
            elif not self.is_host and addr == self.server:
                for p in msg.get("players", []):
                    if isinstance(p, dict):
                        self._update(p.get("name"), p.get("pos"))
