"""Network bot tests, no game needed: a bad link, outages, host restarts, junk packets,
duplicate names, version mismatches, and the bot movement sources.

The bad link is a small in-process UDP relay between host and client that drops,
delays, jitters (which reorders) and duplicates datagrams, like a poor internet path.
"""
import heapq
import json
import math
import os
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import odyssey_coop.net as net  # noqa: E402
from odyssey_coop.net import Peer  # noqa: E402
from odyssey_coop.overlay import SNAP, Smoother, layout  # noqa: E402
from odyssey_coop.sources import CircleBot, FollowBot, OrbitBot, ReplayBot  # noqa: E402

_ports = iter(range(28100, 28900, 7))


class LineBot:
    """Moves east at a steady speed, so x must never go backwards on the receiver."""

    def __init__(self, build="bot", speed=5.0):
        self.build, self.speed, self.t0 = build, speed, time.time()

    def position(self):
        return [(time.time() - self.t0) * self.speed, 0.0, 1.5]


class BadLink:
    """UDP relay with loss, delay + jitter, duplicates and an on/off partition."""

    def __init__(self, target, loss=0.0, delay=0.0, jitter=0.0, dup=0.0, seed=1):
        self.target = target
        self.loss, self.delay, self.jitter, self.dup = loss, delay, jitter, dup
        self.rng = random.Random(seed)
        self.down = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.up = {}  # client addr -> upstream socket
        self.q, self.qlock = [], threading.Lock()
        self.running = True
        self.n = 0
        threading.Thread(target=self._from_clients, daemon=True).start()
        threading.Thread(target=self._pump, daemon=True).start()

    def _schedule(self, data, sock, addr):
        if self.down or self.rng.random() < self.loss:
            return
        for _ in range(2 if self.rng.random() < self.dup else 1):
            at = time.time() + max(0.0, self.delay + self.rng.uniform(-self.jitter, self.jitter))
            with self.qlock:
                self.n += 1
                heapq.heappush(self.q, (at, self.n, data, sock, addr))

    def _from_clients(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            if addr not in self.up:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(("127.0.0.1", 0))
                s.settimeout(0.05)
                self.up[addr] = s
                threading.Thread(target=self._from_host, args=(s, addr), daemon=True).start()
            self._schedule(data, self.up[addr], self.target)

    def _from_host(self, s, client):
        while self.running:
            try:
                data, _ = s.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            self._schedule(data, self.sock, client)

    def _pump(self):
        while self.running:
            now = time.time()
            due = []
            with self.qlock:
                while self.q and self.q[0][0] <= now:
                    due.append(heapq.heappop(self.q))
            for _, _, data, sock, addr in due:
                try:
                    sock.sendto(data, addr)
                except OSError:
                    pass
            time.sleep(0.002)

    def close(self):
        self.running = False


def wait_for(cond, timeout=3.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class BadNetwork(unittest.TestCase):
    def test_lossy_jittery_link(self):
        port = next(_ports)
        host = Peer("host", CircleBot(0), port=port)
        link = BadLink(("127.0.0.1", port), loss=0.2, delay=0.08, jitter=0.06, dup=0.1)
        cli = Peer("walker", LineBot(), host="127.0.0.1", port=link.port)
        host.start(); cli.start()
        try:
            self.assertTrue(wait_for(lambda: "walker" in host.snapshot()))
            fresh = total = backwards = 0
            last_x = -1.0
            t_end = time.time() + 6.0
            while time.time() < t_end:
                m = host.snapshot(meta=True).get("walker")
                total += 1
                if m:
                    fresh += m["age"] < net.STALE
                    backwards += m["pos"][0] < last_x - 1e-6
                    last_x = max(last_x, m["pos"][0])
                time.sleep(0.05)
            print(f"\n  bad link: host saw walker fresh {fresh}/{total}, went backwards {backwards} times, "
                  f"dropped {host.stats['old_dropped']} old/duplicate packets")
            self.assertGreaterEqual(fresh / total, 0.95)
            self.assertEqual(backwards, 0)
            self.assertGreater(host.stats["old_dropped"], 0, "reorder/dup handling was never exercised")
            self.assertIn("host", cli.snapshot())
        finally:
            cli.stop(); host.stop(); link.close()

    def test_outage_shows_lag_then_recovers(self):
        port = next(_ports)
        old_timeout = net.TIMEOUT
        net.TIMEOUT = 2.0
        host = Peer("host", CircleBot(0), port=port)
        link = BadLink(("127.0.0.1", port))
        cli = Peer("walker", LineBot(), host="127.0.0.1", port=link.port)
        host.start(); cli.start()
        try:
            self.assertTrue(wait_for(lambda: "walker" in host.snapshot()))
            link.down = True
            time.sleep(1.4)
            m = host.snapshot(meta=True).get("walker")
            self.assertIsNotNone(m, "a short outage must not remove the player")
            self.assertGreater(m["age"], net.STALE, "a 1.4 s outage must show as lag")
            link.down = False
            self.assertTrue(wait_for(lambda: host.snapshot(meta=True).get("walker", {}).get("age", 9) < 0.3, 1.0),
                            "player must be fresh again within 1 s after the link returns")
            link.down = True
            self.assertTrue(wait_for(lambda: "walker" not in host.snapshot(), 3.5), "a long outage removes the player")
            link.down = False
            self.assertTrue(wait_for(lambda: "walker" in host.snapshot(), 1.0), "player rejoins by itself")
            self.assertTrue(cli.alive() and host.alive())
        finally:
            net.TIMEOUT = old_timeout
            cli.stop(); host.stop(); link.close()

    def test_host_restart(self):
        port = next(_ports)
        host = Peer("host", CircleBot(0), port=port)
        cli = Peer("walker", LineBot(), host="127.0.0.1", port=port)
        host.start(); cli.start()
        try:
            self.assertTrue(wait_for(lambda: "host" in cli.snapshot()))
            host.stop()
            time.sleep(0.5)
            host = Peer("host", CircleBot(5), port=port)  # new session, seq starts at 1 again
            host.start()
            new_pos = lambda: host.source.position()  # noqa: E731
            self.assertTrue(wait_for(lambda: "walker" in host.snapshot(), 1.0), "restarted host sees the client")
            self.assertTrue(wait_for(lambda: math.dist(cli.snapshot().get("host", [9e9] * 3), new_pos()) < 5, 1.0),
                            "client follows the restarted host at once, not after the old one times out")
            self.assertTrue(cli.alive())
        finally:
            cli.stop(); host.stop()


class Junk(unittest.TestCase):
    def test_host_survives_junk(self):
        port = next(_ports)
        host = Peer("host", CircleBot(0), port=port)
        good = Peer("good", LineBot(), host="127.0.0.1", port=port)
        host.start(); good.start()
        rng = random.Random(7)
        junk = [b"", b"\x00" * 10, b"{", b"null", b"[]", b'"x"', b"1e999", os.urandom(60000),
                b'{"v":2}', b'{"v":2,"name":5}', b'{"v":2,"name":"a","pos":"x","seq":1}',
                b'{"v":2,"name":"a","pos":[1,2],"seq":1}', b'{"v":2,"name":"a","pos":[NaN,1,2],"seq":1}',
                b'{"v":2,"name":"a","pos":[Infinity,1,2],"seq":1}', b'{"v":2,"name":"a","pos":[1e9,1,2],"seq":1}',
                b'{"v":2,"name":"a","pos":[true,1,2],"seq":1}', b'{"v":2,"name":"a","pos":[1,2,3],"seq":"1"}',
                b'{"v":2,"name":"\\u0000\\u0007","pos":[1,2,3],"seq":1}', b'{"v":1,"name":"old","pos":[1,2,3]}',
                b"[" * 100000, json.dumps({"v": 2, "name": "x" * 5000, "pos": [1, 2, 3], "seq": 1}).encode()]
        junk += [os.urandom(rng.randint(1, 2000)) for _ in range(200)]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for d in junk:
                try:
                    s.sendto(d, ("127.0.0.1", port))
                except OSError:
                    pass
            # flood of new names from many addresses: the host must cap clients
            socks = []
            for i in range(40):
                f = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                f.sendto(json.dumps({"v": 2, "name": f"flood{i}", "pos": [1, 2, 3], "seq": 1}).encode(), ("127.0.0.1", port))
                socks.append(f)
            time.sleep(1.0)
            self.assertTrue(host.alive(), "host threads died")
            self.assertIn("good", host.snapshot())
            self.assertLessEqual(len(host.clients), net.MAX_CLIENTS)
            names = set(host.snapshot())
            self.assertTrue(all(len(n) <= net.MAX_NAME and n.isprintable() for n in names))
            for p in host.snapshot().values():
                self.assertTrue(all(math.isfinite(c) and abs(c) < 1e5 for c in p))
            print(f"\n  junk: host stats {dict(host.stats)}; {len(host.clients)} clients kept")
        finally:
            good.stop(); host.stop()

    def test_client_survives_junk_from_host_address(self):
        port = next(_ports)
        host = Peer("host", CircleBot(0), port=port)
        cli = Peer("c", LineBot(), host="127.0.0.1", port=port)
        host.start(); cli.start()
        try:
            self.assertTrue(wait_for(lambda: "host" in cli.snapshot()))
            for d in [b'{"v":2,"players":5}', b'{"v":2,"players":"abc"}', b'{"v":2,"players":[1,2,null]}',
                      b'{"v":2,"you":7,"players":[]}', b'{"v":2,"players":[{"name":"z","pos":[NaN,0,0],"seq":1}]}',
                      json.dumps({"v": 2, "players": [{"name": f"p{i}", "pos": [i, 0, 0], "seq": 1} for i in range(500)]}).encode()]:
                cli._handle(d, cli.server)
            self.assertTrue(cli.alive())
            self.assertLessEqual(len(cli.players), 64 + 2)
            self.assertNotIn("z", cli.snapshot())
        finally:
            cli.stop(); host.stop()


class Names(unittest.TestCase):
    def test_duplicate_names_and_versions(self):
        port = next(_ports)
        host = Peer("host", LineBot(build="build-A"), port=port)
        a = Peer("Sam", LineBot(build="build-A"), host="127.0.0.1", port=port)
        b = Peer("Sam", LineBot(build="build-B"), host="127.0.0.1", port=port)
        host.start(); a.start()
        try:
            self.assertTrue(wait_for(lambda: "Sam" in host.snapshot()))
            b.start()
            self.assertTrue(wait_for(lambda: {"Sam", "Sam-2"} <= set(host.snapshot())))
            self.assertTrue(wait_for(lambda: b.assigned == "Sam-2"))
            self.assertTrue(wait_for(lambda: set(a.snapshot()) == {"host", "Sam-2"}))
            self.assertTrue(wait_for(lambda: set(b.snapshot()) == {"host", "Sam"}), f"b sees {set(b.snapshot())}")
            meta = host.snapshot(meta=True)
            self.assertTrue(meta["Sam"]["build_ok"])
            self.assertFalse(meta["Sam-2"]["build_ok"], "a different game build must be flagged")
        finally:
            a.stop(); b.stop(); host.stop()


class Bots(unittest.TestCase):
    def test_follow_bot_trails_leader(self):
        leader = LineBot(speed=4.0)
        f = FollowBot(leader, delay=0.5, side=3.0)
        t_end = time.time() + 1.5
        last = None
        while time.time() < t_end:
            p = f.position()
            last = p or last
            time.sleep(0.02)
        lead = leader.position()
        self.assertIsNotNone(last)
        self.assertAlmostEqual(lead[0] - last[0], 4.0 * 0.5, delta=0.5)  # half a second behind
        self.assertAlmostEqual(last[1], -3.0, delta=0.2)  # 3 m to the right of walking east = south

    def test_orbit_bot_keeps_radius(self):
        o = OrbitBot(LineBot(), radius=25.0)
        for _ in range(5):
            p, c = o.position(), o.around.position()
            self.assertAlmostEqual(math.hypot(p[0] - c[0], p[1] - c[1]), 25.0, delta=0.5)
            time.sleep(0.05)

    def test_replay_bot_interpolates_and_loops(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "walk.jsonl"
            f.write_text("\n".join(json.dumps([t, t * 10.0, 0, 1]) for t in (0.0, 0.5, 1.0)))
            r = ReplayBot(f)
            r.t0 = time.time() - 0.25
            self.assertAlmostEqual(r.position()[0], 2.5, delta=0.3)
            r.t0 = time.time() - 1.25  # wrapped around
            self.assertAlmostEqual(r.position()[0], 2.5, delta=0.3)


class LoadingBot:
    """Walks east, then sits on a 'loading screen' (no position, state loading)."""

    def __init__(self):
        self.build, self.state, self.loading = "bot", "ok", False

    def position(self):
        if self.loading:
            self.state = "loading"
            return None
        self.state = "ok"
        return [12.5, 34.5, 5.0]


class Loading(unittest.TestCase):
    def test_limbo_points_from_real_session(self):
        from odyssey_coop.sources import in_limbo
        self.assertTrue(in_limbo([-94.7, 33.8, -4.3]))  # loading screen, seen 3 times in-game
        self.assertTrue(in_limbo([-45.6, -17.1, 0.0]))  # main menu background character
        self.assertFalse(in_limbo([-3137.9, 4687.7, 399.2]))  # a real spot (viewpoint)
        self.assertFalse(in_limbo([-94.7, 33.8, 12.0]))  # same xy but not the loading height

    def test_loading_player_stays_put_and_greyed(self):
        port = next(_ports)
        old_timeout = net.TIMEOUT
        net.TIMEOUT = 1.5
        host = Peer("host", CircleBot(0), port=port)
        bot = LoadingBot()
        cli = Peer("loader", bot, host="127.0.0.1", port=port)
        host.start(); cli.start()
        try:
            self.assertTrue(wait_for(lambda: host.snapshot(meta=True).get("loader", {}).get("state") == "ok"))
            bot.loading = True
            self.assertTrue(wait_for(lambda: host.snapshot(meta=True).get("loader", {}).get("state") == "loading", 1.0))
            time.sleep(net.TIMEOUT + 0.5)  # a long load must not drop the player
            m = host.snapshot(meta=True).get("loader")
            self.assertIsNotNone(m)
            self.assertEqual(m["pos"], [12.5, 34.5, 5.0], "shown at the last real position")
            self.assertLess(m["age"], net.STALE)
            bot.loading = False
            self.assertTrue(wait_for(lambda: host.snapshot(meta=True).get("loader", {}).get("state") == "ok", 1.0))
        finally:
            net.TIMEOUT = old_timeout
            cli.stop(); host.stop()


class Overlay(unittest.TestCase):
    def test_smoother_eases_and_snaps(self):
        s = Smoother()
        s.update({"a": [0, 0, 0]})
        p = s.update({"a": [10, 0, 0]})["a"]
        self.assertTrue(0 < p[0] < 10, "small moves glide")
        p = s.update({"a": [10 + SNAP * 2, 0, 0]})["a"]
        self.assertEqual(p[0], 10 + SNAP * 2, "fast travel jumps straight there")
        self.assertEqual(s.update({}), {}, "players who left are forgotten")

    def test_layout_north_up_and_in_bounds(self):
        spots = layout([0, 0, 0], {"n": [0, 100, 0], "e": [100, 0, 0], "far": [5000, -5000, 40]}, size=240)
        self.assertLess(spots["n"][1], 120)  # north is up on screen
        self.assertGreater(spots["e"][0], 120)  # east is right
        for x, y, _ in spots.values():
            self.assertTrue(0 <= x <= 240 and 0 <= y <= 240)
        self.assertIn("▲40", spots["far"][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
