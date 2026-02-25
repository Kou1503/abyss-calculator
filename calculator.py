import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------- load ----------
def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- parsing helpers ----------
def parse_seconds(s: str) -> float:
    s = str(s).strip().lower()
    # "2s", "1.9s"
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s$", s)
    if not m:
        raise ValueError(f"Bad seconds value: {s}")
    return float(m.group(1))

def pick_roll(vmin: float, vmax: float, mode: str) -> float:
    if mode == "min":
        return vmin
    if mode == "max":
        return vmax
    return (vmin + vmax) / 2.0  # avg

@dataclass
class ArtifactMods:
    dmg_mult: float = 0.0      # decimal, e.g. +24% => 0.24
    cd_sec: float = 0.0        # seconds, e.g. -0.25
    oxygen_mult: float = 0.0   # decimal, e.g. +10% => 0.10
    speed_flat: float = 0.0    # flat
    weight_flat: float = 0.0   # flat kg

def parse_artifact_effects(effects: str, roll_mode: str) -> ArtifactMods:
    """
    Handles:
      "Speed -2.5 → -1 Cooldown -0.25s → -0.4s Damage +24% → +33%"
      and fixed values like "Cooldown -0.2s Speed +2"
    """
    s = effects.replace("→", "->")

    # Match: <StatName> <min><unit?> [-> <max><unit?>]
    # unit in: %, s, kg, or nothing
    pattern = re.compile(
        r"(?P<stat>[A-Za-z ]+?)\s+"
        r"(?P<min>[+-]?\d+(?:\.\d+)?)\s*(?P<minunit>%|s|kg)?"
        r"(?:\s*->\s*(?P<max>[+-]?\d+(?:\.\d+)?)\s*(?P<maxunit>%|s|kg)?)?"
        r"(?=\s+[A-Za-z ]+\s+[+-]?\d|$)"
    )

    mods = ArtifactMods()

    for m in pattern.finditer(s):
        stat = m.group("stat").strip().lower()
        vmin = float(m.group("min"))
        vmax = float(m.group("max")) if m.group("max") is not None else vmin
        unit = (m.group("minunit") or m.group("maxunit") or "").lower()

        v = pick_roll(vmin, vmax, roll_mode)

        if stat == "damage" and unit == "%":
            mods.dmg_mult += v / 100.0
        elif stat == "cooldown" and unit == "s":
            mods.cd_sec += v
        elif stat == "oxygen" and unit == "%":
            mods.oxygen_mult += v / 100.0
        elif stat == "speed" and unit == "":
            mods.speed_flat += v
        elif stat == "weight" and unit == "kg":
            mods.weight_flat += v

    return mods

def race_first_part(passive: Optional[str]) -> str:
    if not passive:
        return ""
    # Keep text before first ":" (abilities usually start there)
    return passive.split(":", 1)[0].strip()

def parse_race_passive(passive: str) -> Tuple[float, float, float, float]:
    """
    Returns:
      (race_dmg_mult_decimal, race_cd_sec, race_oxygen_mult_decimal, race_speed_mult_decimal)
    Accepts things like:
      "-0.2s Cooldown, +10% Damage"
      "+10% Speed, +25% Oxygen, +15% Cash"
    """
    dmg_mult = 0.0
    cd_sec = 0.0
    oxy_mult = 0.0
    spd_mult = 0.0

    # normalize separators
    s = passive.replace(".", ",").replace(";", ",")
    parts = [p.strip() for p in s.split(",") if p.strip()]

    for p in parts:
        # cooldown
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s\s+cooldown$", p, flags=re.I)
        if m:
            cd_sec += float(m.group(1))
            continue

        # % stat
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*%\s+([A-Za-z ]+)$", p)
        if m:
            val = float(m.group(1)) / 100.0
            stat = m.group(2).strip().lower()
            if stat == "damage":
                dmg_mult += val
            elif stat == "oxygen":
                oxy_mult += val
            elif stat == "speed":
                spd_mult += val
            continue

    return dmg_mult, cd_sec, oxy_mult, spd_mult

