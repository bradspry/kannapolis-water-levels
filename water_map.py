"""
Fetches live water data and renders a PNG map of Kannapolis/Concord water sources.

Sources:
  - City lakes:        concordnc.gov/Departments/Water-Resources/Lake-Level-Data
  - USGS stream gauges: waterservices.usgs.gov (NWIS instantaneous values)
  - Cube Hydro lakes:  ww4.cubecarolinas.com/lake/levels?orgID=3 (hourly)
  - NOAA lakes:        api.water.noaa.gov/nwps/v1/gauges (pool elevation, hourly)

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License v3.0. See LICENSE.
"""

import warnings
import sys
import re
from datetime import date

import numpy as np
import requests
from bs4 import BeautifulSoup
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.path import Path
from matplotlib.patches import PathPatch
import contextily as ctx
from pyproj import Transformer
import db

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# City-managed lakes (WGS-84 lat/lon, label_side: "above" | "below")
# ---------------------------------------------------------------------------
LAKES = {
    "Howell/Coddle Creek": {"lat": 35.445188, "lon": -80.705149, "label": "left"},
    "Fisher":              {"lat": 35.490328, "lon": -80.578603, "label": "above"},
    "Concord":             {"lat": 35.479678, "lon": -80.588138, "label": "below"},
    "Kannapolis Lake":     {"display": "Kannapolis Lake", "lat": 35.512083, "lon": -80.646457, "label": "left"},
}

# ---------------------------------------------------------------------------
# USGS stream gauges  (coordinates hardcoded; only flow is fetched live)
# ---------------------------------------------------------------------------
USGS_SITES = {
    "Second Creek": {
        "site_no": "02120780",
        "lat": 35.717611,
        "lon": -80.596075,
        "label": "left",
    },
    "Yadkin River": {
        "site_no": "02116500",
        "lat": 35.856667,
        "lon": -80.386944,
        "label": "below",
    },
}

# ---------------------------------------------------------------------------
# Cube Hydro Carolinas reservoirs (Yadkin River chain), keyed by HTML name
# ---------------------------------------------------------------------------
CUBE_LAKES = {
    "High Rock": {
        "display": "High Rock Lake",
        "lat": 35.668,  "lon": -80.308,
        "label": "right",
    },
    "Tuckertown": {
        "display": "Tuckertown Reservoir",
        "lat": 35.542,  "lon": -80.198,
        "label": "right",
    },
    "Badin (Narrows)": {
        "display": "Badin Lake (Narrows)",
        "lat": 35.460,  "lon": -80.114,
        "label": "below",
    },
}

# ---------------------------------------------------------------------------
# NOAA lake gauges (Catawba River chain), keyed by NWSLI gauge ID
# full_pool: pool elevation (ft) at which water spills over the uncontrolled spillway
# ---------------------------------------------------------------------------
NOAA_LAKES = {
    "LKSN7": {
        "display":   "Lookout Shoals Lake",
        "full_pool": 100.0,
        "lat": 35.757739, "lon": -81.089201,
        "label": "above",
    },
    "CWAN7": {
        "display":   "Lake Norman",
        "full_pool": 100.0,
        "lat": 35.434722, "lon": -80.958333,
        "label": "above",
    },
    "MOUN7": {
        "display":   "Mountain Island Lake",
        "full_pool": 100.0,
        "lat": 35.3338, "lon": -80.9868,
        "label": "right",
    },
}

LAKE_URL       = "https://concordnc.gov/Departments/Water-Resources/Lake-Level-Data"
CUBE_HYDRO_URL = "https://ww4.cubecarolinas.com/lake/levels?orgID=3"
NOAA_GAUGE_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
USGS_SITE_URL  = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV_URL    = "https://waterservices.usgs.gov/nwis/iv/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

PIN_COLOR     = "#1a7abf"
NO_DATA_COLOR = "#cc2200"


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_lake_levels() -> tuple[dict[str, str], str]:
    """Scrape current lake levels from the City of Concord website."""
    resp = requests.get(LAKE_URL, headers=HEADERS, timeout=20, verify=False)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)

    date_match = re.search(r"Levels updated\s+(\d{2}/\d{2}/\d{4})", text)
    updated = date_match.group(1) if date_match else "unknown"

    level_pattern = re.compile(
        r"(Howell/Coddle Creek|Fisher|Concord)\s*\n\s*([^\n]*(?:Below Full|Full|Above Full)[^\n]*)",
        re.IGNORECASE,
    )
    levels = {k.strip(): v.strip() for k, v in level_pattern.findall(text)}

    if not levels:
        print("WARNING: Could not parse lake levels from page.")

    return levels, updated


