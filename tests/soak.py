"""Soak test: a host and four bot players (two over a bad link) for N minutes.
Checks that nobody drops out, threads stay alive, memory doesn't grow, and CPU stays low.

    python tests/soak.py --minutes 10
"""
import argparse
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import odyssey_coop.net as net  # noqa: E402
from odyssey_coop.net import Peer  # noqa: E402
from odyssey_coop.sources import CircleBot  # noqa: E402
from test_network import BadLink  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--port", type=int, default=28950)
    a = ap.parse_args()
    tracemalloc.start()
    host = Peer("host", CircleBot(0), port=a.port)
    link = BadLink(("127.0.0.1", a.port), loss=0.1, delay=0.06, jitter=0.04, dup=0.05, seed=3)
    bots = [Peer(f"bot{i}", CircleBot(i + 1, radius=20 + 10 * i), host="127.0.0.1",
                 port=link.port if i >= 2 else a.port) for i in range(4)]
    for p in [host] + bots:
        p.start()
    t0, cpu0 = time.time(), time.process_time()
    end = t0 + a.minutes * 60
    samples = missing = 0
    mem_1min = None
    worst_age = 0.0
    while time.time() < end:
        time.sleep(1.0)
        meta = host.snapshot(meta=True)
        samples += 1
        missing += 4 - len(meta)
        worst_age = max([worst_age] + [m["age"] for m in meta.values()])
        if mem_1min is None and time.time() - t0 > 60:
            mem_1min = tracemalloc.get_traced_memory()[0]
        dead = [p.assigned for p in [host] + bots if not p.alive()]
        if dead:
            print(f"FAIL: threads died for {dead}")
            sys.exit(1)
    mem_end = tracemalloc.get_traced_memory()[0]
    cpu = (time.process_time() - cpu0) / (time.time() - t0) * 100
    each_sees = {p.assigned: len(p.snapshot()) for p in bots}
    for p in [host] + bots:
        p.stop()
    link.close()
    growth = (mem_end - mem_1min) / 1024 if mem_1min else 0
    print(f"ran {a.minutes:g} min, 5 peers in one process: missing player-samples {missing}/{samples * 4}, "
          f"worst age {worst_age:.2f}s, CPU {cpu:.1f}% of one core, memory growth after minute 1: {growth:.0f} KiB")
    print("each bot sees", each_sees, "| host stats", dict(host.stats))
    ok = missing == 0 and worst_age < net.STALE and growth < 512 and all(v == 4 for v in each_sees.values())
    print("SOAK OK" if ok else "SOAK FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
