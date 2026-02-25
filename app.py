# app.py
# Streamlit GUI for Abyss calculator (guns/tubes/races/items/artifacts)
# Updates in this version:
# - FIXED Speed/Weight math:
#   * Parse race Weight%
#   * Apply flat bonuses first, then percent multipliers:
#       Speed  = (tubeSpeed + artifactSpeedFlat) * (1 + raceSpeed% + itemSpeed%)
#       Weight = (tubeWeight + artifactWeightFlat) * (1 + raceWeight% + itemWeight%)
# - 3 artifact dropdowns (duplicates allowed)
# - manual stat input per artifact based on its min→max ranges (fixed stats auto-locked)
# - shows full calculation breakdown in Details

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import streamlit as st


# -------------------- Load helpers --------------------
def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -------------------- Basic parsers --------------------
def parse_seconds(s: str) -> float:
    s = str(s).strip().lower()
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s$", s)
    if not m:
        raise ValueError(f"Bad seconds value: {s}")
    return float(m.group(1))


def race_first_part(passive: Optional[str]) -> str:
    if not passive:
        return ""
    # keep only the simple stats portion; abilities usually begin after first ':'
    return passive.split(":", 1)[0].strip()


def parse_race_passive(passive: str) -> Tuple[float, float, float, float, float]:
    """
    Parses only simple stat phrases like:
      "+10% Speed, +25% Oxygen, -0.2s Cooldown, +10% Damage, +20% Weight"
    Returns:
      (race_dmg_mult_decimal, race_cd_sec, race_oxygen_mult_decimal, race_speed_mult_decimal, race_weight_mult_decimal)
    """
    dmg_mult = 0.0
    cd_sec = 0.0
    oxy_mult = 0.0
    spd_mult = 0.0
    wgt_mult = 0.0

    s = passive.replace(".", ",").replace(";", ",")
    parts = [p.strip() for p in s.split(",") if p.strip()]

    for p in parts:
        # cooldown seconds, like "-0.2s Cooldown"
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s\s+cooldown$", p, flags=re.I)
        if m:
            cd_sec += float(m.group(1))
            continue

        # percent stats, like "+10% Damage"
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
            elif stat == "weight":
                wgt_mult += val
            continue

    return dmg_mult, cd_sec, oxy_mult, spd_mult, wgt_mult