def _parse_usgs_stats(rdb_text: str, today: date) -> dict[str, dict]:
    """Parse USGS RDB stats response into {site_no: {p10, p25, p50, p75, p90, ...}}."""
    result: dict[str, dict] = {}
    for line in rdb_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 24 or parts[0] != "USGS" or parts[2] != "00060":
            continue
        try:
            if int(parts[5]) != today.month or int(parts[6]) != today.day:
                continue
        except ValueError:
            continue
        def _f(v):
            try: return float(v)
            except (ValueError, TypeError): return None
        result[parts[1]] = {
            "p05": _f(parts[15]), "p10": _f(parts[16]),
            "p20": _f(parts[17]), "p25": _f(parts[18]),
            "p50": _f(parts[19]), "p75": _f(parts[20]),
            "p80": _f(parts[21]), "p90": _f(parts[22]),
            "p95": _f(parts[23]),
        }
    return result


def _interpolate_percentile(value: float, pcts: dict) -> float | None:
    """Linearly interpolate where `value` falls in the historical percentile distribution."""
    known = [
        (5,  pcts.get("p05")), (10, pcts.get("p10")),
        (20, pcts.get("p20")), (25, pcts.get("p25")),
        (50, pcts.get("p50")), (75, pcts.get("p75")),
        (80, pcts.get("p80")), (90, pcts.get("p90")),
        (95, pcts.get("p95")),
    ]
    known = [(p, v) for p, v in known if v is not None]
    if not known:
        return None
    pct_list = [p for p, _ in known]
    val_list = [v for _, v in known]
    if value <= val_list[0]:
        return float(pct_list[0])
    if value >= val_list[-1]:
        return float(pct_list[-1])
    for i in range(len(val_list) - 1):
        if val_list[i] <= value <= val_list[i + 1]:
            frac = (value - val_list[i]) / (val_list[i + 1] - val_list[i])
            return pct_list[i] + frac * (pct_list[i + 1] - pct_list[i])
    return None


def fetch_usgs_gauges() -> dict[str, dict]:
    """
    Fetch live CFS and historical percentile rank for all USGS_SITES.

    Returns dict keyed by display name:
      {"lat", "lon", "site_no", "flow_cfs", "percentile", "label"}
    """
    site_nos = ",".join(s["site_no"] for s in USGS_SITES.values())
    today = date.today()

    # Live instantaneous CFS
    iv_resp = requests.get(USGS_IV_URL, params={
        "sites": site_nos, "format": "json", "period": "PT2H",
        "parameterCd": "00060",
    }, timeout=30)
    iv_resp.raise_for_status()

    flows: dict[str, float | None] = {}
    for ts in iv_resp.json()["value"]["timeSeries"]:
        site_no = ts["sourceInfo"]["siteCode"][0]["value"]
        vals = ts["values"][0]["value"]
        flows[site_no] = float(vals[-1]["value"]) if vals else None

    # Historical daily percentiles for today's calendar date
    stat_resp = requests.get("https://waterservices.usgs.gov/nwis/stat/", params={
        "sites": site_nos, "statReportType": "daily",
        "statType": "all", "parameterCd": "00060", "format": "rdb",
    }, timeout=30)
    stat_resp.raise_for_status()
    hist = _parse_usgs_stats(stat_resp.text, today)

    result = {}
    for name, cfg in USGS_SITES.items():
        sno = cfg["site_no"]
        flow = flows.get(sno)
        pcts = hist.get(sno, {})
        pct = _interpolate_percentile(flow, pcts) if flow is not None else None
        result[name] = {
            "lat": cfg["lat"], "lon": cfg["lon"],
            "site_no": sno,
            "flow_cfs": flow,
            "percentile": pct,
            "label": cfg["label"],
        }
    return result


