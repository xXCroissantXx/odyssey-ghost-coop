"""Tiny UDP position sync. One player hosts; everyone else sends to the host, and
the host relays the full player list back 20 times a second.

Protocol v2:
  client -> host  {"v":2, "name", "pos":[x,y,z], "seq", "sid", "build", "state"}
  host -> client  {"v":2, "you": <name the host gave you>, "players":[{"name","pos","seq","sid","build","state"}]}

state is "ok", or "loading" while a player sits on a loading screen or menu: their last real
position is sent, so friends see them greyed out where they were, not at a fake spot.

Every position carries its sender's sequence number and session id (random per run),
so reordered or duplicated packets never move a player backwards, and a player who
restarts is picked up at once. Anything malformed is counted and dropped.
"""
import json
import math
import random
import socket
import threading
import time
from collections import Counter

PROTO = 2
TICK = 0.05
TIMEOUT = 5.0  # a player silent this long is removed
STALE = 1.0  # a player silent this long is shown as lagging
STATES = ("ok", "loading")
MAX_PACKET = 65535
MAX_NAME = 24
MAX_CLIENTS = 16


def _valid_pos(pos):
    return (isinstance(pos, list) and len(pos) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and abs(c) < 1e5 for c in pos))


def _clean_name(name):
    if not isinstance(name, str):
        return None
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:MAX_NAME]
    return name or None


class Peer:
    def __init__(self, name, source, host=None, port=27015, bind="0.0.0.0"):
        self.name = _clean_name(name) or "player"
        self.assigned = self.name  # the host may rename us if the name is taken
        self.source = source
        self.build = getattr(source, "build", None)
        self.is_host = host is None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.is_host:
            self.sock.bind((bind, port))
            self.server = None
        else:
            self.sock.bind((bind, 0))
            self.server = (socket.gethostbyname(host), port)
        self.sock.settimeout(0.2)
        self.seq = 0
        self.sid = random.getrandbits(32)
        self.last_pos = None
        self.players = {}  # name -> {"pos", "seen", "seq", "build"}
        self.clients = {}  # host only: addr -> {"name", "seen"}
        self.stats = Counter()
        self.lock = threading.Lock()
        self.running = False
        self.threads = []

    # --- state -------------------------------------------------------
    def snapshot(self, meta=False):
        """Other players. meta=True adds age (s) and whether their game build matches ours."""
        now = time.time()
        me = {self.assigned}
        with self.lock:
            out = {}
            for n, p in self.players.items():
                age = now - p["seen"]
                if n in me or age >= TIMEOUT:
                    continue
                if meta:
                    out[n] = {"pos": p["pos"], "age": age, "state": p["state"],
                              "build_ok": p["build"] is None or self.build is None or p["build"] == self.build}
                else:
                    out[n] = p["pos"]
            return out

    def _accept(self, name, pos, seq, build, sid=None, state="ok"):
        name = _clean_name(name)
        if name is None or not _valid_pos(pos) or not isinstance(seq, int) or isinstance(seq, bool):
            self.stats["bad"] += 1
            return
        build = build if isinstance(build, str) else None
        now = time.time()
        with self.lock:
            p = self.players.get(name)
            if p and p["sid"] == sid and seq <= p["seq"] and now - p["seen"] < TIMEOUT:
                self.stats["old_dropped"] += 1
                return
            self.players[name] = {"pos": [float(c) for c in pos], "seen": now, "seq": seq, "sid": sid,
                                  "build": build[:64] if build else None, "state": state if state in STATES else "ok"}

    def _claim(self, addr, wanted):
        """Host: the name a client address plays under. Fixed after the first packet; duplicates get a suffix."""
        now = time.time()
        with self.lock:
            c = self.clients.get(addr)
            if c:
                c["seen"] = now
                return c["name"]
            wanted = _clean_name(wanted)
            if wanted is None:
                return None
            if sum(1 for v in self.clients.values() if now - v["seen"] < TIMEOUT) >= MAX_CLIENTS:
                self.stats["full"] += 1
                return None
            taken = {self.name} | {v["name"] for v in self.clients.values() if now - v["seen"] < TIMEOUT}
            name, i = wanted, 2
            while name in taken:
                name = f"{wanted[:MAX_NAME - 3]}-{i}"
                i += 1
            self.clients[addr] = {"name": name, "seen": now}
            return name

    # --- loops -------------------------------------------------------
    def start(self):
        self.running = True
        self.threads = [threading.Thread(target=self._recv_loop, daemon=True),
                        threading.Thread(target=self._send_loop, daemon=True)]
        for t in self.threads:
            t.start()

    def stop(self):
        self.running = False
        for t in self.threads:
            t.join(timeout=1.0)
        self.sock.close()

    def alive(self):
        return all(t.is_alive() for t in self.threads)

    def _send(self, msg, addr):
        try:
            self.sock.sendto(json.dumps(msg).encode(), addr)
        except OSError:
            self.stats["send_err"] += 1

    def _send_loop(self):
        while self.running:
            try:
                pos = self.source.position()
            except Exception:
                self.stats["source_err"] += 1
                pos = None
            state = "ok"
            if pos is not None and _valid_pos(list(pos)):
                self.last_pos = list(pos)
            elif getattr(self.source, "state", None) == "loading" and self.last_pos is not None:
                pos, state = self.last_pos, "loading"  # stay where we were, greyed out
            else:
                pos = None
            if pos is not None:
                self.seq += 1
                self._accept(self.assigned, list(pos), self.seq, self.build, self.sid, state)
            if self.is_host:
                now = time.time()
                with self.lock:
                    for addr in [a for a, c in self.clients.items() if now - c["seen"] >= TIMEOUT]:
                        del self.clients[addr]
                    players = [{"name": n, "pos": p["pos"], "seq": p["seq"], "sid": p["sid"], "build": p["build"], "state": p["state"]}
                               for n, p in self.players.items() if now - p["seen"] < TIMEOUT]
                    targets = [(a, c["name"]) for a, c in self.clients.items()]
                for addr, name in targets:
                    self._send({"v": PROTO, "you": name, "players": players}, addr)
            elif pos is not None:
                self._send({"v": PROTO, "name": self.name, "pos": list(pos), "seq": self.seq, "sid": self.sid,
                            "build": self.build, "state": state}, self.server)
            time.sleep(TICK)

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:  # ICMP port unreachable, oversized datagram, closed socket
                self.stats["recv_err"] += 1
                if not self.running:
                    return
                continue
            try:
                self._handle(data, addr)
            except Exception:
                self.stats["bad"] += 1

    def _handle(self, data, addr):
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.stats["bad"] += 1
            return
        if not isinstance(msg, dict) or msg.get("v") != PROTO:
            self.stats["bad"] += 1
            return
        if self.is_host:
            if "name" not in msg:
                self.stats["bad"] += 1
                return
            name = self._claim(addr, msg["name"])
            if name:
                self._accept(name, msg.get("pos"), msg.get("seq"), msg.get("build"), msg.get("sid"), msg.get("state"))
        elif addr == self.server:
            you = _clean_name(msg.get("you"))
            if you and you != self.assigned:
                with self.lock:
                    self.players.pop(self.assigned, None)  # our own entry under the old name
                self.assigned = you
            players = msg.get("players")
            if not isinstance(players, list):
                self.stats["bad"] += 1
                return
            for p in players[:64]:
                if isinstance(p, dict):
                    self._accept(p.get("name"), p.get("pos"), p.get("seq"), p.get("build"), p.get("sid"), p.get("state"))
        else:
            self.stats["foreign"] += 1
