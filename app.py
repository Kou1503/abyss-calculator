# app.py
# Abyss Build Calculator (Streamlit)
# - Tier sliders (1..7) for artifact ranged stats (linear: step=(max-min)/6)
# - Fixed race parsing (doesn't break decimals like -0.2s)
# - Speed/Weight: flat first, then % multipliers
# - Import/Export build as JSON (recommended: store tiers)

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st


# ==================== Paths ====================
BASE_DIR = Path(__file__).resolve().parent

def load_json_file(filename: str):
    with open(BASE_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== Parsing helpers ====================
def parse_seconds(s: str) -> float:
    s = str(s).strip().lower()
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s$", s)
    if not m:
        raise ValueError(f"Bad seconds value: {s}")
    return float(m.group(1))


def race_first_part(passive: Optional[str]) -> str:
    if not passive:
        return ""
    return passive.split(":", 1)[0].strip()


def parse_race_passive(passive: str) -> Tuple[float, float, float, float, float]:
    """
    Parses ONLY simple stat phrases like:
      "+8% Cash, -0.2s Cooldown, +12% Damage."
    Does NOT break decimals.
    Returns:
      (race_dmg_mult, race_cd_sec, race_oxygen_mult, race_speed_mult, race_weight_mult)
    """
    dmg_mult = 0.0
    cd_sec = 0.0
    oxy_mult = 0.0
    spd_mult = 0.0
    wgt_mult = 0.0

    s = passive.replace(";", ",")
    # split by commas OR ". " sentence breaks; keep decimals intact
    parts = [p.strip() for p in re.split(r",|\.\s+", s) if p.strip()]

    for p in parts:
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*s\s+cooldown$", p, flags=re.I)
        if m:
            cd_sec += float(m.group(1))
            continue

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
    Returns keys:
      dmg_mult, tube_speed_mult, oxygen_mult, weight_mult
    """
    if not effect:
        return {}
    e = effect.lower()
    out: Dict[str, float] = {}

    m = re.search(r"gun damage by\s*([0-9]+(?:\.\d+)?)\s*%", e)
    if m:
        out["dmg_mult"] = float(m.group(1)) / 100.0

    m = re.search(r"tube speed by\s*([0-9]+(?:\.\d+)?)\s*%", e)
    if m:
        out["tube_speed_mult"] = float(m.group(1)) / 100.0

    m = re.search(r"oxygen capacity by\s*([0-9]+(?:\.\d+)?)\s*%", e)
    if m:
        out["oxygen_mult"] = float(m.group(1)) / 100.0

    m = re.search(r"all stats by\s*([0-9]+(?:\.\d+)?)\s*%", e)
    if m:
        val = float(m.group(1)) / 100.0
        out["dmg_mult"] = val
        out["tube_speed_mult"] = val
        out["oxygen_mult"] = val
        out["weight_mult"] = val

    if "increases speed and drop luck by" in e:
        m = re.search(r"increases speed and drop luck by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers strength by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["dmg_mult"] = -float(m.group(1)) / 100.0

    if "increases strength and xp by" in e:
        m = re.search(r"increases strength and xp by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["dmg_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers speed by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = -float(m.group(1)) / 100.0

    return out


# ==================== Artifact parsing + tier system ====================
@dataclass
class ArtifactMods:
    dmg_mult: float = 0.0      # decimal (e.g., +24% => 0.24)
    cd_sec: float = 0.0        # seconds
    oxygen_mult: float = 0.0   # decimal
    speed_flat: float = 0.0    # flat
    weight_flat: float = 0.0   # flat kg


def parse_artifact_effects_ranges(effects: str):
    """
    Parses 'Effects' like:
      "Speed -2.5 → -1 Cooldown -0.25s → -0.4s Damage +24% → +33%"
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


def tier_to_value(vmin: float, vmax: float, tier: int, tiers: int = 7) -> float:
    # 7 tiers => 6 equal gaps
    t = max(1, min(int(tier), tiers))
    if vmin == vmax:
        return float(vmin)
    step = (vmax - vmin) / (tiers - 1)
    return float(vmin + (t - 1) * step)


# Stable field mapping (used for session keys + export/import)
# field_key => (stat_name, unit)
ART_FIELD_DEFS = {
    "dmg_pct":    ("damage", "%"),
    "cd_s":       ("cooldown", "s"),
    "oxy_pct":    ("oxygen", "%"),
    "speed_flat": ("speed", ""),
    "weight_kg":  ("weight", "kg"),
}

def range_to_field(stat: str, unit: str) -> Optional[str]:
    stat = stat.strip().lower()
    unit = unit.strip().lower()
    for field_key, (s, u) in ART_FIELD_DEFS.items():
        if stat == s and unit == u:
            return field_key
    return None


def artifact_inputs_ui(slot: str, artifact_row: Optional[dict]) -> Tuple[ArtifactMods, dict]:
    """
    slot: 'A1'|'A2'|'A3'
    Uses stable tier keys in session_state:
      A1_dmg_pct, A1_cd_s, A1_oxy_pct, A1_speed_flat, A1_weight_kg
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
    chosen = []

    for r in ranges:
        stat, unit, vmin, vmax = r["stat"], r["unit"], r["min"], r["max"]
        field = range_to_field(stat, unit)
        if field is None:
            continue

        fixed = (vmin == vmax)
        pretty = {
            "dmg_pct": "Damage bonus",
            "cd_s": "Cooldown change",
            "oxy_pct": "Oxygen bonus",
            "speed_flat": "Speed change",
            "weight_kg": "Weight change",
        }[field]

        if fixed:
            val = float(vmin)
            st.text(f"{pretty}: {val} (fixed)")
            chosen.append({"field": field, "min": vmin, "max": vmax, "tier": None, "value": val})
        else:
            tier_key = f"{slot}_{field}"
            # ensure default exists
            if tier_key not in st.session_state:
                st.session_state[tier_key] = 4

            tier = st.slider(
                f"{pretty} Tier",
                min_value=1,
                max_value=7,
                value=int(st.session_state[tier_key]),
                step=1,
                key=tier_key,
            )
            val = tier_to_value(vmin, vmax, int(tier), tiers=7)
            st.caption(f"[{vmin} → {vmax}]  Tier {tier} ⇒ {val:.4f}")
            chosen.append({"field": field, "min": vmin, "max": vmax, "tier": int(tier), "value": float(val)})

        # apply
        if field == "dmg_pct":
            mods.dmg_mult += val / 100.0
        elif field == "cd_s":
            mods.cd_sec += val
        elif field == "oxy_pct":
            mods.oxygen_mult += val / 100.0
        elif field == "speed_flat":
            mods.speed_flat += val
        elif field == "weight_kg":
            mods.weight_flat += val

    return mods, {"artifact": name, "raw_effects": effects, "chosen": chosen}


# ==================== Compute ====================
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
    a = ArtifactMods()
    for m in artifact_mods_list:
        a.dmg_mult += m.dmg_mult
        a.cd_sec += m.cd_sec
        a.oxygen_mult += m.oxygen_mult
        a.speed_flat += m.speed_flat
        a.weight_flat += m.weight_flat

    r_first = race_first_part(race_passive_text)
    r_dmg, r_cd, r_oxy, r_spd, r_wgt = parse_race_passive(r_first)

    item = parse_item_effect(item_effect_text)
    potion_mult = item.get("dmg_mult", 0.0)

    total_cd = gun_cd + a.cd_sec + r_cd
    total_dmg = gun_damage * (1 + potion_mult + a.dmg_mult + r_dmg)

    base_dps = gun_damage / gun_cd
    true_dps = total_dmg / total_cd

    item_oxy = item.get("oxygen_mult", 0.0)
    item_spd = item.get("tube_speed_mult", 0.0)
    item_wgt = item.get("weight_mult", 0.0)

    total_oxygen = tube_oxygen * (1 + a.oxygen_mult + r_oxy + item_oxy)
    total_speed = (tube_speed + a.speed_flat) * (1 + r_spd + item_spd)
    total_weight = (tube_weight + a.weight_flat) * (1 + r_wgt + item_wgt)

    debug = {
        "race_passive_used": r_first,
        "total_cd": total_cd,
        "total_dmg": total_dmg,
        "gun_cd": gun_cd,
        "gun_damage": gun_damage,
        "artifact_cd_sum": a.cd_sec,
        "race_cd": r_cd,
        "potion_mult": potion_mult,
        "artifact_dmg_mult_sum": a.dmg_mult,
        "race_dmg_mult": r_dmg,
        "tube_oxygen_base": tube_oxygen,
        "tube_speed_base": tube_speed,
        "tube_weight_base": tube_weight,
        "artifact_oxygen_mult_sum": a.oxygen_mult,
        "race_oxygen_mult": r_oxy,
        "item_oxygen_mult": item_oxy,
        "artifact_speed_flat_sum": a.speed_flat,
        "race_speed_mult": r_spd,
        "item_speed_mult": item_spd,
        "artifact_weight_flat_sum": a.weight_flat,
        "race_weight_mult": r_wgt,
        "item_weight_mult": item_wgt,
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


# ==================== Import/Export ====================
BUILD_VERSION = 1

def build_export_dict() -> dict:
    # selection keys
    gun = st.session_state.get("gun_select")
    tube = st.session_state.get("tube_select")
    race = st.session_state.get("race_select")
    item = st.session_state.get("item_select")
    a1 = st.session_state.get("a1_select")
    a2 = st.session_state.get("a2_select")
    a3 = st.session_state.get("a3_select")

    def slot_tiers(slot: str) -> dict:
        # store tiers for all known fields (even if not currently used)
        out = {}
        for field in ART_FIELD_DEFS.keys():
            k = f"{slot}_{field}"
            if k in st.session_state:
                out[field] = int(st.session_state[k])
        return out

    return {
        "version": BUILD_VERSION,
        "gun": gun,
        "tube": tube,
        "race": race,
        "item": None if item == "(none)" else item,
        "artifacts": [
            {"slot": "A1", "name": None if a1 == "(none)" else a1, "tiers": slot_tiers("A1")},
            {"slot": "A2", "name": None if a2 == "(none)" else a2, "tiers": slot_tiers("A2")},
            {"slot": "A3", "name": None if a3 == "(none)" else a3, "tiers": slot_tiers("A3")},
        ],
    }


def apply_import_dict(data: dict):
    # set selections first
    st.session_state["gun_select"] = data.get("gun", st.session_state.get("gun_select"))
    st.session_state["tube_select"] = data.get("tube", st.session_state.get("tube_select"))
    st.session_state["race_select"] = data.get("race", st.session_state.get("race_select"))
    st.session_state["item_select"] = data.get("item") or "(none)"

    arts = data.get("artifacts", [])
    slot_map = {"A1": "a1_select", "A2": "a2_select", "A3": "a3_select"}

    for entry in arts:
        slot = entry.get("slot")
        if slot not in slot_map:
            continue
        st.session_state[slot_map[slot]] = entry.get("name") or "(none)"

        tiers = entry.get("tiers", {}) or {}
        for field, tier in tiers.items():
            if field in ART_FIELD_DEFS:
                st.session_state[f"{slot}_{field}"] = int(tier)


# ==================== UI ====================
st.set_page_config(page_title="Abyss Build Calculator", layout="wide")
st.title("Abyss Build Calculator")

# Load data
try:
    guns = load_json_file("guns.json")
    tubes = load_json_file("tubes.json")
    races = load_json_file("races.json")
    items = load_json_file("items.json")
    artifacts = load_json_file("artifacts.json")
except Exception as e:
    st.error(f"Failed to load json files: {e}")
    st.stop()

guns_by = {g["Gun"]: g for g in guns}
tubes_by = {t["Tube"]: t for t in tubes}
races_by = {r["Race"]: r for r in races}
items_by = {i["Item"]: i for i in items}
arts_by = {a["Artifact"]: a for a in artifacts}

# Defaults for selection widgets
if "gun_select" not in st.session_state:
    st.session_state["gun_select"] = sorted(guns_by.keys())[0]
if "tube_select" not in st.session_state:
    st.session_state["tube_select"] = sorted(tubes_by.keys())[0]
if "race_select" not in st.session_state:
    st.session_state["race_select"] = sorted(races_by.keys())[0]
if "item_select" not in st.session_state:
    st.session_state["item_select"] = "(none)"
if "a1_select" not in st.session_state:
    st.session_state["a1_select"] = "(none)"
if "a2_select" not in st.session_state:
    st.session_state["a2_select"] = "(none)"
if "a3_select" not in st.session_state:
    st.session_state["a3_select"] = "(none)"

# --- Import/Export panel
with st.sidebar:
    st.header("Build Import / Export")

    export_data = json.dumps(build_export_dict(), indent=2)
    st.download_button(
        "⬇️ Export build.json",
        data=export_data,
        file_name="abyss_build.json",
        mime="application/json",
        use_container_width=True,
    )

    st.caption("Import a previously exported build JSON.")
    up = st.file_uploader("Upload build.json", type=["json"])
    if up is not None:
        try:
            imported = json.load(up)
            st.success("Loaded JSON file.")
            if st.button("✅ Apply imported build", use_container_width=True):
                apply_import_dict(imported)
                st.rerun()
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    with st.expander("Copy/paste JSON"):
        st.text_area("Build JSON", value=export_data, height=220)

# --- Main selections
col1, col2, col3 = st.columns(3)

with col1:
    st.selectbox("Gun", sorted(guns_by.keys()), key="gun_select")
    st.selectbox("Tube", sorted(tubes_by.keys()), key="tube_select")

with col2:
    st.selectbox("Race", sorted(races_by.keys()), key="race_select")
    st.selectbox("Item buff (optional)", ["(none)"] + sorted(items_by.keys()), key="item_select")

with col3:
    st.markdown("**Optional manual overrides**")
    use_manual = st.checkbox("Override gun/tube base stats", value=False)

gun = guns_by[st.session_state["gun_select"]]
tube = tubes_by[st.session_state["tube_select"]]
race = races_by[st.session_state["race_select"]]
item = None if st.session_state["item_select"] == "(none)" else items_by[st.session_state["item_select"]]

base_gun_damage = float(gun["Damage"])
base_gun_cd = parse_seconds(gun["Cooldown"])
base_tube_oxygen = float(tube["Oxygen"])
base_tube_speed = float(tube["Speed"])
base_tube_weight = float(tube["Weight"])

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
st.caption("Ranged stats use Tier 1–7 sliders (linear). Tier 1=min, Tier 7=max.")

art_options = ["(none)"] + sorted(arts_by.keys())

st.selectbox("Artifact 1", art_options, key="a1_select")
st.selectbox("Artifact 2", art_options, key="a2_select")
st.selectbox("Artifact 3", art_options, key="a3_select")

a1 = None if st.session_state["a1_select"] == "(none)" else arts_by[st.session_state["a1_select"]]
a2 = None if st.session_state["a2_select"] == "(none)" else arts_by[st.session_state["a2_select"]]
a3 = None if st.session_state["a3_select"] == "(none)" else arts_by[st.session_state["a3_select"]]

cA, cB, cC = st.columns(3)
with cA:
    a1_mods, a1_dbg = artifact_inputs_ui("A1", a1 or {"Artifact": "(none)"})
with cB:
    a2_mods, a2_dbg = artifact_inputs_ui("A2", a2 or {"Artifact": "(none)"})
with cC:
    a3_mods, a3_dbg = artifact_inputs_ui("A3", a3 or {"Artifact": "(none)"})

# Compute
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
    dbg = result["_debug"]
    st.write("Gun:", gun)
    st.write("Tube:", tube)
    st.write("Race:", st.session_state["race_select"])
    st.write("Race passive used (trimmed):", dbg["race_passive_used"])
    st.write("Item:", item if item else "(none)")
    st.write("Artifacts chosen + tiers:")
    st.json({"A1": a1_dbg, "A2": a2_dbg, "A3": a3_dbg})

    st.markdown("### DPS calculations")
    st.code(
        f"""TotalCD = gunCD + artifactCDsum + raceCD
= {dbg['gun_cd']:.4f} + {dbg['artifact_cd_sum']:.4f} + {dbg['race_cd']:.4f}
= {dbg['total_cd']:.4f}

TotalDMG = gunDMG * (1 + potionMult + artifactMultSum + raceMult)
= {dbg['gun_damage']:.4f} * (1 + {dbg['potion_mult']:.4f} + {dbg['artifact_dmg_mult_sum']:.4f} + {dbg['race_dmg_mult']:.4f})
= {dbg['total_dmg']:.4f}

BaseDPS = {result['Base DPS']:.4f}
TrueDPS = {result['True DPS']:.4f}
""",
        language="text",
    )

    st.markdown("### Dive stat calculations (flat then %)")
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
