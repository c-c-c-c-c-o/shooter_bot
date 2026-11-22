"""A stealth-oriented bot that seeks cover and delays combat until endgame."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shooter_bot"))

import mechfight as mf


def _polygon(ring: list[float]) -> list[tuple[float, float]]:
    return [(ring[i], ring[i + 1]) for i in range(0, len(ring) - 1, 2)]


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
) -> bool:
    def orientation(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p, q, r):
        return (min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6
                and min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6)

    o1, o2 = orientation(a, b, c), orientation(a, b, d)
    o3, o4 = orientation(c, d, a), orientation(c, d, b)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    return (
        (abs(o1) <= 1e-6 and on_segment(a, c, b))
        or (abs(o2) <= 1e-6 and on_segment(a, d, b))
        or (abs(o3) <= 1e-6 and on_segment(c, a, d))
        or (abs(o4) <= 1e-6 and on_segment(c, b, d))
    )


def _segment_crosses_polygon(
    start: tuple[float, float], end: tuple[float, float],
    polygon: list[tuple[float, float]],
) -> bool:
    if _point_in_polygon(start, polygon) or _point_in_polygon(end, polygon):
        return True
    return any(
        _segments_intersect(start, end, polygon[i], polygon[(i + 1) % len(polygon)])
        for i in range(len(polygon))
    )


def _distance(a: list[float], b: list[float]) -> float:
    return math.dist(a[:2], b[:2])


def _unit(x: float, y: float) -> tuple[float, float]:
    length = math.hypot(x, y)
    if length <= 1e-9:
        return 0.0, 0.0
    return x / length, y / length


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def _aim_at(point: list[float], origin: list[float]) -> list[float]:
    dx, dy, dz = point[0] - origin[0], point[1] - origin[1], point[2] - origin[2]
    return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]


def _gun_intercept(target: dict, me: dict, speed: float) -> list[float]:
    pos, vel = target["pos"], target["vel"]
    rx, ry = pos[0] - me["pos"][0], pos[1] - me["pos"][1]
    vx, vy = vel[0], vel[1]
    a = vx * vx + vy * vy - speed * speed
    b = 2 * (rx * vx + ry * vy)
    c = rx * rx + ry * ry
    disc = b * b - 4 * a * c
    times = []
    if abs(a) < 1e-6:
        if abs(b) > 1e-6:
            times = [-c / b]
    elif disc >= 0:
        root = math.sqrt(disc)
        times = [(-b - root) / (2 * a), (-b + root) / (2 * a)]
    positive = [t for t in times if t > 0]
    flight = min(positive) if positive else math.sqrt(c) / max(speed, 1)
    muzzle = me["pos"][2] + me["stats"].get("muzzle_height", 7)
    return _aim_at(
        [pos[0] + vx * flight, pos[1] + vy * flight,
         pos[2] + 4.5 + vel[2] * flight],
        [me["pos"][0], me["pos"][1], muzzle],
    )


def _mortar_aim(target: dict, me: dict, speed: float, gravity: float) -> list[float] | None:
    pos, vel = target["pos"], target["vel"]
    muzzle_z = me["pos"][2] + me["stats"].get("muzzle_height", 7)
    flight = 0.0
    pitch = 0.0
    predicted = pos
    for _ in range(3):
        predicted = [pos[i] + vel[i] * flight for i in range(3)]
        dx, dy = predicted[0] - me["pos"][0], predicted[1] - me["pos"][1]
        horizontal = math.hypot(dx, dy)
        if horizontal < 1:
            return None
        height = predicted[2] + 4.5 - muzzle_z
        disc = speed ** 4 - gravity * (gravity * horizontal ** 2 + 2 * height * speed ** 2)
        if disc < 0:
            return None
        tangent = (speed ** 2 - math.sqrt(disc)) / (gravity * horizontal)
        pitch = math.atan(tangent)
        flight = horizontal / max(speed * math.cos(pitch), 1)
    return [math.atan2(predicted[1] - me["pos"][1], predicted[0] - me["pos"][0]), pitch]


def _weapon_score(kind: str, enemies: list[dict], distance: float) -> float:
    if kind == "laser":
        score = 4.0
        if any("mortar" in enemy.get("weapons", []) for enemy in enemies):
            score += 1.0
        return score
    if kind == "mortar":
        score = 3.8
        if any("lightning" in enemy.get("weapons", []) for enemy in enemies):
            score += 1.0
        return score
    if kind == "lightning":
        return 4.5 if distance < 45 and any(
            "laser" in enemy.get("weapons", []) for enemy in enemies
        ) else 2.8
    return 0.0


class Bot:
    name = "avoid-bot"

    def __init__(self, hello: mf.Hello):
        self.seed = int(hello["seed"])
        self.pickup_radius = hello["config"]["loot"]["pickup_radius"]
        self.weapons = hello["config"]["weapons"]
        world_path = Path(hello["map"]["dir"]) / "v2" / "world.json"
        with world_path.open(encoding="utf-8") as world_file:
            world = json.load(world_file)
        self.cover_spots = self._make_cover_spots(world["prisms"])

    @staticmethod
    def _make_cover_spots(prisms: list[dict]) -> list[dict]:
        spots = []
        for prism in prisms:
            if prism["kind"] not in ("building", "stadium") or prism["top"] - prism["base"] < 7:
                continue
            polygon = _polygon(prism["ring"])
            if len(polygon) < 3:
                continue
            area = sum(
                polygon[i][0] * polygon[(i + 1) % len(polygon)][1]
                - polygon[(i + 1) % len(polygon)][0] * polygon[i][1]
                for i in range(len(polygon))
            )
            if abs(area) < 1e-6:
                continue
            stride = max(1, len(polygon) // 10)
            for index in range(0, len(polygon), stride):
                a = polygon[index]
                b = polygon[(index + 1) % len(polygon)]
                dx, dy = b[0] - a[0], b[1] - a[1]
                edge_length = math.hypot(dx, dy)
                if edge_length < 1:
                    continue
                if area > 0:
                    outward = (dy / edge_length, -dx / edge_length)
                else:
                    outward = (-dy / edge_length, dx / edge_length)
                x = (a[0] + b[0]) * 0.5 + outward[0] * 6.0
                y = (a[1] + b[1]) * 0.5 + outward[1] * 6.0
                if _point_in_polygon((x, y), polygon):
                    continue
                if any(_point_in_polygon((x, y), _polygon(hole)) for hole in prism.get("holes", [])):
                    continue
                spots.append({
                    "pos": [x, y],
                    "polygon": polygon,
                    "base": prism["base"],
                    "top": prism["top"],
                })
        return spots

    def _threats(self, obs: mf.Observation, me: dict) -> list[tuple[list[float], float]]:
        threats = []
        for actor in obs["mechs"]:
            if actor["id"] == me["id"]:
                continue
            weight = 1.0 if actor["kind"] == "mech" else 0.45
            distance = _distance(actor["pos"], me["pos"])
            threats.append((actor["pos"], weight * (1.5 if distance < 55 else 1.0)))
        for sound in obs["sounds"]:
            if sound["age"] > 1.5:
                continue
            estimated_distance = {"near": 35.0, "mid": 105.0, "far": 210.0}.get(sound["dist"], 100.0)
            bearing = sound["bearing"]
            threats.append((
                [
                    me["pos"][0] + math.cos(bearing) * estimated_distance,
                    me["pos"][1] + math.sin(bearing) * estimated_distance,
                    me["pos"][2],
                ],
                0.3,
            ))
        return threats

    def _cover_goal(
        self, obs: mf.Observation, me: dict, threats: list[tuple[list[float], float]]
    ) -> list[float] | None:
        zone = obs["zone"]
        if obs["t"] >= zone["shrink_start"] - 15:
            center, radius = zone["next_center"], zone["next_radius"]
        else:
            center, radius = zone["center"], zone["radius"]
        safe_radius = max(0.0, radius - 36.0)
        px, py = me["pos"][:2]
        candidates = []
        for spot in self.cover_spots:
            goal = spot["pos"]
            travel = math.hypot(goal[0] - px, goal[1] - py)
            if travel > 170:
                continue
            if math.hypot(goal[0] - center[0], goal[1] - center[1]) > safe_radius:
                continue
            start = (px, py)
            end = (goal[0], goal[1])
            if travel > 8 and _segment_crosses_polygon(start, end, spot["polygon"]):
                continue

            covered_weight = 0.0
            closest_threat = math.inf
            for enemy_pos, weight in threats:
                distance = math.hypot(goal[0] - enemy_pos[0], goal[1] - enemy_pos[1])
                closest_threat = min(closest_threat, distance)
                eye_height = max(me["pos"][2], enemy_pos[2]) + 5.0
                if spot["base"] <= eye_height <= spot["top"] and _segment_crosses_polygon(
                    (enemy_pos[0], enemy_pos[1]), end, spot["polygon"]
                ):
                    covered_weight += weight
            if not threats:
                closest_threat = 170.0
            score = (
                48.0 * covered_weight
                + min(closest_threat, 170.0) * 0.12
                - travel * 0.42
            )
            candidates.append((score, goal))
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[0])[1]

    def _zone_goal(self, obs: mf.Observation, me: dict) -> tuple[list[float], bool]:
        zone = obs["zone"]
        x, y = me["pos"][:2]
        current = zone["center"]
        next_center = zone["next_center"]
        shrinking_soon = obs["t"] >= zone["shrink_start"] - 12
        center = next_center if shrinking_soon else current
        radius = zone["next_radius"] if shrinking_soon else zone["radius"]
        dx, dy = x - center[0], y - center[1]
        dist = math.hypot(dx, dy)
        safe_radius = max(0.0, radius - 30.0)
        urgent = dist > safe_radius
        if urgent:
            if dist <= safe_radius or dist < 1e-6:
                return [x, y], False
            return [center[0] + dx * safe_radius / dist,
                    center[1] + dy * safe_radius / dist], True

        # Keep moving within the safe interior instead of standing still.
        phase = int(obs["t"] / 4.0)
        angle = (self.seed % 6283) / 1000.0 + phase * 2.399963229728653
        orbit_radius = min(45.0, max(18.0, radius * 0.12))
        return [current[0] + math.cos(angle) * orbit_radius,
                current[1] + math.sin(angle) * orbit_radius], False

    def _choose_target(self, enemies: list[dict], me: dict) -> dict | None:
        if not enemies:
            return None
        return min(
            enemies,
            key=lambda enemy: _distance(enemy["pos"], me["pos"])
            - max(0.0, 180.0 - enemy.get("hp", 180.0)) * 0.08,
        )

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        action: mf.Action = {"tick": obs["tick"]}
        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        target = self._choose_target(enemies, me)
        target_distance = _distance(target["pos"], me["pos"]) if target else math.inf
        endgame = obs["alive"] <= 2
        threats = self._threats(obs, me)

        if obs["offer"]:
            priorities = {
                "armor": 110, "plating": 100, "nanites": 88, "servos": 82,
                "sensors": 78, "gun_rate": 76, "gun_damage": 74,
                "laser_damage": 90, "laser_charge": 88, "mortar_reload": 72,
                "lightning_range": 70, "lightning_efficiency": 72,
                "jets": 68, "mortar_splash": 64, "mortar_velocity": 61,
                "gun_velocity": 66, "gun_mag": 58, "capacitor": 52,
                "reactor": 48, "gyros": 48, "laser_pierce": 55,
                "lightning_chain": 67,
            }
            choices = obs["offer"]["choices"]
            action["pick"] = max(
                range(len(choices)),
                key=lambda i: priorities.get(choices[i]["id"], 0),
            )

        weapon_slots = me["weapons"]
        held = {w["kind"] for w in weapon_slots if w is not None}
        pickups = [
            p for p in obs["pickups"]
            if p["kind"] != "gun" and p["kind"] not in held
        ]
        if pickups:
            nearest_pickup = min(pickups, key=lambda p: _distance(p["pos"], me["pos"]))
            pickup_dist = _distance(nearest_pickup["pos"], me["pos"])
            if pickup_dist <= self.pickup_radius and abs(nearest_pickup["pos"][2] - z) <= 4:
                if weapon_slots[1] is None:
                    action["take"] = nearest_pickup["id"]
                else:
                    current_score = _weapon_score(weapon_slots[1]["kind"], enemies, target_distance)
                    new_score = _weapon_score(nearest_pickup["kind"], enemies, target_distance)
                    if new_score > current_score + 0.5:
                        action["take"] = nearest_pickup["id"]
                        action["drop"] = 1

        goal, zone_urgent = self._zone_goal(obs, me)
        movement_target = goal

        nearest_enemy = min(
            (_distance(enemy["pos"], me["pos"]) for enemy in enemies),
            default=math.inf,
        )
        loot_goal = None
        if not zone_urgent and nearest_enemy > 50:
            nearby_cubes = [
                cube for cube in obs["cubes"]
                if _distance(cube["pos"], me["pos"]) < 9
                and abs(cube["pos"][2] - z) <= 4
            ]
            if nearby_cubes:
                loot_goal = min(
                    nearby_cubes,
                    key=lambda cube: _distance(cube["pos"], me["pos"]),
                )["pos"]
            elif pickups and weapon_slots[1] is None:
                pickup = min(
                    pickups,
                    key=lambda item: _distance(item["pos"], me["pos"]),
                )
                if _distance(pickup["pos"], me["pos"]) < 8:
                    loot_goal = pickup["pos"]

        # Only stay at a supply drop already being captured; never cross open
        # ground to contest one while the bot is in hiding mode.
        for drop in obs["drops"] if not zone_urgent else []:
            if drop["holder"] == me["id"] and _distance(drop["pos"], me["pos"]) < 10:
                movement_target = drop["pos"]
                break

        gx, gy = movement_target[0] - x, movement_target[1] - y
        goal_distance = math.hypot(gx, gy)
        move_x, move_y = (gx / goal_distance, gy / goal_distance) if goal_distance > 1 else (0.0, 0.0)

        if zone_urgent:
            boost = goal_distance > 18 and me["energy"] > 12
            jump = bool(
                    me["grounded"] and obs["tick"] % 24 == self.seed % 24
            )
        elif not endgame:
            cover_goal = self._cover_goal(obs, me, threats)
            if loot_goal and (cover_goal is None or _distance(loot_goal, me["pos"]) < 6):
                    cover_goal = loot_goal
            if cover_goal:
                    dx, dy = cover_goal[0] - x, cover_goal[1] - y
                    length = math.hypot(dx, dy)
                    move_x, move_y = (dx / length, dy / length) if length > 6 else (0.0, 0.0)
            elif threats:
                    away_x = away_y = 0.0
                    for position, weight in threats:
                        dx, dy = x - position[0], y - position[1]
                        distance_sq = max(dx * dx + dy * dy, 25.0)
                        away_x += dx * weight / distance_sq
                        away_y += dy * weight / distance_sq
                    away_x, away_y = _unit(away_x, away_y)
                    move_x, move_y = away_x, away_y
            boost = False
            jump = False
        else:
            if loot_goal:
                    movement_target = loot_goal
                    gx, gy = movement_target[0] - x, movement_target[1] - y
                    goal_distance = math.hypot(gx, gy)
                    move_x, move_y = (
                        (gx / goal_distance, gy / goal_distance)
                        if goal_distance > 1 else (0.0, 0.0)
                    )
            if target and target_distance < 70:
                    tx, ty = target["pos"][0] - x, target["pos"][1] - y
                    length = max(math.hypot(tx, ty), 1.0)
                    if target_distance < 32:
                        dodge = (-tx / length, -ty / length)
                    else:
                        side = -1.0 if (int(obs["t"] / 2.0) + self.seed) % 2 else 1.0
                        dodge = (-ty / length * side, tx / length * side)
                    move_x, move_y = (move_x * 0.3 + dodge[0] * 0.7,
                                      move_y * 0.3 + dodge[1] * 0.7)
                    move_x, move_y = _unit(move_x, move_y)
            boost = bool(target and target_distance < 45 and me["energy"] > 55)
            jump = bool(me["grounded"] and target and target_distance < 65
                            and obs["tick"] % 32 == (self.seed % 32))

        action["move"] = [move_x, move_y]
        action["boost"] = boost
        action["jump"] = jump

        aim = None
        if endgame and target:
            gun_stats = weapon_slots[0]["stats"]
            aim = _gun_intercept(target, me, gun_stats.get("speed", 252.0))
        else:
            if threats:
                    closest_threat = min(threats, key=lambda item: _distance(item[0], me["pos"]))[0]
                    scan_center = math.atan2(y - closest_threat[1], x - closest_threat[0])
            else:
                    scan_center = me["aim"][0]
            angle = scan_center + 0.8 * math.sin(obs["t"] * 0.7 + self.seed)
            aim = [angle, 0.0]
        action["aim"] = aim

        fire = [False, False]
        gun = weapon_slots[0]
        if endgame and target and gun:
            yaw_error = abs(_wrap(aim[0] - me["aim"][0]))
            pitch_error = abs(aim[1] - me["aim"][1])
            in_range = target_distance <= gun["stats"].get("range", 230.0)
            fire[0] = in_range and yaw_error < 0.16 and pitch_error < 0.13
            if gun.get("ammo", 1) <= 2 and gun.get("reload", 0) <= 0:
                    action["reload"] = True

        secondary = weapon_slots[1]
        if endgame and target and secondary:
            kind = secondary["kind"]
            stats = secondary["stats"]
            secondary_aim = aim
            mortar_reachable = False
            if kind == "mortar":
                    if target_distance < 35:
                        secondary_aim = aim
                    else:
                        secondary_aim = _mortar_aim(
                            target, me, stats.get("speed", 72.0),
                            self.weapons["mortar"].get("gravity", 22.0),
                        )
                        mortar_reachable = secondary_aim is not None
                        if not mortar_reachable:
                            secondary_aim = aim
            action["aim"] = secondary_aim
            yaw_error = abs(_wrap(secondary_aim[0] - me["aim"][0]))
            pitch_error = abs(secondary_aim[1] - me["aim"][1])
            aligned = yaw_error < 0.12 and pitch_error < 0.12

            if kind == "laser":
                    charge = secondary.get("charge", 0.0)
                    full_charge = stats.get("charge", 0.75)
                    fire[1] = not (charge >= full_charge - 0.03 and aligned)
            elif kind == "mortar":
                    fire[1] = bool(
                        mortar_reachable
                        and yaw_error < 0.16 and pitch_error < 0.15
                    )
            elif kind == "lightning":
                    fire[1] = bool(
                        target_distance <= stats.get("range", 45.0)
                        and yaw_error < math.radians(stats.get("cone_deg", 30.0) * 0.4)
                    )
            if kind == "mortar" and target_distance >= 35:
                    fire[0] = False
        action["fire"] = fire

        mf.draw.label(
            f"{'fight' if endgame else 'hide'} | HP {me['hp']:.0f} | alive {obs['alive']:.0f}"
        )
        return action


mf.run(Bot)
