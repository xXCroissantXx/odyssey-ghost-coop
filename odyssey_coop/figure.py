"""An original jointed figure for remote players, posed in world space.

Each friend gets an Animator that turns their stream of positions into a state
(idle, walk, run, climb, fall, ride) and a facing, and poses a simple 3D skeleton
at their position. The overlay projects every joint through the game camera, so the
figure has real perspective and turns with the camera.
"""
import math

# skeleton in metres, z up, facing +y (local frame), feet at the origin
HIP_H, CHEST_H, NECK_H, HEAD_H = 0.95, 1.40, 1.52, 1.68
HIP_W, SHOULDER_W = 0.11, 0.21
THIGH, SHIN, UPPER_ARM, FOREARM = 0.46, 0.46, 0.30, 0.28
HEAD_R = 0.12

BONES = [("pelvis", "chest"), ("chest", "neck"),
         ("l_hip", "l_knee"), ("l_knee", "l_foot"), ("r_hip", "r_knee"), ("r_knee", "r_foot"),
         ("l_shoulder", "l_elbow"), ("l_elbow", "l_hand"), ("r_shoulder", "r_elbow"), ("r_elbow", "r_hand"),
         ("l_hip", "r_hip"), ("l_shoulder", "r_shoulder")]

# movement thresholds (m/s)
WALK, RUN, RIDE = 0.4, 4.0, 9.0
CLIMB_VZ, FALL_VZ = 0.6, -2.5


def _limb(origin, length, swing, fwd_sign=1.0):
    """End point of a limb hanging down from `origin`, swung forward by `swing` radians."""
    ox, oy, oz = origin
    return (ox, oy + fwd_sign * length * math.sin(swing), oz - length * math.cos(swing))


class Animator:
    def __init__(self):
        self.pos = None
        self.t = None
        self.vel = (0.0, 0.0, 0.0)
        self.facing = 0.0  # radians, 0 = north (+y), clockwise toward east
        self.phase = 0.0
        self.state = "idle"

    def update(self, pos, t):
        if self.pos is not None and t > self.t:
            dt = t - self.t
            raw = tuple((pos[k] - self.pos[k]) / dt for k in range(3))
            if math.dist(pos, self.pos) > 60:  # fast travel: no animation across the jump
                raw = (0.0, 0.0, 0.0)
            a = min(1.0, dt * 6)  # smooth velocity over ~1/6 s
            self.vel = tuple(self.vel[k] + (raw[k] - self.vel[k]) * a for k in range(3))
            speed = math.hypot(self.vel[0], self.vel[1])
            if speed > 0.3:
                target = math.atan2(self.vel[0], self.vel[1])
                d = (target - self.facing + math.pi) % (2 * math.pi) - math.pi
                self.facing += d * min(1.0, dt * 8)
            vz = self.vel[2]
            if vz < FALL_VZ:
                self.state = "fall"
            elif vz > CLIMB_VZ and speed < 1.5:
                self.state = "climb"
            elif speed > RIDE:
                self.state = "ride"
            elif speed > RUN:
                self.state = "run"
            elif speed > WALK:
                self.state = "walk"
            else:
                self.state = "idle"
            stride = {"walk": 1.4, "run": 2.6, "climb": 0.5}.get(self.state, 1.4)
            travel = math.hypot(speed, vz) * dt if self.state == "climb" else speed * dt
            self.phase = (self.phase + 2 * math.pi * travel / stride) % (2 * math.pi)
            if self.state == "idle":
                self.phase = (self.phase + dt * 1.5) % (2 * math.pi)  # breathing
        self.pos, self.t = list(pos), t

    def joints(self):
        """World-space joint positions for the current pose."""
        s, p = self.state, self.phase
        lean, leg, knee, arm, elbow, arms_up, bob = 0.0, 0.0, 0.0, 0.0, 0.15, 0.0, 0.0
        if s == "walk":
            leg, knee, arm, bob = 0.45 * math.sin(p), 0.35, 0.35 * math.sin(p), 0.03 * abs(math.cos(p))
        elif s == "run":
            leg, knee, arm, elbow, lean, bob = 0.8 * math.sin(p), 0.9, 0.7 * math.sin(p), 1.2, 0.25, 0.06 * abs(math.cos(p))
        elif s == "climb":
            leg, knee, arms_up = 0.5 * math.sin(p), 0.8, 1.0
        elif s == "fall":
            leg, knee, arms_up = 0.3, 0.4, 0.6
        elif s == "ride":
            leg, knee, arm, lean = 0.9, 1.4, 0.6, 0.15
        else:  # idle breathing
            bob = 0.01 * math.sin(p)
        L = {}
        L["pelvis"] = (0.0, 0.0, HIP_H + bob)
        L["chest"] = (0.0, (CHEST_H - HIP_H) * math.sin(lean), HIP_H + bob + (CHEST_H - HIP_H) * math.cos(lean))
        L["neck"] = (0.0, (NECK_H - HIP_H) * math.sin(lean), HIP_H + bob + (NECK_H - HIP_H) * math.cos(lean))
        L["head"] = (0.0, (HEAD_H - HIP_H) * math.sin(lean), HIP_H + bob + (HEAD_H - HIP_H) * math.cos(lean))
        for side, sign in (("l", -1.0), ("r", 1.0)):
            ph = 1.0 if side == "l" else -1.0  # legs and arms alternate
            hip = (sign * HIP_W, 0.0, HIP_H + bob)
            k = _limb(hip, THIGH, leg * ph if s != "ride" else leg)
            bend = knee * max(0.0, ph * math.sin(p)) if s in ("walk", "run", "climb") else knee
            f = _limb(k, SHIN, (leg * ph if s != "ride" else leg) - bend)
            L[f"{side}_hip"], L[f"{side}_knee"], L[f"{side}_foot"] = hip, k, f
            sh = (sign * SHOULDER_W, L["chest"][1], L["chest"][2])
            if arms_up:
                reach = arms_up * (0.8 + 0.2 * ph * math.sin(p)) * math.pi * 0.9
                e = _limb(sh, UPPER_ARM, reach)
                h = _limb(e, FOREARM, reach * 1.05)
            else:
                e = _limb(sh, UPPER_ARM, -arm * ph)
                h = _limb(e, FOREARM, -arm * ph + elbow)
            L[f"{side}_shoulder"], L[f"{side}_elbow"], L[f"{side}_hand"] = sh, e, h
        # rotate local (+y forward) into the world by facing, then move to the player
        c, sn = math.cos(self.facing), math.sin(self.facing)
        x0, y0, z0 = self.pos
        return {name: (x0 + lx * c + ly * sn, y0 - lx * sn + ly * c, z0 + lz) for name, (lx, ly, lz) in L.items()}