def parse_item_effect(item_name: str, effect: str) -> Dict[str, float]:
    """
    Only covers the items you listed:
      Strength Potion, Speed Potion, Love Potion, Crimson Shard, Void Shard, Oxygen Shard
    """
    e = effect.lower()

    out = {}

    # Strength Potion
    if "gun damage by" in e:
        m = re.search(r"gun damage by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["dmg_mult"] = float(m.group(1)) / 100.0

    # Speed Potion
    if "tube speed" in e and "by" in e:
        m = re.search(r"tube speed.*?by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = float(m.group(1)) / 100.0

    # Oxygen Shard
    if "oxygen capacity" in e and "by" in e:
        m = re.search(r"oxygen capacity.*?by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["oxygen_mult"] = float(m.group(1)) / 100.0

    # Love Potion
    if "all stats" in e and "by" in e:
        m = re.search(r"all stats.*?by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            val = float(m.group(1)) / 100.0
            out["dmg_mult"] = val
            out["tube_speed_mult"] = val
            out["oxygen_mult"] = val
            out["weight_mult"] = val

    # Crimson Shard / Void Shard (mixed buffs)
    if "increases" in e and "but lowers" in e:
        # Crimson: +Speed +DropLuck 50%, -Strength 15%
        # Void: +Strength +XP 50%, -Speed 15%
        if "lowers strength" in e:
            out["dmg_mult"] = -(0.15)
        if "increases strength" in e:
            out["dmg_mult"] = 0.50
        if "lowers speed" in e:
            out["tube_speed_mult"] = -(0.15)
        if "increases speed" in e:
            out["tube_speed_mult"] = 0.50

    return out

# ---------- compute (matches your Desmos equations) ----------
def compute(
    gun_damage: float,
    gun_cd: float,
    tube_oxygen: float,
    tube_speed: float,
    tube_weight: float,
    artifact_effect_texts: List[str],
    race_passive_text: str,
    item_effect_text: Optional[str],
    roll_mode: str = "avg",
    sea_angel_enabled: int = 0,
):
    # Artifacts
    a = ArtifactMods()
    for txt in artifact_effect_texts:
        mods = parse_artifact_effects(txt, roll_mode)
        a.dmg_mult += mods.dmg_mult
        a.cd_sec += mods.cd_sec
        a.oxygen_mult += mods.oxygen_mult
        a.speed_flat += mods.speed_flat
        a.weight_flat += mods.weight_flat

    # Race (first part only)
    r_first = race_first_part(race_passive_text)
    r_dmg_mult, r_cd_sec, r_oxy_mult, r_spd_mult = parse_race_passive(r_first)

    # Item (optional)
    item = {}
    if item_effect_text:
        item = parse_item_effect("selected", item_effect_text)

    potion_mult = item.get("dmg_mult", 0.0)

    # Sea Angel passive (your formula has *0 toggle; treat as enabled 0/1)
    # If you later want the real Sea Angel formula (35% of movement speed), we can replace this.
    m_sea_angel_passive = (tube_speed * 0.0035 * sea_angel_enabled)

    total_cd = gun_cd + a.cd_sec + r_cd_sec
    total_dmg = gun_damage * (1 + potion_mult + a.dmg_mult + r_dmg_mult + m_sea_angel_passive)

    base_dps = gun_damage / gun_cd
    true_dps = total_dmg / total_cd

    # Dive stats (tube + % + flat)
    total_oxygen = tube_oxygen * (1 + a.oxygen_mult + r_oxy_mult + item.get("oxygen_mult", 0.0))
    total_speed = tube_speed * (1 + r_spd_mult + item.get("tube_speed_mult", 0.0)) + a.speed_flat
    total_weight = tube_weight * (1 + item.get("weight_mult", 0.0)) + a.weight_flat

    return {
        "base_dps": base_dps,
        "true_dps": true_dps,
        "total_damage": total_dmg,
        "total_cooldown_s": total_cd,
        "total_oxygen": total_oxygen,
        "total_speed": total_speed,
        "total_weight": total_weight,
    }

# ---------- example usage (edit names like dropdown choices) ----------
if __name__ == "__main__":
    guns = load_json("guns.json")
    tubes = load_json("tubes.json")
    races = load_json("races.json")
    items = load_json("items.json")
    artifacts = load_json("artifacts.json")

    gun_name = "Morveth's Treasure Gun"
    tube_name = "Fire Tube"
    race_name = "Cyclops Race"
    artifact_names = ["Drowned Relic", "Hour Glass", "Banana"]
    item_name = "Strength Potion"
    roll_mode = "avg"  # min / avg / max

    gun = next(g for g in guns if g["Gun"] == gun_name)
    tube = next(t for t in tubes if t["Tube"] == tube_name)
    race = next(r for r in races if r["Race"] == race_name)
    item = next(i for i in items if i["Item"] == item_name)
    arts = [next(a for a in artifacts if a["Artifact"] == n) for n in artifact_names]

    result = compute(
        gun_damage=float(gun["Damage"]),
        gun_cd=parse_seconds(gun["Cooldown"]),
        tube_oxygen=float(tube["Oxygen"]),
        tube_speed=float(tube["Speed"]),
        tube_weight=float(tube["Weight"]),
        artifact_effect_texts=[a["Effects"] for a in arts],
        race_passive_text=race.get("Passive") or "",
        item_effect_text=item.get("Effect"),
        roll_mode=roll_mode,
    )

    print(result)