def fetch_cube_hydro() -> tuple[dict[str, dict], str]:
    """
    Scrape hourly lake elevation and ft-below-full from Cube Hydro Carolinas.

    Returns (data, timestamp) where data is keyed by HTML lake name:
      {"display": str, "lat": float, "lon": float,
       "elevation_ft": float, "ft_below_full": float, "label": str}
    """
    resp = requests.get(CUBE_HYDRO_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    ts_el = soup.find(id="FormView1_timestmpLabel")
    timestamp = ts_el.get_text(strip=True) if ts_el else "unknown"

    table = soup.find(id="GridView1")
    data: dict[str, dict] = {}
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        html_name = cells[0].get_text(strip=True)
        if html_name not in CUBE_LAKES:
            continue
        cfg = CUBE_LAKES[html_name]
        data[html_name] = {
            **cfg,
            "elevation_ft":  float(cells[1].get_text(strip=True)),
            "ft_below_full": float(cells[2].get_text(strip=True)),
        }

    return data, timestamp


def fetch_noaa_lakes() -> dict[str, dict]:
    """
    Fetch pool elevations from NOAA NWPS for Catawba River reservoir gauges.

    Returns dict keyed by NWSLI (e.g. "LKSN7"):
      {"display", "full_pool", "lat", "lon", "label",
       "elevation_ft", "ft_below_full"}
    """
    result = {}
    for lid, cfg in NOAA_LAKES.items():
        resp = requests.get(f"{NOAA_GAUGE_URL}/{lid}", timeout=15)
        resp.raise_for_status()
        obs = resp.json()["status"]["observed"]
        pool_ft = obs["primary"] if obs["primary"] != -999 else None
        ft_below = round(cfg["full_pool"] - pool_ft, 2) if pool_ft is not None else None
        result[lid] = {
            **cfg,
            "elevation_ft":  pool_ft,
            "ft_below_full": ft_below,
        }
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_mercator(lat: float, lon: float) -> tuple[float, float]:
    """Project WGS-84 lat/lon to Web Mercator (EPSG:3857) for plotting on the basemap."""
    return TRANSFORMER.transform(lon, lat)


def trend_label(delta: float | None, unit: str = "ft", higher_is_better: bool = False) -> str:
    """
    Format a trend line: arrow + magnitude.
    higher_is_better=False  → positive delta means level dropped (bad) → ↓
    higher_is_better=True   → positive delta means value rose (good) → ↑
    """
    if delta is None:
        return ""
    magnitude = abs(delta)
    threshold = 10.0 if unit == "cfs" else 0.1
    if magnitude < threshold:
        return "→ steady"
    rising = (delta < 0) if not higher_is_better else (delta > 0)
    if unit == "cfs":
        amount = f"{magnitude:.0f} cfs"
    else:
        amount = f"{magnitude:.2f} ft"
    return f"↑ {amount}" if rising else f"↓ {amount}"


def inches_to_ft_label(level_text: str) -> str:
    """Convert city lake inch-based level text to feet for consistent display."""
    text = level_text.strip().lower()
    if text == "no data":
        return "No data"
    if text == "full":
        return "Full"
    m = re.search(r"([\d.]+)", text)
    if not m:
        return level_text
    feet = float(m.group(1)) / 12
    direction = "Above" if "above" in text else "Below"
    return f"{feet:.2f} ft {direction} Full"




def lake_color(level_text: str) -> str:
    """Shade city lake pins by inches below full (darker = lower)."""
    text = level_text.lower()
    if text.strip() == "full":
        return "#1a7abf"
    m = re.search(r"([\d.]+)", text)
    if m:
        inches = float(m.group(1))
        if "above" in text:
            return "#0d4f8b"
        if inches < 12:
            return "#4da6e8"
        elif inches < 36:
            return "#89c4f0"
        else:
            return "#c2dff5"
    return "#aaaaaa"


def stream_color(percentile: float | None) -> str:
    """USGS standard streamflow condition colors by historical percentile."""
    if percentile is None:
        return "#aaaaaa"
    if percentile < 10:
        return "#b31a1a"   # red    — much below normal
    elif percentile < 25:
        return "#e07b39"   # orange — below normal
    elif percentile < 75:
        return "#3cb371"   # green  — normal
    elif percentile < 90:
        return "#4da6e8"   # blue   — above normal
    else:
        return "#1a4fbf"   # dark blue — much above normal


def cube_lake_color(ft_below: float) -> str:
    """Teal shades by feet below full pond (Cube Hydro reservoirs)."""
    if ft_below <= 0:
        return "#006064"
    elif ft_below < 2:
        return "#00838f"
    elif ft_below < 5:
        return "#26c6da"
    elif ft_below < 10:
        return "#80deea"
    else:
        return "#b2ebf2"


def draw_pin(ax, x, y, color, size=1_100, zorder=5):
    """Google Maps-style teardrop pin with tip at (x, y)."""
    r = size * 0.36
    head_y = y + r * 2.1
    d = head_y - y
    alpha = np.arccos(r / d)

    right_angle = -np.pi / 2 + alpha
    left_angle  = -np.pi / 2 - alpha

    arc = np.linspace(right_angle, left_angle + 2 * np.pi, 64)
    arc_x = x + r * np.cos(arc)
    arc_y = head_y + r * np.sin(arc)

    verts = list(zip(arc_x, arc_y)) + [(x, y), (arc_x[0], arc_y[0])]
    codes = [Path.MOVETO] + [Path.LINETO] * (len(verts) - 2) + [Path.CLOSEPOLY]

    shadow = [(vx + r * 0.12, vy - r * 0.12) for vx, vy in verts]
    ax.add_patch(PathPatch(Path(shadow, codes), fc="#00000033", ec="none", zorder=7))
    ax.add_patch(PathPatch(Path(verts, codes), fc=color, ec="white", lw=2, zorder=8))
    ax.add_patch(mpatches.Circle((x, head_y), radius=r * 0.38, color="white", zorder=9))


def add_label(ax, x, y, text, color, side="above", pin_size=5_000):
    """Draw a label with a leader line in one of four directions from the pin."""
    pin_height = pin_size * 0.36 * 2.1 + pin_size * 0.36
    pin_mid_y  = y + pin_height * 0.5
    pin_top_y  = y + pin_height
    V_OFFSET = 8_000   # vertical clearance (above/below)
    H_OFFSET = 14_000  # horizontal clearance (left/right — text is wider than tall)

    if side == "above":
        lx, ly  = x,             pin_top_y + V_OFFSET
        ann_xy  = (x,             pin_top_y)
    elif side == "below":
        lx, ly  = x,             y - V_OFFSET
        ann_xy  = (x,             y)
    elif side == "left":
        lx, ly  = x - H_OFFSET,  pin_mid_y
        ann_xy  = (x,             pin_mid_y)
    else:  # right
        lx, ly  = x + H_OFFSET,  pin_mid_y
        ann_xy  = (x,             pin_mid_y)

    ax.annotate(
        text,
        xy=ann_xy, xytext=(lx, ly),
        ha="center", va="center",
        fontsize=10, fontweight="bold",
        color="#1a1a1a", linespacing=1.5,
        bbox=dict(boxstyle="round,pad=0.4", fc="white", ec=color, lw=2, alpha=0.92),
        arrowprops=dict(arrowstyle="-", color=color, lw=1.5),
        zorder=5,
    )


# ---------------------------------------------------------------------------
# Map builder
# ---------------------------------------------------------------------------

def build_map(
    lake_levels: dict[str, str],
    lake_updated: str,
    usgs_data: dict[str, dict],
    cube_data: dict[str, dict],
    cube_updated: str,
    output: str = "water_map.png",
):
    """Render all pins/labels over an OSM basemap and save the composed PNG."""
    # Collect all coordinates
    all_coords: dict[str, tuple[float, float]] = {}
    for name, info in LAKES.items():
        all_coords[name] = to_mercator(info["lat"], info["lon"])
    for name, info in usgs_data.items():
        if info["lat"] is not None:
            all_coords[name] = to_mercator(info["lat"], info["lon"])
    for name, info in cube_data.items():
        all_coords[name] = to_mercator(info["lat"], info["lon"])

    xs = [c[0] for c in all_coords.values()]
    ys = [c[1] for c in all_coords.values()]

    x_min, x_max = min(xs) - 12_000, max(xs) + 12_000
    y_min, y_max = min(ys) - 18_000, max(ys) + 6_000

    fig, ax = plt.subplots(figsize=(14, 14))
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.set_aspect("equal")

    ctx.add_basemap(ax, crs="EPSG:3857", source=ctx.providers.OpenStreetMap.Mapnik, zoom=12)

    # Draw lakes
    for name, info in LAKES.items():
        x, y = all_coords[name]
        level_text = lake_levels.get(name, "No data")
        display_name = info.get("display", f"Lake {name}")
        # A parseable level has a digit or is exactly "Full"
        has_level = level_text.lower() == "full" or bool(re.search(r"[\d.]+", level_text))
        level_label = inches_to_ft_label(level_text) if has_level else level_text
        color = PIN_COLOR if has_level else NO_DATA_COLOR
        if has_level and level_text != "No data":
            _delta_in = db.lake_trend(name)
            trend = trend_label(_delta_in / 12 if _delta_in is not None else None, unit="ft", higher_is_better=False)
        else:
            trend = ""
        label_text = f"{display_name}\n{level_label}" + (f"\n{trend}" if trend else "")
        draw_pin(ax, x, y, color, size=5_000)
        add_label(ax, x, y, label_text, color, side=info["label"])

    # Draw USGS stream gauges
    for name, info in usgs_data.items():
        if name not in all_coords:
            continue
        x, y = all_coords[name]
        flow = info["flow_cfs"]
        pct  = info.get("percentile")
        if pct is not None:
            if pct < 10:
                condition = "Well Below Full Flow"
            elif pct < 25:
                condition = "Below Full Flow"
            elif pct < 75:
                condition = "At Full Flow"
            elif pct < 90:
                condition = "Above Full Flow"
            else:
                condition = "Well Above Full Flow"
            flow_label = f"{flow:.0f} cfs · {condition}" if flow is not None else condition
        elif flow is not None:
            flow_label = f"{flow:.0f} cfs"
        else:
            flow_label = "No data"
        color = NO_DATA_COLOR if flow is None else PIN_COLOR
        # flow_cfs: positive delta = more flow = good (higher_is_better=True)
        trend = trend_label(db.stream_trend(name), unit="cfs", higher_is_better=True)
        label_text = f"{name}\n{flow_label}" + (f"\n{trend}" if trend else "")
        draw_pin(ax, x, y, color, size=5_000)
        add_label(ax, x, y, label_text, color, side=info["label"])

    # Draw Cube Hydro reservoirs
    for name, info in cube_data.items():
        if name not in all_coords:
            continue
        x, y = all_coords[name]
        ft = info["ft_below_full"]
        level_label = f"{ft:.2f} ft Below Full" if ft is not None and ft > 0 else ("Full" if ft is not None else "No data")
        color = NO_DATA_COLOR if ft is None else PIN_COLOR
        # ft_below_full: positive delta = dropped = bad (higher_is_better=False)
        trend = trend_label(db.reservoir_trend(info["display"]), unit="ft", higher_is_better=False)
        label_text = f"{info['display']}\n{level_label}" + (f"\n{trend}" if trend else "")
        draw_pin(ax, x, y, color, size=5_000)
        add_label(ax, x, y, label_text, color, side=info["label"])


    ax.set_title(
        f"Kannapolis / Concord Water Sources\n"
        f"City lakes: {lake_updated}  ·  Cube Hydro: {cube_updated}  ·  NOAA/USGS: live",
        fontsize=11, fontweight="bold", pad=12,
    )
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Map saved to {output}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def prompt_kannapolis_lake() -> str:
    """
    Ask the user for Kannapolis Lake's current level and normalize it to the
    same display format as the city lakes, e.g. '93.0\" Below Full'.
    Returns 'No data' if the user skips.
    """
    raw = input("\nKannapolis Lake level (e.g. '93 inches below full pond', or press Enter to skip): ").strip()
    if not raw:
        return "No data"
    text = raw.lower()
    if re.search(r'\bfull\b', text) and not re.search(r'(above|below)', text):
        return "Full"
    m = re.search(r"([\d.]+)", text)
    if not m:
        return raw  # can't parse — store as-is
    val = float(m.group(1))
    if "above" in text:
        return f'{val:.1f}" Above Full'
    return f'{val:.1f}" Below Full'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Fetch all four data sources, persist readings, then build the map PNG."""
    db.init_db()

    print("Fetching city lake levels from concordnc.gov …")
    lake_levels, lake_updated = fetch_lake_levels()
    for name, level in lake_levels.items():
        print(f"  {name}: {level}  (updated {lake_updated})")

    lake_levels["Kannapolis Lake"] = prompt_kannapolis_lake()
    print(f"  Kannapolis Lake: {lake_levels['Kannapolis Lake']}")

    print("Fetching USGS stream gauge data …")
    usgs_data = fetch_usgs_gauges()
    for name, info in usgs_data.items():
        print(f"  {name}: {info['flow_cfs']} cfs")

    print("Fetching Cube Hydro reservoir levels …")
    cube_data, cube_updated = fetch_cube_hydro()
    for name, info in cube_data.items():
        print(f"  {info['display']}: {info['elevation_ft']} ft elev, {info['ft_below_full']} ft below full")

    print("Fetching NOAA reservoir levels …")
    noaa_data = fetch_noaa_lakes()
    for lid, info in noaa_data.items():
        print(f"  {info['display']}: {info['elevation_ft']} ft elev, {info['ft_below_full']} ft below full")

    all_reservoirs = {**cube_data, **noaa_data}

    print("Storing readings …")
    db.store_lake_readings(lake_levels)
    db.store_stream_readings(usgs_data)
    db.store_cube_readings(all_reservoirs)
    db.summary()

    output = sys.argv[1] if len(sys.argv) > 1 else "water_map.png"
    build_map(lake_levels, lake_updated, usgs_data, all_reservoirs, cube_updated, output)


if __name__ == "__main__":
    main()
