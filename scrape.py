import pandas as pd
import requests
from io import StringIO
import re

HEADERS = {"User-Agent": "Mozilla/5.0"}

URLS = {
    "guns": "https://abysswiki.com/guns/",
    "tubes": "https://abysswiki.com/tubes/",
    "artifacts": "https://abysswiki.com/artifacts/",
    "items": "https://abysswiki.com/items/",
    "races": "https://abysswiki.com/races/",
}

def fetch_table(url):
    html = requests.get(url, headers=HEADERS, timeout=30).text
    return pd.read_html(StringIO(html), flavor="lxml")[0]

def save(df, name):
    df.to_csv(f"{name}.csv", index=False)
    df.to_json(f"{name}.json", orient="records", indent=2)

# -------------- Effects Parser --------------

STAT_MAP = {
    "speed": "speed",
    "damage": "damage",
    "oxygen": "oxygen",
    "max depth": "max_depth",
    "depth": "max_depth",
    "weight": "weight",
    "cooldown": "cooldown",
    "control": "control",
    "cost": "cost",
}

def parse_effects(effects: str):
    """
    Parses strings like:
        "Speed +0.75 → +1.5 Damage +1% → +1.6%"
    Returns a list of modifier dicts:
    """
    if not isinstance(effects, str):
        return []

    s = effects.replace("→", "->").strip()
    pattern = re.compile(
        r"(?P<stat>[A-Za-z ]+?)\s+"
        r"(?P<min>[+\-]?\d+(?:\.\d+)?)\s*(?P<minpct>%?)"
        r"(?:\s*->\s*(?P<max>[+\-]?\d+(?:\.\d+)?)\s*(?P<maxpct>%?))?"
        r"(?=\s+[A-Za-z ]+\s+[+\-]?\d|$)"
    )

    out = []
    for m in pattern.finditer(s):
        raw_stat = m.group("stat").strip().lower()
        raw_stat = re.sub(r"\s+", " ", raw_stat)
        stat = STAT_MAP.get(raw_stat, raw_stat.replace(" ", "_"))

        vmin = float(m.group("min"))
        vmax = float(m.group("max")) if m.group("max") else vmin

        is_pct = (m.group("minpct") == "%") or (m.group("maxpct") == "%")
        mtype = "pct" if is_pct else "flat"

        out.append({"stat": stat, "type": mtype, "min": vmin, "max": vmax})

    return out

def expand_artifacts(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds structured stat columns to artifacts.
    """
    df = df.copy()
    parsed = df["Effects"].apply(parse_effects)
    
    cols = set()
    for mods in parsed:
        for mod in mods:
            base = f"{mod['stat']}_{mod['type']}"
            cols.add(base + "_min")
            cols.add(base + "_max")
            cols.add(base + "_avg")

    for c in sorted(cols):
        df[c] = pd.NA

    for i, mods in enumerate(parsed):
        for mod in mods:
            base = f"{mod['stat']}_{mod['type']}"
            df.at[i, base + "_min"] = mod["min"]
            df.at[i, base + "_max"] = mod["max"]
            df.at[i, base + "_avg"] = (mod["min"] + mod["max"]) / 2

    return df

# -------------- Main --------------

def main():
    # Download tables
    for name, url in URLS.items():
        print(f"Fetching {name} ...")
        df = fetch_table(url)

        # clean header names (remove arrows/symbols)
        df.columns = [c.replace("⇅", "").strip() for c in df.columns]

        # Save raw
        save(df, name)

        # If artifacts, also expand effects
        if name == "artifacts":
            parsed = expand_artifacts(df)
            save(parsed, "artifacts_parsed")

    print("Done — all data saved!")

if __name__ == "__main__":
    main()