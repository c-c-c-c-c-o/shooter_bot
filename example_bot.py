"""A zone-aware mechfight bot focused on surviving to a good placement."""
from __future__ import annotations

import math

import mechfight as mf


def _distance(a: list[float], b: list[float]) -> float:
    return math.dist(a[:2], b[:2])


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
    name = "oval-survivor"

    def __init__(self, hello: mf.Hello):
        self.seed = int(hello["seed"])
        self.pickup_radius = hello["config"]["loot"]["pickup_radius"]
        self.weapons = hello["config"]["weapons"]

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
        urgent = not me["in_zone"] or dist > safe_radius
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

        # A nearby uncontested drop is a large survival boost. Start moving
        # toward it early enough to arrive just as it lands.
        for drop in obs["drops"]:
            distance = _distance(drop["pos"], me["pos"])
            if drop["holder"] == me["id"]:
                movement_target = drop["pos"]
                break
            if (drop["holder"] is None and distance < 150
                    and drop["lands_in"] <= distance / 11.7 + 4
                    and (me["hp"] < me["max_hp"] or me["cubes"] < 10)):
                movement_target = drop["pos"]
                break

        # Collect visible cubes and useful weapons when no immediate zone
        # correction or nearby fight takes precedence.
        if not zone_urgent and (target is None or target_distance > 80):
            cube_targets = [c for c in obs["cubes"] if _distance(c["pos"], me["pos"]) < 30]
            if cube_targets:
                movement_target = min(cube_targets, key=lambda c: _distance(c["pos"], me["pos"]))["pos"]
            elif pickups and weapon_slots[1] is None:
                candidate = min(pickups, key=lambda p: _distance(p["pos"], me["pos"]))
                if _distance(candidate["pos"], me["pos"]) < 24:
                    movement_target = candidate["pos"]

        gx, gy = movement_target[0] - x, movement_target[1] - y
        goal_distance = math.hypot(gx, gy)
        move_x, move_y = (gx / goal_distance, gy / goal_distance) if goal_distance > 1 else (0.0, 0.0)

        # Strafe unpredictably while under fire, but retain a strong vector
        # toward safety when outside the zone.
        if target and target_distance < 70:
            tx, ty = target["pos"][0] - x, target["pos"][1] - y
            length = max(math.hypot(tx, ty), 1.0)
            if target_distance < 32:
                dodge = (-tx / length, -ty / length)
            else:
                side = -1.0 if (int(obs["t"] / 2.0) + self.seed) % 2 else 1.0
                dodge = (-ty / length * side, tx / length * side)
            blend = 0.25 if zone_urgent else 0.8
            move_x, move_y = (move_x * blend + dodge[0] * (1 - blend),
                              move_y * blend + dodge[1] * (1 - blend))
            length = math.hypot(move_x, move_y)
            if length > 1e-6:
                move_x, move_y = move_x / length, move_y / length
        action["move"] = [move_x, move_y]
        action["boost"] = bool(
            (zone_urgent and goal_distance > 18 and me["energy"] > 12)
            or (target and target_distance < 45 and me["energy"] > 55)
        )
        action["jump"] = bool(me["grounded"] and target and target_distance < 65
                              and obs["tick"] % 32 == (self.seed % 32))

        aim = None
        if target:
            gun_stats = weapon_slots[0]["stats"]
            aim = _gun_intercept(target, me, gun_stats.get("speed", 252.0))
        else:
            # Scan while moving so enemies entering the sensor wedge are found.
            angle = math.atan2(move_y, move_x) if abs(move_x) + abs(move_y) > 0.1 else me["aim"][0]
            aim = [angle + 0.65 * math.sin(obs["t"] * 0.7), 0.0]
        action["aim"] = aim

        fire = [False, False]
        gun = weapon_slots[0]
        if target and gun:
            yaw_error = abs(_wrap(aim[0] - me["aim"][0]))
            pitch_error = abs(aim[1] - me["aim"][1])
            in_range = target_distance <= gun["stats"].get("range", 230.0)
            fire[0] = in_range and yaw_error < 0.16 and pitch_error < 0.13
            if gun.get("ammo", 1) <= 2 and gun.get("reload", 0) <= 0:
                action["reload"] = True

        secondary = weapon_slots[1]
        if target and secondary:
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

        mf.draw.label(f"hp {me['hp']:.0f} | alive {obs['alive']:.0f}")
        return action


mf.run(Bot)
