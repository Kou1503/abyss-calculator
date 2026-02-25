# app.py
# Abyss Build Calculator (Streamlit)
# - 3 artifact slots (duplicates allowed)
# - Each artifact stat supports: Tier slider (1..7) OR exact numeric input (clamped to min..max)
# - Multiple potions/shards (multiselect) stacking additively
# - Race specials (checkboxes that appear only for certain races) with saved state
# - Import/Export build.json saving EVERYTHING (exact values used)
# - Robust file paths for Streamlit Cloud

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import streamlit as st


# ==================== Paths ====================
BASE_DIR = Path(__file__).resolve().parent


def load_json_file(filename: str):
    with open(BASE_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def load_optional_json(filename: str) -> dict:
    p = BASE_DIR / filename
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# Optional: exact tier tables to fix non-linear / wrong wiki ranges
# Format:
# {
#   "Crown": { "dmg_pct": [v1,v2,v3,v4,v5,v6,v7], "cd_s":[...], ... }
# }
TIER_OVERRIDES = load_optional_json("artifact_tier_overrides.json")


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

    # Crimson Shard
    if "increases speed and drop luck by" in e:
        m = re.search(r"increases speed and drop luck by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers strength by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["dmg_mult"] = -float(m.group(1)) / 100.0

    # Void Shard
    if "increases strength and xp by" in e:
        m = re.search(r"increases strength and xp by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["dmg_mult"] = float(m.group(1)) / 100.0
        m = re.search(r"lowers speed by\s*([0-9]+(?:\.\d+)?)\s*%", e)
        if m:
            out["tube_speed_mult"] = -float(m.group(1)) / 100.0

    return out


def combine_item_effects(items: List[dict]) -> Dict[str, float]:
    total = {"dmg_mult": 0.0, "tube_speed_mult": 0.0, "oxygen_mult": 0.0, "weight_mult": 0.0}
    for it in items:
        eff = parse_item_effect(it.get("Effect"))
        for k in total:
            total[k] += eff.get(k, 0.0)
    return total


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


# Stable field mapping used for session keys + export/import
ART_FIELD_DEFS = {
    "dmg_pct":    ("damage", "%"),
    "cd_s":       ("cooldown", "s"),
    "oxy_pct":    ("oxygen", "%"),
    "speed_flat": ("speed", ""),
    "weight_kg":  ("weight", "kg"),
}

FIELD_LABELS = {
    "dmg_pct": "Damage bonus",
    "cd_s": "Cooldown change",
    "oxy_pct": "Oxygen bonus",
    "speed_flat": "Speed change",
    "weight_kg": "Weight change",
}


def range_to_field(stat: str, unit: str) -> Optional[str]:
    stat = stat.strip().lower()
    unit = unit.strip().lower()
    for field_key, (s, u) in ART_FIELD_DEFS.items():
        if stat == s and unit == u:
            return field_key
    return None


def tier_to_value_linear(vmin: float, vmax: float, tier: int, tiers: int = 7) -> float:
    # 7 tiers => 6 equal gaps
    t = max(1, min(int(tier), tiers))
    if vmin == vmax:
        return float(vmin)
    step = (vmax - vmin) / (tiers - 1)
    return float(vmin + (t - 1) * step)


def tier_to_value_with_override(artifact_name: str, field: str, vmin: float, vmax: float, tier: int) -> float:
    arr = TIER_OVERRIDES.get(artifact_name, {}).get(field)
    if isinstance(arr, list) and len(arr) == 7:
        return float(arr[max(1, min(int(tier), 7)) - 1])
    return tier_to_value_linear(vmin, vmax, tier, tiers=7)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def artifact_inputs_ui(slot: str, artifact_row: Optional[dict]) -> Tuple[ArtifactMods, dict]:
    """
    slot: 'A1'|'A2'|'A3'
    Each ranged stat offers mode: tier or value.
    Stable session keys:
      A1_dmg_pct_mode, A1_dmg_pct_tier, A1_dmg_pct_value, ...
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
    chosen: List[dict] = []

    for r in ranges:
        stat, unit, vmin, vmax = r["stat"], r["unit"], r["min"], r["max"]
        field = range_to_field(stat, unit)
        if field is None:
            continue

        pretty = FIELD_LABELS[field]
        fixed = (vmin == vmax)

        # Keys
        mode_key = f"{slot}_{field}_mode"
        tier_key = f"{slot}_{field}_tier"
        val_key = f"{slot}_{field}_value"

        if fixed:
            val = float(vmin)
            st.text(f"{pretty}: {val} (fixed)")
            chosen.append({"field": field, "min": vmin, "max": vmax, "mode": "fixed", "tier": None, "value": float(val)})
        else:
            # defaults
            if mode_key not in st.session_state:
                st.session_state[mode_key] = "tier"
            if tier_key not in st.session_state:
                st.session_state[tier_key] = 4

            mode = st.radio(
                f"{pretty} input",
                options=["tier", "value"],
                horizontal=True,
                key=mode_key,
            )

            lo = float(min(vmin, vmax))
            hi = float(max(vmin, vmax))

            if mode == "tier":
                tier = st.slider(
                    f"{pretty} Tier",
                    min_value=1,
                    max_value=7,
                    value=int(st.session_state[tier_key]),
                    step=1,
                    key=tier_key,
                )
                val = tier_to_value_with_override(name, field, vmin, vmax, int(tier))
                st.caption(f"[{vmin} → {vmax}]  Tier {tier} ⇒ {val:.4f}")

                # keep a synced numeric value (useful for exporting exact value)
                st.session_state[val_key] = float(val)

                chosen.append({"field": field, "min": vmin, "max": vmax, "mode": "tier", "tier": int(tier), "value": float(val)})

            else:
                # value input; default to last used, else mid range
                if val_key not in st.session_state:
                    st.session_state[val_key] = float((lo + hi) / 2.0)
                step = (hi - lo) / 100.0 if abs(hi - lo) > 0 else 0.01
                val_in = st.number_input(
                    f"{pretty} value",
                    min_value=lo,
                    max_value=hi,
                    value=float(st.session_state[val_key]),
                    step=float(step),
                    key=val_key,
                )
                val = float(clamp(val_in, lo, hi))
                st.caption(f"[{vmin} → {vmax}]  Value ⇒ {val:.4f}")

                chosen.append({"field": field, "min": vmin, "max": vmax, "mode": "value", "tier": None, "value": float(val)})

        # apply the stat value to mods
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

    return mods, {"artifact": name, "raw_effects": effects, "chosen": chosen}


# ==================== Race specials ====================
# Add more races here easily.
# effects supports:
#   dmg_mult (decimal), tube_speed_mult (decimal), oxygen_mult (decimal), weight_mult (decimal), cd_sec (seconds)
RACE_SPECIALS = {
    "Shark Race": [
        {
            "key": "race_special_shark_30",
            "label": "Enable Shark bonus: +30% Damage",
            "effects": {"dmg_mult": 0.30},
        }
    ],
    "Kraken Race": [
        {
            "key": "race_special_kraken_50",
            "label": "Enable Kraken bonus: +50% Damage",
            "effects": {"dmg_mult": 0.50},
        }
    ],
    "Sea Angel Race": [
        {
            "key": "race_special_seaangel_convert",
            "label": "Enable Sea Angel: +35% of movement speed as flat gun damage",
            "effects": {"speed_to_flat_damage_ratio": 0.35},
        }
    ],
}


def get_race_special_effects(race_name: str) -> Tuple[Dict[str, float], Dict[str, bool]]:
    total = {
    "dmg_mult": 0.0,
    "tube_speed_mult": 0.0,
    "oxygen_mult": 0.0,
    "weight_mult": 0.0,
    "cd_sec": 0.0,
    "speed_to_flat_damage_ratio": 0.0,   # NEW
}
    states: Dict[str, bool] = {}

    opts = RACE_SPECIALS.get(race_name, [])
    if not opts:
        return total, states

    st.markdown("### Race Specials")
    for opt in opts:
        enabled = st.checkbox(opt["label"], key=opt["key"])
        states[opt["key"]] = bool(enabled)
        if enabled:
            eff = opt.get("effects", {})
            for k in total:
                total[k] += float(eff.get(k, 0.0))
    return total, states


# ==================== Compute ====================
def compute(
    gun_damage: float,
    gun_cd: float,
    tube_oxygen: float,
    tube_speed: float,
    tube_weight: float,
    artifact_mods_list: List[ArtifactMods],
    race_passive_text: str,
    potions_effects: Dict[str, float],
    race_specials: Dict[str, float],
):
    # sum artifact mods
    a = ArtifactMods()
    for m in artifact_mods_list:
        a.dmg_mult += m.dmg_mult
        a.cd_sec += m.cd_sec
        a.oxygen_mult += m.oxygen_mult
        a.speed_flat += m.speed_flat
        a.weight_flat += m.weight_flat

    # base race stats (simple)
    r_first = race_first_part(race_passive_text)
    r_dmg, r_cd, r_oxy, r_spd, r_wgt = parse_race_passive(r_first)

    # potions
    potion_dmg = potions_effects.get("dmg_mult", 0.0)
    potion_spd = potions_effects.get("tube_speed_mult", 0.0)
    potion_oxy = potions_effects.get("oxygen_mult", 0.0)
    potion_wgt = potions_effects.get("weight_mult", 0.0)

    # race specials
    sp_dmg = race_specials.get("dmg_mult", 0.0)
    sp_spd = race_specials.get("tube_speed_mult", 0.0)
    sp_oxy = race_specials.get("oxygen_mult", 0.0)
    sp_wgt = race_specials.get("weight_mult", 0.0)
    sp_cd = race_specials.get("cd_sec", 0.0)
    sp_speed_to_flat_ratio = race_specials.get("speed_to_flat_damage_ratio", 0.0)

    # DPS
    
    total_cd = gun_cd + a.cd_sec + r_cd + sp_cd

    # Dive stats first (needed for Sea Angel conversion)
    total_oxygen = tube_oxygen * (1 + a.oxygen_mult + r_oxy + potion_oxy + sp_oxy)
    total_speed = (tube_speed + a.speed_flat) * (1 + r_spd + potion_spd + sp_spd)
    total_weight = (tube_weight + a.weight_flat) * (1 + r_wgt + potion_wgt + sp_wgt)

    # Sea Angel: add integer flat damage based on movement speed
    seaangel_flat_dmg = 0
    if sp_speed_to_flat_ratio and sp_speed_to_flat_ratio != 0.0:
        seaangel_flat_dmg = int(total_speed * sp_speed_to_flat_ratio)

    effective_gun_damage = gun_damage + seaangel_flat_dmg

    # DPS uses effective gun damage
    total_dmg = effective_gun_damage * (1 + potion_dmg + a.dmg_mult + r_dmg + sp_dmg)
    base_dps = effective_gun_damage / gun_cd
    true_dps = total_dmg / total_cd

    # Dive stats: flat first, then % multipliers
    total_oxygen = tube_oxygen * (1 + a.oxygen_mult + r_oxy + potion_oxy + sp_oxy)
    total_speed = (tube_speed + a.speed_flat) * (1 + r_spd + potion_spd + sp_spd)
    total_weight = (tube_weight + a.weight_flat) * (1 + r_wgt + potion_wgt + sp_wgt)

    debug = {
        "race_passive_used": r_first,
        "gun_damage": gun_damage,
        "gun_cd": gun_cd,
        "artifact_dmg_mult_sum": a.dmg_mult,
        "artifact_cd_sum": a.cd_sec,
        "race_dmg_mult": r_dmg,
        "race_cd": r_cd,
        "special_cd": sp_cd,
        "potion_dmg_mult": potion_dmg,
        "special_dmg_mult": sp_dmg,
        "total_cd": total_cd,
        "total_dmg": total_dmg,
        "tube_oxygen_base": tube_oxygen,
        "tube_speed_base": tube_speed,
        "tube_weight_base": tube_weight,
        "artifact_oxygen_mult_sum": a.oxygen_mult,
        "race_oxygen_mult": r_oxy,
        "potion_oxygen_mult": potion_oxy,
        "special_oxygen_mult": sp_oxy,
        "artifact_speed_flat_sum": a.speed_flat,
        "race_speed_mult": r_spd,
        "potion_speed_mult": potion_spd,
        "special_speed_mult": sp_spd,
        "artifact_weight_flat_sum": a.weight_flat,
        "race_weight_mult": r_wgt,
        "potion_weight_mult": potion_wgt,
        "special_weight_mult": sp_wgt,
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


# ==================== Import/Export (save EVERYTHING) ====================
BUILD_VERSION = 2


def export_build_dict() -> dict:
    gun = st.session_state.get("gun_select")
    tube = st.session_state.get("tube_select")
    race = st.session_state.get("race_select")
    use_manual = bool(st.session_state.get("use_manual", False))

    # manual exact values
    manual = {
        "enabled": use_manual,
        "gun_damage": float(st.session_state.get("manual_gun_damage", 0.0)) if use_manual else None,
        "gun_cd": float(st.session_state.get("manual_gun_cd", 0.0)) if use_manual else None,
        "tube_oxygen": float(st.session_state.get("manual_tube_oxygen", 0.0)) if use_manual else None,
        "tube_speed": float(st.session_state.get("manual_tube_speed", 0.0)) if use_manual else None,
        "tube_weight": float(st.session_state.get("manual_tube_weight", 0.0)) if use_manual else None,
    }

    potions = list(st.session_state.get("potions_multi", []))

    # race special checkbox states (save all known special keys)
    special_states = {}
    for rname, opts in RACE_SPECIALS.items():
        for opt in opts:
            k = opt["key"]
            if k in st.session_state:
                special_states[k] = bool(st.session_state[k])

    def export_artifact_slot(slot: str, name_key: str) -> dict:
        art_name = st.session_state.get(name_key)
        if art_name == "(none)":
            art_name = None

        fields = {}
        for field in ART_FIELD_DEFS.keys():
            mk = f"{slot}_{field}_mode"
            tk = f"{slot}_{field}_tier"
            vk = f"{slot}_{field}_value"

        # Only export if any relevant key exists
        if mk in st.session_state or tk in st.session_state or vk in st.session_state:
            mode_val = st.session_state.get(mk)
            tier_val = st.session_state.get(tk)
            raw_val = st.session_state.get(vk)

            fields[field] = {
                "mode": str(mode_val) if mode_val is not None else None,
                "tier": int(tier_val) if tier_val is not None else None,
                "value": float(raw_val) if raw_val is not None else None,
            }

        return {"slot": slot, "name": art_name, "fields": fields}

    return {
        "version": BUILD_VERSION,
        "gun": gun,
        "tube": tube,
        "race": race,
        "manual": manual,
        "potions": potions,
        "race_specials": special_states,
        "artifacts": [
            export_artifact_slot("A1", "a1_select"),
            export_artifact_slot("A2", "a2_select"),
            export_artifact_slot("A3", "a3_select"),
        ],
    }


def apply_import_dict(data: dict):
    # selections
    if "gun" in data:
        st.session_state["gun_select"] = data["gun"]
    if "tube" in data:
        st.session_state["tube_select"] = data["tube"]
    if "race" in data:
        st.session_state["race_select"] = data["race"]

    # manual overrides
    manual = data.get("manual", {}) or {}
    st.session_state["use_manual"] = bool(manual.get("enabled", False))
    if st.session_state["use_manual"]:
        st.session_state["manual_gun_damage"] = float(manual.get("gun_damage", 0.0) or 0.0)
        st.session_state["manual_gun_cd"] = float(manual.get("gun_cd", 0.0) or 0.0)
        st.session_state["manual_tube_oxygen"] = float(manual.get("tube_oxygen", 0.0) or 0.0)
        st.session_state["manual_tube_speed"] = float(manual.get("tube_speed", 0.0) or 0.0)
        st.session_state["manual_tube_weight"] = float(manual.get("tube_weight", 0.0) or 0.0)

    # potions
    st.session_state["potions_multi"] = list(data.get("potions", []) or [])

    # race specials
    rs = data.get("race_specials", {}) or {}
    for k, v in rs.items():
        st.session_state[k] = bool(v)

    # artifacts
    arts = data.get("artifacts", []) or []
    slot_to_select_key = {"A1": "a1_select", "A2": "a2_select", "A3": "a3_select"}

    for a in arts:
        slot = a.get("slot")
        if slot not in slot_to_select_key:
            continue
        st.session_state[slot_to_select_key[slot]] = a.get("name") or "(none)"

        fields = a.get("fields", {}) or {}
        for field, payload in fields.items():
            if field not in ART_FIELD_DEFS:
                continue
            mk = f"{slot}_{field}_mode"
            tk = f"{slot}_{field}_tier"
            vk = f"{slot}_{field}_value"
            if "mode" in payload and payload["mode"] is not None:
                st.session_state[mk] = payload["mode"]
            if "tier" in payload and payload["tier"] is not None:
                st.session_state[tk] = int(payload["tier"])
            if "value" in payload and payload["value"] is not None:
                st.session_state[vk] = float(payload["value"])


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

# defaults
def ensure_default(key: str, value: Any):
    if key not in st.session_state:
        st.session_state[key] = value

ensure_default("gun_select", sorted(guns_by.keys())[0])
ensure_default("tube_select", sorted(tubes_by.keys())[0])
ensure_default("race_select", sorted(races_by.keys())[0])
ensure_default("a1_select", "(none)")
ensure_default("a2_select", "(none)")
ensure_default("a3_select", "(none)")
ensure_default("use_manual", False)
ensure_default("potions_multi", [])

# ----- Sidebar import/export
with st.sidebar:
    st.header("Build Import / Export")

    export_json = json.dumps(export_build_dict(), indent=2)
    st.download_button(
        "⬇️ Export build.json",
        data=export_json,
        file_name="abyss_build.json",
        mime="application/json",
        use_container_width=True,
    )

    up = st.file_uploader("Import build.json", type=["json"])
    if up is not None:
        try:
            imported = json.load(up)
            st.success("Loaded JSON.")
            if st.button("✅ Apply imported build", use_container_width=True):
                apply_import_dict(imported)
                st.rerun()
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    with st.expander("Copy/paste JSON"):
        st.text_area("Build JSON", value=export_json, height=240)

    st.divider()
    st.caption("Optional: create artifact_tier_overrides.json to force exact tier values for certain artifacts.")


# ----- Main selection UI
col1, col2, col3 = st.columns(3)

with col1:
    st.selectbox("Gun", sorted(guns_by.keys()), key="gun_select")
    st.selectbox("Tube", sorted(tubes_by.keys()), key="tube_select")

with col2:
    st.selectbox("Race", sorted(races_by.keys()), key="race_select")

    # Potions: allow multiple, stacking
    # You can restrict options if you want (e.g., only certain items).
    potion_options = sorted(items_by.keys())
    st.multiselect("Potions / Shards (stacking)", potion_options, key="potions_multi")

with col3:
    st.markdown("**Manual overrides**")
    st.checkbox("Override gun/tube base stats", key="use_manual")

gun = guns_by[st.session_state["gun_select"]]
tube = tubes_by[st.session_state["tube_select"]]
race = races_by[st.session_state["race_select"]]

# base values from JSON
base_gun_damage = float(gun["Damage"])
base_gun_cd = parse_seconds(gun["Cooldown"])
base_tube_oxygen = float(tube["Oxygen"])
base_tube_speed = float(tube["Speed"])
base_tube_weight = float(tube["Weight"])

# manual inputs (exact values used)
if st.session_state["use_manual"]:
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        ensure_default("manual_gun_damage", base_gun_damage)
        gun_damage = st.number_input("Gun Damage", value=float(st.session_state["manual_gun_damage"]), step=1.0, key="manual_gun_damage")
    with m2:
        ensure_default("manual_gun_cd", base_gun_cd)
        gun_cd = st.number_input("Gun Cooldown (s)", value=float(st.session_state["manual_gun_cd"]), step=0.01, key="manual_gun_cd")
    with m3:
        ensure_default("manual_tube_oxygen", base_tube_oxygen)
        tube_oxygen = st.number_input("Tube Oxygen", value=float(st.session_state["manual_tube_oxygen"]), step=1.0, key="manual_tube_oxygen")
    with m4:
        ensure_default("manual_tube_speed", base_tube_speed)
        tube_speed = st.number_input("Tube Speed", value=float(st.session_state["manual_tube_speed"]), step=1.0, key="manual_tube_speed")
    with m5:
        ensure_default("manual_tube_weight", base_tube_weight)
        tube_weight = st.number_input("Tube Weight", value=float(st.session_state["manual_tube_weight"]), step=1.0, key="manual_tube_weight")
else:
    gun_damage = base_gun_damage
    gun_cd = base_gun_cd
    tube_oxygen = base_tube_oxygen
    tube_speed = base_tube_speed
    tube_weight = base_tube_weight

# Race specials (dynamic checkboxes)
race_specials_effects, race_special_states = get_race_special_effects(st.session_state["race_select"])

# Potions combined effects
selected_potions = [items_by[name] for name in st.session_state["potions_multi"] if name in items_by]
potions_effects = combine_item_effects(selected_potions)

st.divider()
st.subheader("Artifacts (3 slots, duplicates allowed)")
st.caption("Each artifact stat: choose Tier slider (1–7) OR exact value input. Export saves exact values used.")

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
    potions_effects=potions_effects,
    race_specials=race_specials_effects,
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
    st.write("Race specials enabled:", race_special_states if race_special_states else "(none)")
    st.write("Potions selected:", st.session_state["potions_multi"] if st.session_state["potions_multi"] else "(none)")
    st.write("Potions combined effects:", potions_effects)
    st.write("Race specials effects applied:", race_specials_effects)

    st.write("Artifacts chosen + exact values used:")
    st.json({"A1": a1_dbg, "A2": a2_dbg, "A3": a3_dbg})

    st.markdown("### DPS calculations")
    st.code(
        f"""TotalCD = gunCD + artifactCDsum + raceCD + specialCD
= {dbg['gun_cd']:.4f} + {dbg['artifact_cd_sum']:.4f} + {dbg['race_cd']:.4f} + {dbg['special_cd']:.4f}
= {dbg['total_cd']:.4f}

TotalDMG = gunDMG * (1 + potionDMG + artifactDMG + raceDMG + specialDMG)
= {dbg['gun_damage']:.4f} * (1 + {dbg['potion_dmg_mult']:.4f} + {dbg['artifact_dmg_mult_sum']:.4f} + {dbg['race_dmg_mult']:.4f} + {dbg['special_dmg_mult']:.4f})
= {dbg['total_dmg']:.4f}

BaseDPS = {result['Base DPS']:.4f}
TrueDPS = {result['True DPS']:.4f}
""",
        language="text",
    )

    st.markdown("### Dive stat calculations (flat then %)")
    st.code(
        f"""Oxygen = tubeOxygen * (1 + artOxy + raceOxy + potionOxy + specialOxy)
= {dbg['tube_oxygen_base']:.4f} * (1 + {dbg['artifact_oxygen_mult_sum']:.4f} + {dbg['race_oxygen_mult']:.4f} + {dbg['potion_oxygen_mult']:.4f} + {dbg['special_oxygen_mult']:.4f})
= {result['Oxygen']:.4f}

Speed = (tubeSpeed + artSpeedFlat) * (1 + raceSpeed + potionSpeed + specialSpeed)
= ({dbg['tube_speed_base']:.4f} + {dbg['artifact_speed_flat_sum']:.4f}) * (1 + {dbg['race_speed_mult']:.4f} + {dbg['potion_speed_mult']:.4f} + {dbg['special_speed_mult']:.4f})
= {result['Speed']:.4f}

Weight = (tubeWeight + artWeightFlat) * (1 + raceWeight + potionWeight + specialWeight)
= ({dbg['tube_weight_base']:.4f} + {dbg['artifact_weight_flat_sum']:.4f}) * (1 + {dbg['race_weight_mult']:.4f} + {dbg['potion_weight_mult']:.4f} + {dbg['special_weight_mult']:.4f})
= {result['Weight']:.4f}
""",
        language="text",
    )
