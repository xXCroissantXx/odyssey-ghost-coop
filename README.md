# Odyssey ghost co-op (phase 1)

Each player runs their own copy of Assassin's Creed Odyssey. This tool reads your
character's position from the running game, **read-only**, and syncs it with your
friends. Everyone sees the others on an always-on-top minimap, showing their name,
distance and height difference.

It never touches the game exe, the DRM, saves or game files. It does not inject,
patch or write memory.

Unofficial fan project, not affiliated with or endorsed by Ubisoft. You need your own
copy of the game. Tested on Steam build 17083392.

## Requirements
- Windows, Python 3.10+, `pip install -r requirements.txt`
- Odyssey in **borderless window** mode, so the overlay can sit on top of it

## 1. Find your position (once per game launch for now)
Load your save, stand on flat ground, then run:

    python -m odyssey_coop scan

Follow the prompts. Snapshot (`s`), climb up a ledge (`u`), stand still (`=`), drop
down (`d`), stand still (`=`), and repeat. Then walk (`w`), list (`l`) and `save N`
the row whose numbers move as you do. Check it with `python -m odyssey_coop watch`.

## 2. Play
    python -m odyssey_coop host --name Alec            # one player hosts (UDP 27015)
    python -m odyssey_coop join <host-ip> --name Sam   # everyone else

Over the internet, the host must forward UDP port 27015, or everyone can join the
same Tailscale/ZeroTier network and use its IP address. The overlay can be dragged;
double-click it to close. If friends appear mirrored, add `--flip-x` / `--flip-y`.

## Tests (no game needed)
    python tests/test_bots.py

A dummy process plays the part of the game for the memory scan, and three bot players
test the network sync.

## Verified in-game (2026-11-09, Steam build 17083392)
- The position is stored as three floats with **x east, y north, z up**, in metres (checked by
  walking 35 m north and 69 m east).
- The game keeps hundreds of copies of the position. Some are temporary and get zeroed
  when you move. 252 copies moved together, and the reader takes the value most of
  them agree on.
- The minimap over the game showed a bot friend correctly while walking.
- Heap addresses change every launch, but **pointer paths from the exe's static data survive
  restarts**. `python -m odyssey_coop pointers` found 1,986 paths. After a full restart, 249
  still agreed (all 27 one-jump paths among them), and they followed a 30 m walk exactly.
  These are saved in `local/paths.json`, and the mod uses them automatically at startup.
- Friends on the **same Steam build (17083392)** can copy `local/paths.json` and skip the
  scan entirely. After a game update, rerun `scan`, then `pointers`, then `check-paths --prune`
  after a restart.

## Commands
    python -m odyssey_coop scan                 # one-off: find the position by climbing/dropping
    python -m odyssey_coop pointers             # find pointer paths to it (about 25 s)
    python -m odyssey_coop check-paths --prune  # after a restart: keep the paths that still work

## Roadmap
1. **Ghost minimap** (this). Next: a pointer scan so the address survives restarts.
2. Shared world-space markers drawn over the game view (needs the camera matrix).
3. Synced quest progress and time of day, so players stay on the same beat.
4. Friends as visible characters in your world. This is the hard part: it means
   spawning and driving an NPC, which needs runtime hooks into the game.
