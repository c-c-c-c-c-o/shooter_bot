#!/usr/bin/env python3
"""Richer rule-based bot for the local flat 2D duel simulator."""
from __future__ import annotations

import math
import sys
from pathlib import Path

bot_root = Path(__file__).resolve().parent
if not (bot_root / "mechfight.py").is_file():
    bot_root = bot_root.parent / "shooter_bot"
sys.path.insert(0, str(bot_root))

import mechfight as mf


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def _distance(a: list[float], b: list[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _unit(x: float, y: float) -> tuple[float, float]:
    length = math.hypot(x, y)
    if length < 1e-8:
        return 0.0, 0.0
    return x / length, y / length


def _intercept_yaw(me: dict, target: dict, bullet_speed: float) -> float:
    rx = target["pos"][0] - me["pos"][0]
    ry = target["pos"][1] - me["pos"][1]
    vx, vy = target["vel"][0], target["vel"][1]
    a = vx * vx + vy * vy - bullet_speed * bullet_speed
    b = 2 * (rx * vx + ry * vy)
    c = rx * rx + ry * ry
    times = []
    if abs(a) < 1e-8:
        if abs(b) > 1e-8:
            times.append(-c / b)
    else:
        discriminant = b * b - 4 * a * c
        if discriminant >= 0:
            root = math.sqrt(discriminant)
            times.extend(((-b - root) / (2 * a), (-b + root) / (2 * a)))
    flight = min((t for t in times if t > 0), default=math.sqrt(c) / bullet_speed)
    future_x = target["pos"][0] + vx * flight
    future_y = target["pos"][1] + vy * flight
    return math.atan2(future_y - me["pos"][1], future_x - me["pos"][0])


class Bot:
    name = "rich-heuristic"

    def __init__(self, hello: mf.Hello):
        self.seed = int(hello["seed"])
        self.config = hello["config"]
        self.last_attacker: int | None = None
        self.last_attacked_at = -math.inf

    def _select_target(self, obs: mf.Observation, me: dict) -> dict | None:
        enemies = [actor for actor in obs["mechs"] if actor["kind"] == "mech"]
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("by") is not None:
                self.last_attacker = int(event["by"])
                self.last_attacked_at = obs["t"]
        if not enemies:
            return None

        def cost(enemy: dict) -> float:
            distance = _distance(me["pos"], enemy["pos"])
            hp = enemy.get("hp", enemy.get("max_hp", 700.0))
            cost_value = distance - max(0.0, 500.0 - hp) * 0.04
            if enemy["id"] == self.last_attacker and obs["t"] - self.last_attacked_at < 5:
                cost_value -= 35.0
            # A nearby opponent is an immediate threat; do not fixate on a
            # distant, weak target while someone is already pressuring us.
            if distance < 45:
                cost_value -= (45 - distance) * 0.5
            return cost_value

        return min(enemies, key=cost)

    def _preferred_direction(
        self, obs: mf.Observation, me: dict, target: dict | None
    ) -> tuple[float, float]:
        px, py = me["pos"][:2]
        zone = obs["zone"]
        cx, cy = zone["center"]
        dx, dy = px - cx, py - cy
        radius = max(0.0, zone["radius"])
        safe_radius = max(0.0, radius - 16.0)
        radial_distance = math.hypot(dx, dy)

        if radial_distance > safe_radius - 8:
            return _unit(cx - px, cy - py)

        if target:
            tx, ty = target["pos"][0] - px, target["pos"][1] - py
            distance = max(math.hypot(tx, ty), 1.0)
            toward_x, toward_y = tx / distance, ty / distance
            desired_range = 82.0
            radial_weight = max(-1.0, min(0.65, (distance - desired_range) / 55.0))
            phase = int(obs["t"] / 2.5)
            side = -1.0 if (phase + self.seed) % 2 else 1.0
            if distance < 38:
                radial_weight = -1.0
            orbit_weight = 0.9 if distance >= 38 else 0.35
            return _unit(
                radial_weight * toward_x - side * orbit_weight * toward_y,
                radial_weight * toward_y + side * orbit_weight * toward_x,
            )

        # Sweep around a moving waypoint inside the zone while scanning for
        # opponents. This avoids idling at the center when no one is visible.
        phase = (self.seed % 997) * 0.013 + obs["t"] * 0.22
        waypoint_radius = min(90.0, max(20.0, safe_radius * 0.42))
        goal_x = cx + math.cos(phase) * waypoint_radius
        goal_y = cy + math.sin(phase) * waypoint_radius
        return _unit(goal_x - px, goal_y - py)

    def _predict_path(self, me: dict, direction: tuple[float, float]) -> list[tuple[float, float]]:
        stats = me["stats"]
        dt = 0.1
        vx, vy = me["vel"][:2]
        x, y = me["pos"][:2]
        speed = stats.get("walk_speed", 11.7)
        accel = stats.get("ground_accel", 58.5)
        path = []
        for _ in range(8):
            desired_x, desired_y = direction[0] * speed, direction[1] * speed
            ax, ay = desired_x - vx, desired_y - vy
            acceleration = math.hypot(ax, ay)
            max_delta = accel * dt
            if acceleration > max_delta:
                ax, ay = ax * max_delta / acceleration, ay * max_delta / acceleration
            vx += ax
            vy += ay
            x += vx * dt
            y += vy * dt
            path.append((x, y))
        return path

    def _projectile_risk(
        self, obs: mf.Observation, me: dict, path: list[tuple[float, float]]
    ) -> float:
        risk = 0.0
        for projectile in obs["projectiles"]:
            px, py = projectile["pos"][:2]
            vx, vy = projectile["vel"][:2]
            for index, (mx, my) in enumerate(path):
                time = (index + 1) * 0.1
                distance = math.hypot(px + vx * time - mx, py + vy * time - my)
                if distance < 11.0:
                    risk += (11.0 - distance) ** 2 * 0.45
        return risk

    def _aimline_risk(
        self, target: dict | None, me: dict, path: list[tuple[float, float]]
    ) -> float:
        if target is None:
            return 0.0
        ex, ey = target["pos"][:2]
        yaw = target["aim"][0]
        ux, uy = math.cos(yaw), math.sin(yaw)
        distance = _distance(me["pos"], target["pos"])
        direct_bearing = math.atan2(me["pos"][1] - ey, me["pos"][0] - ex)
        if abs(_wrap(direct_bearing - yaw)) > 0.42 or distance > 230:
            return 0.0

        risk = 0.0
        for index, (mx, my) in enumerate(path):
            rx, ry = mx - ex, my - ey
            along = rx * ux + ry * uy
            if along <= 0 or along > 230:
                continue
            cross_track = abs(rx * uy - ry * ux)
            if cross_track < 9:
                risk += (9 - cross_track) ** 2 * 0.22
        return risk

    def _score_direction(
        self, direction: tuple[float, float], preferred: tuple[float, float],
        obs: mf.Observation, me: dict, target: dict | None,
    ) -> float:
        path = self._predict_path(me, direction)
        score = 7.0 * (direction[0] * preferred[0] + direction[1] * preferred[1])
        zone = obs["zone"]
        safe_radius = max(0.0, zone["radius"] - 16.0)
        for index, (x, y) in enumerate(path):
            distance_from_center = math.hypot(x - zone["center"][0], y - zone["center"][1])
            outside = max(0.0, distance_from_center - safe_radius)
            score -= outside * outside * (0.08 + index * 0.005)

        if target:
            target_future = (
                target["pos"][0] + target["vel"][0] * 0.8,
                target["pos"][1] + target["vel"][1] * 0.8,
            )
            future_range = math.hypot(path[-1][0] - target_future[0],
                                      path[-1][1] - target_future[1])
            score -= abs(future_range - 82.0) * 0.035
            if future_range < 38:
                score -= (38 - future_range) * 0.8

        score -= self._projectile_risk(obs, me, path)
        score -= self._aimline_risk(target, me, path)
        return score

    def _movement(self, obs: mf.Observation, me: dict, target: dict | None) -> tuple[list[float], bool]:
        preferred = self._preferred_direction(obs, me, target)
        directions = [(0.0, 0.0)]
        directions.extend(
            (math.cos(index * math.tau / 24), math.sin(index * math.tau / 24))
            for index in range(24)
        )
        directions.append(preferred)
        best = max(
            directions,
            key=lambda direction: self._score_direction(
                direction, preferred, obs, me, target
            ),
        )

        zone = obs["zone"]
        zone_distance = math.hypot(
            me["pos"][0] - zone["center"][0],
            me["pos"][1] - zone["center"][1],
        )
        outside = zone_distance > max(0.0, zone["radius"] - 16.0)
        close_threat = any(
            math.hypot(p["pos"][0] - me["pos"][0], p["pos"][1] - me["pos"][1]) < 22
            for p in obs["projectiles"]
        )
        boost = me["energy"] > 15 and (
            (outside and zone_distance > zone["radius"] + 3)
            or (close_threat and me["energy"] > 55)
        )
        return [best[0], best[1]], boost

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        target = self._select_target(obs, me)
        move, boost = self._movement(obs, me, target)

        if target:
            weapon = me["weapons"][0]
            stats = weapon["stats"] if weapon else self.config["weapons"]["gun"]
            yaw = _intercept_yaw(me, target, stats.get("speed", 252.0))
            aim = [yaw, 0.0]
        else:
            # Rotate the sensor wedge while the movement policy patrols.
            yaw = (self.seed % 6283) / 1000.0 + obs["t"] * 1.35
            aim = [_wrap(yaw), 0.0]

        action: mf.Action = {
            "tick": obs["tick"],
            "move": move,
            "boost": boost,
            "aim": aim,
            "fire": [False, False],
        }

        gun = me["weapons"][0]
        if gun and target:
            yaw_error = abs(_wrap(aim[0] - me["aim"][0]))
            distance = _distance(me["pos"], target["pos"])
            action["fire"] = [
                distance <= gun["stats"].get("range", 230.0)
                and yaw_error < 0.14,
                False,
            ]
            if (gun.get("ammo", 0) <= 2 and gun.get("reload", 0) <= 0
                    and gun.get("ammo", 0) < gun["stats"].get("mag", 12)):
                action["reload"] = True

        mf.draw.label(f"HP {me['hp']:.0f} | threats {len(obs['projectiles'])}")
        return action


mf.run(Bot)