def parse_item_effect(effect: Optional[str]) -> Dict[str, float]:
    """
    Matches the wording you pasted:
      Strength Potion: "Increases your Gun Damage by 30% ..."
      Speed Potion: "Increases Tube Speed by 20% ..."
      Oxygen Shard: "Increases Oxygen Capacity by 30% ..."
      Love Potion: "Increases all stats by 10% ..."
      Crimson Shard: "Increases Speed and Drop Luck by 50%, but lowers Strength by 15% ..."
      Void Shard: "Increases Strength and XP by 50%, but lowers Speed by 15% ..."
    Returns keys used by calculator:
      dmg_mult, tube_speed_mult, oxygen_mult, weight_mult
    """
    if not effect:
        return {}

    e = effect.lower()
    out: Dict[str, float] = {}

    # Strength Potion
    m = re.search(r"gun damage by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
    if m:
        out["dmg_mult"] = float(m.group(1)) / 100.0

    # Speed Potion
    m = re.search(r"tube speed by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
    if m:
        out["tube_speed_mult"] = float(m.group(1)) / 100.0

    # Oxygen Shard
    m = re.search(r"oxygen capacity by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
    if m:
        out["oxygen_mult"] = float(m.group(1)) / 100.0

    # Love Potion
    m = re.search(r"all stats by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
    if m:
        val = float(m.group(1)) / 100.0
        out["dmg_mult"] = val
        out["tube_speed_mult"] = val
        out["oxygen_mult"] = val
        out["weight_mult"] = val

    # Crimson Shard
    if "increases speed and drop luck by" in e:
        m = re.search(r"increases speed and drop luck by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers strength by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["dmg_mult"] = -float(m.group(1)) / 100.0

    # Void Shard
    if "increases strength and xp by" in e:
        m = re.search(r"increases strength and xp by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["dmg_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers speed by\s*([0-9]+(?:\.[0-9]+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = -float(m.group(1)) / 100.0

    return out


# -------------------- Artifact parsing + manual inputs --------------------
@dataclass
class ArtifactMods:
    dmg_mult: float = 0.0      # decimal, e.g. +24% => 0.24
    cd_sec: float = 0.0        # seconds, e.g. -0.25
    oxygen_mult: float = 0.0   # decimal, e.g. +10% => 0.10
    speed_flat: float = 0.0    # flat
    weight_flat: float = 0.0   # flat kg


def parse_artifact_effects_ranges(effects: str):
    """
    Returns list of:
      {"stat":"damage","unit":"%","min":24.0,"max":33.0}
      {"stat":"cooldown","unit":"s","min":-0.25,"max":-0.4}
      {"stat":"speed","unit":"","min":-2.5,"max":-1.0}
      {"stat":"weight","unit":"kg","min":10.0,"max":10.0}
    Also works for fixed values (no arrow).
    """
    if not effects:
        return []

    s = effects.replace("→", "->")

    pattern = re.compile(
        r"(?P<stat>[A-Za-z ]+?)\s+"
        r"(?P<min>[+-]?\d+(?:\.\d+)?)\s*(?P<minunit>%|s|kg)?"
        r"(?:\s*->\s*(?P<max>[+-]?\d+(?:\.\d+)?)\s*(?P<maxunit>%|s|kg)?)?"
        r"(?=\s+[A-Za-z ]+\s+[+-]?\d|$)"
    )

    out = []
    for m in pattern.finditer(s):
        stat = m.group("stat").strip().lower()
        vmin = float(m.group("min"))
        vmax = float(m.group("max")) if m.group("max") is not None else vmin
        unit = (m.group("minunit") or m.group("maxunit") or "").lower()
        out.append({"stat": stat, "unit": unit, "min": vmin, "max": vmax})

    return out


def artifact_inputs_ui(slot_key: str, artifact_row: Optional[dict]) -> Tuple[ArtifactMods, dict]:
    """
    Creates number inputs for the selected artifact's relevant stats.
    Returns:
      (ArtifactMods, debug_dict)
    """
    if not artifact_row or artifact_row.get("Artifact") == "(none)":
        return ArtifactMods(), {"artifact": "(none)", "raw_effects": "", "chosen": []}

    name = artifact_row.get("Artifact", "(unknown)")
    effects = artifact_row.get("Effects", "") or ""
    ranges = parse_artifact_effects_ranges(effects)

    st.markdown(f"**{name}**")
    if not ranges:
        st.caption("No parsed effects found for this artifact.")
        return ArtifactMods(), {"artifact": name, "raw_effects": effects, "chosen": []}

    mods = ArtifactMods()
    chosen_debug = []

    # Show only calculator-relevant stats
    for r in ranges:
        stat, unit, vmin, vmax = r["stat"], r["unit"], r["min"], r["max"]

        # Map supported fields
        field = None
        label = None

        if stat == "damage" and unit == "%":
            field = "dmg_pct"
            label = "Damage bonus (%)"
        elif stat == "cooldown" and unit == "s":
            field = "cd_s"
            label = "Cooldown change (seconds)"
        elif stat == "oxygen" and unit == "%":
            field = "oxy_pct"
            label = "Oxygen bonus (%)"
        elif stat == "speed" and unit == "":
            field = "speed_flat"
            label = "Speed change (flat)"
        elif stat == "weight" and unit == "kg":
            field = "weight_kg"
            label = "Weight change (kg)"

        if field is None:
            continue

        fixed = (vmin == vmax)
        default = (vmin + vmax) / 2.0

        diff = abs(vmax - vmin)
        step = 0.1 if diff >= 1 else 0.01

        val = st.number_input(
            f"{label} [{vmin} → {vmax}]",
            value=float(default),
            min_value=float(min(vmin, vmax)),
            max_value=float(max(vmin, vmax)),
            step=float(step),
            disabled=bool(fixed),
            key=f"{slot_key}_{field}_{name}_{vmin}_{vmax}",
        )

        chosen_debug.append({"stat": stat, "unit": unit, "min": vmin, "max": vmax, "chosen": float(val)})

        if field == "dmg_pct":
            mods.dmg_mult += float(val) / 100.0
        elif field == "cd_s":
            mods.cd_sec += float(val)
        elif field == "oxy_pct":
            mods.oxygen_mult += float(val) / 100.0
        elif field == "speed_flat":
            mods.speed_flat += float(val)
        elif field == "weight_kg":
            mods.weight_flat += float(val)

    return mods, {"artifact": name, "raw_effects": effects, "chosen": chosen_debug}


# -------------------- Core compute (matches your Desmos equations) --------------------
def compute(
    gun_damage: float,
    gun_cd: float,
    tube_oxygen: float,
    tube_speed: float,
    tube_weight: float,
    artifact_mods_list: List[ArtifactMods],
    race_passive_text: str,
    item_effect_text: Optional[str],
):
    # Sum artifact mods
    a = ArtifactMods()
    for m in artifact_mods_list:
        a.dmg_mult += m.dmg_mult
        a.cd_sec += m.cd_sec
        a.oxygen_mult += m.oxygen_mult
        a.speed_flat += m.speed_flat
        a.weight_flat += m.weight_flat

    # Race (simple stats only)
    r_first = race_first_part(race_passive_text)
    r_dmg_mult, r_cd_sec, r_oxy_mult, r_spd_mult, r_wgt_mult = parse_race_passive(r_first)

    # Item
    item = parse_item_effect(item_effect_text)
    potion_mult = item.get("dmg_mult", 0.0)

    # DPS formulas (Desmos)
    total_cd = gun_cd + a.cd_sec + r_cd_sec
    total_dmg = gun_damage * (1 + potion_mult + a.dmg_mult + r_dmg_mult)
    base_dps = gun_damage / gun_cd
    true_dps = total_dmg / total_cd

    # Dive stats (FIXED): flat first, then percent
    item_oxy = item.get("oxygen_mult", 0.0)
    item_spd = item.get("tube_speed_mult", 0.0)
    item_wgt = item.get("weight_mult", 0.0)

    total_oxygen = tube_oxygen * (1 + a.oxygen_mult + r_oxy_mult + item_oxy)
    total_speed = (tube_speed + a.speed_flat) * (1 + r_spd_mult + item_spd)
    total_weight = (tube_weight + a.weight_flat) * (1 + r_wgt_mult + item_wgt)

    debug = {
        "gun_damage": gun_damage,
        "gun_cd": gun_cd,
        "potion_mult": potion_mult,
        "artifact_dmg_mult_sum": a.dmg_mult,
        "artifact_cd_sum": a.cd_sec,
        "race_dmg_mult": r_dmg_mult,
        "race_cd": r_cd_sec,
        "total_cd": total_cd,
        "total_dmg": total_dmg,
        "tube_oxygen_base": tube_oxygen,
        "tube_speed_base": tube_speed,
        "tube_weight_base": tube_weight,
        "artifact_oxygen_mult_sum": a.oxygen_mult,
        "race_oxygen_mult": r_oxy_mult,
        "item_oxygen_mult": item_oxy,
        "race_speed_mult": r_spd_mult,
        "item_speed_mult": item_spd,
        "artifact_speed_flat_sum": a.speed_flat,
        "race_weight_mult": r_wgt_mult,
        "item_weight_mult": item_wgt,
        "artifact_weight_flat_sum": a.weight_flat,
        "race_passive_used": r_first,
        "item_effect_used": item_effect_text or "",
    }

    return {
        "Base DPS": base_dps,
        "True DPS": true_dps,
        "Total Damage": total_dmg,
        "Total Cooldown (s)": total_cd,
        "Oxygen": total_oxygen,
        "Speed": total_speed,
        "Weight": total_weight,
        "_debug": debug,
    }


# -------------------- Streamlit UI --------------------
st.set_page_config(page_title="Abyss Build Calculator", layout="wide")
st.title("Abyss Build Calculator")

st.caption("Put these files next to app.py: guns.json, tubes.json, races.json, items.json, artifacts.json")

# Load data
try:
    guns = load_json("guns.json")
    tubes = load_json("tubes.json")
    races = load_json("races.json")
    items = load_json("items.json")
    artifacts = load_json("artifacts.json")
except Exception as e:
    st.error(f"Failed to load json files: {e}")
    st.stop()

# Maps
guns_by = {g["Gun"]: g for g in guns}
tubes_by = {t["Tube"]: t for t in tubes}
races_by = {r["Race"]: r for r in races}
items_by = {i["Item"]: i for i in items}
arts_by = {a["Artifact"]: a for a in artifacts}

# Controls
col1, col2, col3 = st.columns(3)

with col1:
    gun_name = st.selectbox("Gun", sorted(guns_by.keys()))
    tube_name = st.selectbox("Tube", sorted(tubes_by.keys()))

with col2:
    race_name = st.selectbox("Race", sorted(races_by.keys()))
    item_name = st.selectbox("Item buff (optional)", ["(none)"] + sorted(items_by.keys()))

with col3:
    st.markdown("**Optional manual overrides**")
    use_manual = st.checkbox("Override gun/tube base stats", value=False)
    st.caption("Turn on if you want to input custom base values.")

gun = guns_by[gun_name]
tube = tubes_by[tube_name]
race = races_by[race_name]
item = None if item_name == "(none)" else items_by[item_name]

try:
    base_gun_damage = float(gun["Damage"])
    base_gun_cd = parse_seconds(gun["Cooldown"])
    base_tube_oxygen = float(tube["Oxygen"])
    base_tube_speed = float(tube["Speed"])
    base_tube_weight = float(tube["Weight"])
except Exception as e:
    st.error(f"Bad data in selected gun/tube: {e}")
    st.stop()

if use_manual:
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        gun_damage = st.number_input("Gun Damage", value=float(base_gun_damage), step=1.0)
    with m2:
        gun_cd = st.number_input("Gun Cooldown (s)", value=float(base_gun_cd), step=0.01)
    with m3:
        tube_oxygen = st.number_input("Tube Oxygen", value=float(base_tube_oxygen), step=1.0)
    with m4:
        tube_speed = st.number_input("Tube Speed", value=float(base_tube_speed), step=1.0)
    with m5:
        tube_weight = st.number_input("Tube Weight", value=float(base_tube_weight), step=1.0)
else:
    gun_damage = base_gun_damage
    gun_cd = base_gun_cd
    tube_oxygen = base_tube_oxygen
    tube_speed = base_tube_speed
    tube_weight = base_tube_weight

st.divider()
st.subheader("Artifacts (3 slots, duplicates allowed)")

art_options = ["(none)"] + sorted(arts_by.keys())
a1_name = st.selectbox("Artifact 1", art_options, index=0)
a2_name = st.selectbox("Artifact 2", art_options, index=0)
a3_name = st.selectbox("Artifact 3", art_options, index=0)

a1 = None if a1_name == "(none)" else arts_by[a1_name]
a2 = None if a2_name == "(none)" else arts_by[a2_name]
a3 = None if a3_name == "(none)" else arts_by[a3_name]

st.caption("Enter your exact artifact roll values (within the shown min→max). Fixed values are locked.")

cA, cB, cC = st.columns(3)
with cA:
    a1_mods, a1_dbg = artifact_inputs_ui("A1", a1 or {"Artifact": "(none)"})
with cB:
    a2_mods, a2_dbg = artifact_inputs_ui("A2", a2 or {"Artifact": "(none)"})
with cC:
    a3_mods, a3_dbg = artifact_inputs_ui("A3", a3 or {"Artifact": "(none)"})

# Compute
try:
    result = compute(
        gun_damage=gun_damage,
        gun_cd=gun_cd,
        tube_oxygen=tube_oxygen,
        tube_speed=tube_speed,
        tube_weight=tube_weight,
        artifact_mods_list=[a1_mods, a2_mods, a3_mods],
        race_passive_text=race.get("Passive") or "",
        item_effect_text=(item.get("Effect") if item else None),
    )
except Exception as e:
    st.error(f"Error computing stats: {e}")
    st.stop()

st.divider()
st.subheader("Results")

r1, r2, r3 = st.columns(3)
r1.metric("Base DPS", f"{result['Base DPS']:.3f}")
r2.metric("True DPS", f"{result['True DPS']:.3f}")
r3.metric("Total Cooldown", f"{result['Total Cooldown (s)']:.3f}s")

d1, d2, d3 = st.columns(3)
d1.metric("Total Damage", f"{result['Total Damage']:.3f}")
d2.metric("Oxygen", f"{result['Oxygen']:.2f}")
d3.metric("Speed", f"{result['Speed']:.2f}")

st.metric("Weight", f"{result['Weight']:.2f}")

with st.expander("Details (inputs + calculations)"):
    st.write("Gun:", gun)
    st.write("Tube:", tube)
    st.write("Race:", race_name)
    st.write("Race passive used (trimmed):", result["_debug"]["race_passive_used"])
    if item:
        st.write("Item:", item)
    else:
        st.write("Item: (none)")

    st.write("Artifacts chosen + chosen rolls:")
    st.json({"A1": a1_dbg, "A2": a2_dbg, "A3": a3_dbg})

    dbg = result["_debug"]

    st.markdown("### DPS calculations (your Desmos formulas)")
    st.code(
        f"""TotalCD = gunCD + artifactCDsum + raceCD
= {dbg['gun_cd']:.4f} + {dbg['artifact_cd_sum']:.4f} + {dbg['race_cd']:.4f}
= {dbg['total_cd']:.4f}

TotalDMG = gunDMG * (1 + potionMult + artifactMultSum + raceMult)
= {dbg['gun_damage']:.4f} * (1 + {dbg['potion_mult']:.4f} + {dbg['artifact_dmg_mult_sum']:.4f} + {dbg['race_dmg_mult']:.4f})
= {dbg['total_dmg']:.4f}

BaseDPS = gunDMG / gunCD
= {dbg['gun_damage']:.4f} / {dbg['gun_cd']:.4f}
= {result['Base DPS']:.4f}

TrueDPS = TotalDMG / TotalCD
= {dbg['total_dmg']:.4f} / {dbg['total_cd']:.4f}
= {result['True DPS']:.4f}
""",
        language="text",
    )

    st.markdown("### Dive stat calculations (fixed order: flat then %)")
    st.code(
        f"""Oxygen = tubeOxygen * (1 + artifactOxyMult + raceOxyMult + itemOxyMult)
= {dbg['tube_oxygen_base']:.4f} * (1 + {dbg['artifact_oxygen_mult_sum']:.4f} + {dbg['race_oxygen_mult']:.4f} + {dbg['item_oxygen_mult']:.4f})
= {result['Oxygen']:.4f}

Speed = (tubeSpeed + artifactSpeedFlat) * (1 + raceSpeedMult + itemSpeedMult)
= ({dbg['tube_speed_base']:.4f} + {dbg['artifact_speed_flat_sum']:.4f}) * (1 + {dbg['race_speed_mult']:.4f} + {dbg['item_speed_mult']:.4f})
= {result['Speed']:.4f}

Weight = (tubeWeight + artifactWeightFlat) * (1 + raceWeightMult + itemWeightMult)
= ({dbg['tube_weight_base']:.4f} + {dbg['artifact_weight_flat_sum']:.4f}) * (1 + {dbg['race_weight_mult']:.4f} + {dbg['item_weight_mult']:.4f})
= {result['Weight']:.4f}
""",
        language="text",
    )