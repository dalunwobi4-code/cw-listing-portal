"""
CW Lagos — Property Listing Web Form
Paste WhatsApp description → AI parses it → upload images → post everywhere.
"""

import json
import os
import sys
import threading
import queue
import uuid
import re
import time
from pathlib import Path
from flask import Flask, request, jsonify, Response, render_template

# ── paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "agents"))

UPLOADS_DIR    = Path(__file__).parent / "uploads"
# Logo: bundled in web/static for Railway; falls back to local Downloads for Mac
WATERMARK_LOGO = str(Path(__file__).parent / "static" / "cw_logo.png")
if not Path(WATERMARK_LOGO).exists():
    WATERMARK_LOGO = "/Users/apple/Downloads/CW White Logo.png"

from agents.watermark import apply_watermark
from agents.enhance  import enhance_image
from agents.poster_ppng        import post_property as post_ppng
from agents.poster_propertypro import post_property as post_propertypro
from agents.poster_npc         import post_property as post_npc
from agents.poster_airtable    import post_property as post_airtable

# ── Credentials: env vars (Railway) with fallback to local orchestrate.py ───
def _get_credentials():
    if os.environ.get("AIRTABLE_TOKEN"):
        return {
            "ppng":        {"email": os.environ.get("PPNG_EMAIL", ""),             "password": os.environ.get("PPNG_PASSWORD", "")},
            "propertypro": {"email": os.environ.get("PROPERTYPRO_EMAIL", ""),      "password": os.environ.get("PROPERTYPRO_PASSWORD", "")},
            "npc_daniel":  {"email": os.environ.get("NPC_DANIEL_EMAIL", ""),       "password": os.environ.get("NPC_DANIEL_PASSWORD", "")},
            "npc_cw":      {"email": os.environ.get("NPC_CW_EMAIL", ""),           "password": os.environ.get("NPC_CW_PASSWORD", "")},
            "airtable":    {"token": os.environ.get("AIRTABLE_TOKEN", "")},
        }
    try:
        from orchestrate import CREDENTIALS as _C
        return _C
    except ImportError:
        return {"ppng": {}, "propertypro": {}, "npc_daniel": {}, "npc_cw": {}, "airtable": {}}

CREDENTIALS = _get_credentials()

# Optional residential proxy
_PROXY_URL = os.environ.get("PROXY_URL", "")
if _PROXY_URL:
    for _p in ("ppng", "propertypro", "npc_daniel", "npc_cw"):
        CREDENTIALS.setdefault(_p, {})["proxy_url"] = _PROXY_URL

import requests as req
try:
    import anthropic as _anthropic_sdk
    # env var always wins over orchestrate.py so key rotations take effect immediately
    _ANT_KEY = (os.environ.get("ANTHROPIC_API_KEY", "")
                or CREDENTIALS.get("anthropic", {}).get("api_key", ""))
    print(f"[INIT] _ANT_KEY resolved: {_ANT_KEY[:30] if _ANT_KEY else 'EMPTY'}...", flush=True)
    _ANTHROPIC_CLIENT = _anthropic_sdk.Anthropic(api_key=_ANT_KEY) if _ANT_KEY else None
except ImportError:
    _ANTHROPIC_CLIENT = None

PLATFORM_POSTERS = {
    "ppng":        post_ppng,
    "propertypro": post_propertypro,
    "npc_daniel":  post_npc,
    "npc_cw":      post_npc,
    "airtable":    post_airtable,
}
PLATFORM_LABELS = {
    "ppng":        "PP Nigeria",
    "propertypro": "PropertyPro",
    "npc_daniel":  "Daniel NPC",
    "npc_cw":      "CW NPC",
    "airtable":    "Airtable",
}

TEAM_MEMBERS = ["Maryann", "Tumi", "Nancy"]
TEAM_PIN     = "0000"

# ── Cloudinary land card URL builders (ported from cw-framer-sync/land-card.js) ──

import hashlib
from urllib.parse import quote as _url_quote

_CLD_CLOUD    = "lssset6f"
_CLD_BASE     = "land-base"
_CLD_OVERLAY  = "land-overlay-v3"
_CLD_OG_OVERLAY = "land-overlay-og"
_CLD_PAD      = 120


def _land_enc(s: str) -> str:
    encoded = _url_quote(str(s), safe='')
    return encoded.replace('%2C', '%252C').replace('%2F', '%252F')


def _format_land_size(raw: str) -> str:
    """'1400 sqm' / '9,000 sqm' / '600' -> '1,400 sqm'."""
    import re as _re
    s = str(raw or "").strip()
    if not s:
        return ""
    m = _re.match(r'^([\d,]*\.?\d+)\s*(.*)$', s)
    if not m:
        return s
    n = float(m.group(1).replace(",", ""))
    unit = m.group(2).strip()
    if _re.search(r'\d', unit):
        return s
    if not unit or _re.match(r'^(sqm|sq\.?\s?m(eters?|etres?)?|m2|m²|square met(er|re)s?)\.?$', unit, _re.IGNORECASE):
        unit = "sqm"
    return f"{int(n):,} {unit}"


def _clip(s: str, n: int) -> str:
    return s[:n - 1].rstrip() + "…" if len(s) > n else s


def build_land_card_url(fields: dict, base_id: str = _CLD_BASE) -> str:
    title = _clip(str(fields.get("Property Title") or "").strip(), 96)
    size  = _format_land_size(fields.get("Land Size") or "")
    price = str(fields.get("New Price") or "").strip()
    loc_raw = fields.get("Main Neighbourhood Label") or ""
    loc   = str(loc_raw[0] if isinstance(loc_raw, list) else loc_raw).strip()
    ptype_raw = fields.get("Property Type") or ""
    ptype = str(ptype_raw[0] if isinstance(ptype_raw, list) else ptype_raw).strip()
    PAD   = _CLD_PAD
    parts = [
        f"https://res.cloudinary.com/{_CLD_CLOUD}/image/upload",
        "c_fill,w_1536,h_1024,g_auto",
        f"l_{_CLD_OVERLAY},g_north_west,x_0,y_0",
        f"l_text:Montserrat_28_bold_letter_spacing_3:%2520LAND%2520FOR%2520SALE%2520,co_white,b_rgb:e5484d,g_north_west,x_{PAD},y_76",
    ]
    if ptype:
        parts.append(f"l_text:Montserrat_26_bold_letter_spacing_2:%2520{_land_enc(ptype.upper())}%2520,co_white,b_rgb:0f1729,g_north_east,x_{PAD},y_76")
    if loc:
        parts.append(f"l_text:Montserrat_36_semibold:{_land_enc(loc)}%252C%2520Lagos,co_rgb:f1f4fa,g_north_west,x_{PAD},y_150")
    if title:
        parts.append(f"l_text:Montserrat_46_semibold:{_land_enc(title)},co_white,g_south_west,x_{PAD},y_410,w_1250,c_fit")
    if size:
        parts.append(f"l_text:Montserrat_104_bold:{_land_enc(size)},co_white,g_south_west,x_{PAD},y_268")
        if price:
            parts.append(f"l_text:Montserrat_66_bold:{_land_enc(price)},co_rgb:ff6b6f,g_south_west,x_{PAD},y_176")
    elif price:
        parts.append(f"l_text:Montserrat_96_bold:{_land_enc(price)},co_white,g_south_west,x_{PAD},y_246")
    parts += [
        f"l_text:Montserrat_24_bold_letter_spacing_3:CW%2520REAL%2520ESTATE,co_white,g_south_west,x_{PAD},y_52",
        f"l_text:Montserrat_24_regular:cwlagos.com,co_rgb:c3cad9,g_south_east,x_{PAD},y_52",
        "q_auto",
        f"{base_id}.jpg",
    ]
    return "/".join(parts)


def build_land_og_card_url(fields: dict, base_id: str = _CLD_BASE) -> str:
    title = _clip(str(fields.get("Property Title") or "").strip(), 80)
    size  = _format_land_size(fields.get("Land Size") or "")
    price = str(fields.get("New Price") or "").strip()
    loc_raw = fields.get("Main Neighbourhood Label") or ""
    loc   = str(loc_raw[0] if isinstance(loc_raw, list) else loc_raw).strip()
    ptype_raw = fields.get("Property Type") or ""
    ptype = str(ptype_raw[0] if isinstance(ptype_raw, list) else ptype_raw).strip()
    parts = [
        f"https://res.cloudinary.com/{_CLD_CLOUD}/image/upload",
        "c_fill,w_1200,h_630,g_auto",
        f"l_{_CLD_OG_OVERLAY},g_north_west,x_0,y_0",
        f"l_text:Montserrat_22_bold_letter_spacing_3:%2520LAND%2520FOR%2520SALE%2520,co_white,b_rgb:e5484d,g_north_west,x_56,y_48",
    ]
    if ptype:
        parts.append(f"l_text:Montserrat_20_bold_letter_spacing_2:%2520{_land_enc(ptype.upper())}%2520,co_white,b_rgb:0f1729,g_north_east,x_56,y_48")
    if title:
        parts.append(f"l_text:Montserrat_34_semibold:{_land_enc(title)},co_white,g_south_west,x_56,y_290,w_1080,c_fit")
    if size:
        parts.append(f"l_text:Montserrat_84_bold:{_land_enc(size)},co_white,g_south_west,x_56,y_180")
        if price:
            parts.append(f"l_text:Montserrat_48_bold:{_land_enc(price)},co_rgb:ff6b6f,g_south_west,x_56,y_118")
    elif price:
        parts.append(f"l_text:Montserrat_76_bold:{_land_enc(price)},co_white,g_south_west,x_56,y_170")
    if loc:
        parts.append(f"l_text:Montserrat_28_semibold:{_land_enc(loc)}%252C%2520Lagos,co_rgb:e3e8f2,g_south_west,x_56,y_70")
    parts += [
        f"l_text:Montserrat_19_bold_letter_spacing_3:CW%2520REAL%2520ESTATE,co_white,g_south_west,x_56,y_32",
        f"l_text:Montserrat_19_regular:cwlagos.com,co_rgb:c3cad9,g_south_east,x_56,y_32",
        "q_auto",
        f"{base_id}.jpg",
    ]
    return "/".join(parts)


def land_card_hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]

AIRTABLE_BASE_ID = "appui0GIVsqalToEa"
_PID_LOCK = threading.Lock()  # prevents two simultaneous jobs getting the same PID
AIRTABLE_TABLE   = "Lagos Properties"
AIRTABLE_TOKEN   = CREDENTIALS.get("airtable", {}).get("token", "")

app   = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
JOBS  = {}


# ── Airtable: get agents list ────────────────────────────────────────────────

def get_agents() -> list:
    """Fetch all agents from the Airtable Agents table."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Agents"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params  = {"fields[]": "Name", "pageSize": 100, "sort[0][field]": "Name", "sort[0][direction]": "asc"}
    agents  = []
    offset  = None
    while True:
        if offset:
            params["offset"] = offset
        r    = req.get(url, headers=headers, params=params)
        data = r.json()
        for rec in data.get("records", []):
            name = rec.get("fields", {}).get("Name", "")
            if name:
                agents.append({"id": rec["id"], "name": name})
        offset = data.get("offset")
        if not offset:
            break
    return sorted(agents, key=lambda a: a["name"])


# ── Airtable: get next PID ───────────────────────────────────────────────────

def _fetch_max_pid(prefix: str) -> int:
    """Return the highest numeric suffix for Property IDs matching prefix + digits."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params  = {
        "fields[]": "Property ID",
        "maxRecords": 200,
        "sort[0][field]": "Created Time",
        "sort[0][direction]": "desc",
    }
    r    = req.get(url, headers=headers, params=params, timeout=15)
    data = r.json()
    max_num = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.IGNORECASE)
    for rec in data.get("records", []):
        pid = rec.get("fields", {}).get("Property ID", "")
        m = pat.match(str(pid))
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num


def get_next_pid() -> str:
    """Return next CW##### property ID."""
    return f"CW{_fetch_max_pid('CW') + 1:05d}"


def get_next_land_pid() -> str:
    """Return next CWL#### property ID for land listings (e.g. CWL1531)."""
    return f"CWL{_fetch_max_pid('CWL') + 1}"


# ── Claude: parse WhatsApp description ──────────────────────────────────────

LOCATIONS = [
    "Lekki Phase 1", "Victoria Island", "Ikoyi", "Lekki",
    "Oniru", "Banana Island", "Old Ikoyi", "Eko Atlantic",
    "Chevron Drive", "VGC", "Osapa London", "Ajah",
]

# Aliases map common spelling variants → canonical location name.
# Order matters: more specific entries must come before substrings they contain.
_LOC_ALIASES = {
    "lekki phase one": "Lekki Phase 1",
    "lekki phase 1":   "Lekki Phase 1",
    "lekki phase i":   "Lekki Phase 1",
    "lekki ph 1":      "Lekki Phase 1",
    "lekki ph1":       "Lekki Phase 1",
    "phase 1 lekki":   "Lekki Phase 1",
    "banana island":   "Banana Island",
    "old ikoyi":       "Old Ikoyi",
    "eko atlantic":    "Eko Atlantic",
    "chevron drive":   "Chevron Drive",
    "chevron":         "Chevron Drive",
    "victoria garden": "VGC",
    "vgc":             "VGC",
    "osapa london":    "Osapa London",
    "osapa":           "Osapa London",
    "oniru":           "Oniru",
    "victoria island": "Victoria Island",
    "v.i.":            "Victoria Island",
    "v/i":             "Victoria Island",
    "ajah":            "Ajah",
    "ikoyi":           "Ikoyi",
    "lekki":           "Lekki",
}
PROPERTY_TYPES = [
    "Semi-detached Duplex", "Terraced Duplex", "Detached Duplex",
    "Apartment", "Flat", "Duplex", "Bungalow", "House", "Studio",
    "Mansion", "Penthouse", "Townhouse",
    # Non-residential
    "Land", "Commercial Property", "Office Space", "Shop", "Warehouse",
    "Plaza", "Factory", "Event Centre",
]

# Aliases for property type matching (handles WhatsApp shorthand + hyphen variants)
# Order matters: more specific aliases must come before substrings they contain
# e.g. "semi-detached duplex" must be checked before "detached duplex"
_PTYPE_ALIASES = {
    "semi-detached duplex": "Semi-detached Duplex",
    "semi detached duplex": "Semi-detached Duplex",
    "semi-detached":        "Semi-detached Duplex",
    "semi detached":        "Semi-detached Duplex",
    "terraced duplex":      "Terraced Duplex",
    "terrace duplex":       "Terraced Duplex",
    "terraced house":       "Terraced Duplex",
    "terrace house":        "Terraced Duplex",
    "terraced":             "Terraced Duplex",
    "terrace":              "Terraced Duplex",
    "detached duplex":      "Detached Duplex",
    "detached house":       "Detached Duplex",
    "detached bungalow":    "Bungalow",
    "flat":                 "Apartment",
    "apartment":            "Apartment",
}
FEATURE_KEYWORDS = {
    "All Room Ensuite":   ["all room ensuite", "all rooms ensuite", "en-suite", "ensuite"],
    "Swimming Pool":      ["swimming pool", "pool"],
    "24 Hours Security":  ["24.hour security", "24hrs security", "24 hour security",
                           "24 hours security", "round the clock security", "security"],
    "Generator":          ["generator", "gen set", "diesel generator"],
    "Boys Quarter":       ["boys quarter", "bq", "boy's quarter"],
    "Elevator":           ["elevator", "lift"],
    "CCTV":               ["cctv", "surveillance camera", "security camera"],
    "Gym":                ["gym", "fitness"],
    "Intercom":           ["intercom"],
}


def parse_price(text: str) -> int:
    """Extract price in Naira from text like ₦18M, ₦18,000,000, 18 million, N18M."""
    t = text.replace(",", "").replace("₦", "")
    t = re.sub(r'\bngn\b', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'(?<!\w)N(?=\d)', ' ', t)  # N as currency prefix before digit (N100 → 100)
    t = t.lower()
    # Billion
    m = re.search(r"(\d+(?:\.\d+)?)\s*b(?:illion)?\b", t)
    if m: return int(float(m.group(1)) * 1_000_000_000)
    # Million (M or million)
    m = re.search(r"(\d+(?:\.\d+)?)\s*m(?:illion)?\b", t)
    if m: return int(float(m.group(1)) * 1_000_000)
    # Thousands (K)
    m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", t)
    if m: return int(float(m.group(1)) * 1_000)
    # Plain number ≥ 100000
    m = re.search(r"(\d{6,})", t.replace(" ", ""))
    if m: return int(m.group(1))
    return 0


def parse_description(text: str) -> dict:
    """Parse a WhatsApp real estate description into structured property data."""
    t_low = text.lower()
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # ── mode ──────────────────────────────────────────────────────────────
    if re.search(r"short.?let|shortlet", t_low):
        mode = "short-let"
    elif re.search(r"\bfor rent\b|\bto let\b|\brent[:\s]|\bper annum\b|\bp\.a\.\b", t_low):
        mode = "rent"
    else:
        mode = "sale"

    # ── bedrooms / bathrooms / toilets ────────────────────────────────────
    def extract_num(patterns):
        for pat in patterns:
            m = re.search(pat, t_low)
            if m: return int(m.group(1))
        return 0

    bedrooms  = extract_num([r"(\d+)\s*bed", r"(\d+)\s*br\b", r"(\d+)\s*room"])
    bathrooms = extract_num([r"(\d+)\s*bath", r"(\d+)\s*baths"])
    toilets   = extract_num([r"(\d+)\s*toilet", r"(\d+)\s*wc"])
    if not bathrooms: bathrooms = bedrooms
    if not toilets:   toilets   = bathrooms + 1

    # ── property type tag (team adds as first line, overrides all regex) ──
    _PROP_TAG_RE = re.compile(
        r'^\s*(apartment|flat|studio|penthouse|semi[\s\-]?detached|terraced?|detached'
        r'|duplex|bungalow|house|mansion|townhouse|commercial|office|shop|warehouse'
        r'|plaza|factory|event\s+centre|land)\s+listing\s*$',
        re.IGNORECASE | re.MULTILINE
    )
    _PROP_TAG_MAP = {
        "apartment":     "Apartment",          "flat":          "Apartment",
        "studio":        "Studio",             "penthouse":     "Penthouse",
        "semi detached": "Semi-detached Duplex","semi-detached": "Semi-detached Duplex",
        "semidetached":  "Semi-detached Duplex",
        "terraced":      "Terraced Duplex",    "terrace":       "Terraced Duplex",
        "detached":      "Detached Duplex",    "duplex":        "Detached Duplex",
        "bungalow":      "Bungalow",           "house":         "House",
        "mansion":       "Mansion",            "townhouse":     "Townhouse",
        "commercial":    "Commercial Property","office":        "Office Space",
        "shop":          "Shop",               "warehouse":     "Warehouse",
        "plaza":         "Plaza",              "factory":       "Factory",
        "event centre":  "Event Centre",       "land":          "Land",
    }
    tag_match = _PROP_TAG_RE.search(text)
    tagged_type = None
    if tag_match:
        tag_key = re.sub(r'[\s\-]+', ' ', tag_match.group(1).lower().strip())
        tagged_type = _PROP_TAG_MAP.get(tag_key)
        # Strip tag line from lines so it doesn't pollute features/title
        tag_line = tag_match.group(0).strip()
        lines = [l for l in lines if l.strip().lower() != tag_line.lower()]

    # ── land detection ────────────────────────────────────────────────────
    _LAND_SIGNALS = re.compile(r"\bplot\b|\bdeed\s+of\s+assign\b", re.IGNORECASE)
    is_land = (tagged_type == "Land"
               or bool(_LAND_SIGNALS.search(text))
               or bool(re.search(r"\bland\b", t_low)
                       and not re.search(r"\bland\s*lord\b|\bland\s*mark\b|\bislande?\b", t_low)))

    # ── sqm size (for land) ───────────────────────────────────────────────
    size_sqm = 0
    if is_land:
        sqm_m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sqm|sq\.?\s*m(?:etres?)?)\b", text, re.IGNORECASE)
        if sqm_m:
            size_sqm = int(float(sqm_m.group(1).replace(",", "")))

    # ── land subtype (for Airtable) ───────────────────────────────────────
    land_subtype = "Mixed-Use Land"
    if is_land:
        if re.search(r"\bcommercial\b", t_low):
            land_subtype = "Commercial Land"
        elif re.search(r"\bresidential\b", t_low):
            land_subtype = "Residential Land"
        elif re.search(r"\bjoint\s+venture\b|\bjv\b", t_low):
            land_subtype = "Joint Venture"
        elif re.search(r"\bindustrial\b", t_low):
            land_subtype = "Industrial Land"

    # ── property type ─────────────────────────────────────────────────────
    if tagged_type:
        property_type = tagged_type
    elif is_land:
        property_type = "Land"
    else:
        property_type = "Flat"
        # Check aliases first (handles "semi detached" without hyphen, etc.)
        for alias, canonical in _PTYPE_ALIASES.items():
            if alias in t_low:
                property_type = canonical
                break
        else:
            for pt in PROPERTY_TYPES:
                if pt.lower() in t_low:
                    property_type = pt
                    break

    # ── location ──────────────────────────────────────────────────────────
    location = "Lekki Phase 1"
    for alias, canonical in _LOC_ALIASES.items():
        if alias in t_low:
            location = canonical
            break

    # ── price ─────────────────────────────────────────────────────────────
    price_section = ""
    pm = re.search(r"(?:price|land\s+value)[:\s]+(.{3,50})", t_low)
    if pm:
        price_section = pm.group(0)
    else:
        for line in lines:
            if re.search(r"[₦n]\s*\d|per\s+annum|\d+m\b|\d+\s*million", line.lower()):
                price_section = line
                break
        if not price_section:
            price_section = text
    price = parse_price(price_section)

    # ── street ────────────────────────────────────────────────────────────
    street = ""
    # Prefer explicit "Street: ..." label
    sm = re.search(r"(?:street|address)[:\s]+([^\n]{3,60})", text, re.IGNORECASE)
    if sm:
        street = sm.group(1).strip()
    else:
        # Look for "number + name + road/close/way/etc" pattern
        sm2 = re.search(
            r"(\d+[a-z]?\s+[A-Z][\w ]{2,25}(?:Street|Road|Close|Drive|Way|Avenue|Crescent|Lane))",
            text
        )
        if sm2: street = sm2.group(1).strip()

    # ── features ──────────────────────────────────────────────────────────
    features = []
    for feat, kws in FEATURE_KEYWORDS.items():
        if any(kw in t_low for kw in kws):
            features.append(feat)

    # ── bedrooms / baths / toilets — zero for land ───────────────────────
    if is_land:
        bedrooms = bathrooms = toilets = 0

    # ── title ─────────────────────────────────────────────────────────────
    if is_land:
        size_str = f"{size_sqm:,}" if size_sqm else ""
        title = f"{size_str}sqm Land in {location}" if size_str else f"Land in {location}"
    else:
        _NON_RESIDENTIAL = {"Commercial Property", "Office Space", "Shop", "Warehouse",
                            "Plaza", "Factory", "Event Centre"}
        if property_type in _NON_RESIDENTIAL or not bedrooms:
            title = f"{property_type} in {location}"
        else:
            title = f"{bedrooms} Bedroom {property_type} in {location}"

    # ── extract feature bullets from raw text ────────────────────────────
    _INVIS = re.compile(r'[\xad​‌‍‎‏    ⁠⁡⁢⁣⁤﻿]')
    def _clean(s): return _INVIS.sub('', s).strip()

    BULLET_PAT   = re.compile(
        r'^[•‣◦⁃∙\-\*\+✅✔✓☑➡➤▶►🔥🏠💎🌟⭐🔑🏡✨🎯]|^\d+[\.\)]\s')
    BULLET_STRIP = re.compile(
        r'^[•‣◦⁃∙\-\*\+✅✔✓☑➡➤▶►🔥🏠💎🌟⭐🔑🏡✨🎯]+\s*|^\d+[\.\)]\s*')
    # Lines to skip — financial/contact/metadata (used in all passes)
    SKIP_FINANCIAL = re.compile(
        r'price[:\s]|₦|\bcontact\b|\bcall\b|per annum|p\.a\.|per year|📍|💰'
        r'|\bagency\b|\blegal\b|\bcaution\b|\bservice charge\b|\btotal\b'
        r'|\bcommission\b|\bpremium\b|\bfacilitator\b'
        r'|\d{10,}',   # phone numbers
        re.IGNORECASE
    )
    # Lines that are metadata headers / location / title — not features
    SKIP_META = re.compile(
        r'^\s*(?:details?|lekki|victoria island|ikoyi|lagos|nigeria'
        r'|for\s+(?:rent|sale|let)|serviced|available|contact|call'
        r'|\d+\s*(?:bedroom|bed|bath|toilet)'
        r'|[A-Z\s!]{10,})\s*$',   # ALL-CAPS shouting lines (title/header)
        re.IGNORECASE
    )

    raw_bullets = []

    # Pass 1: lines with explicit bullet symbols — skip financial items
    for line in lines:
        line = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', line).strip()
        if BULLET_PAT.match(line):
            cleaned = BULLET_STRIP.sub('', line).strip()
            if cleaned and not SKIP_FINANCIAL.search(cleaned):
                raw_bullets.append(cleaned)

    # Pass 2: plain lines under a "Features:" section header
    in_features = False
    for line in lines:
        line = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', line).strip()
        if re.match(r'(?:property\s+)?features?\s*:?$', line, re.IGNORECASE):
            in_features = True
            continue
        if in_features:
            if re.search(r':\s*$', line) and len(line) < 40:
                in_features = False
                continue
            if line and not SKIP_FINANCIAL.search(line) and not BULLET_PAT.match(line):
                if line not in raw_bullets:
                    raw_bullets.append(line)

    # Pass 3: plain lines that look like property features even without a header.
    # Catches listings that just list amenities one-per-line (no bullets, no header).
    # Always run — pass 1/2 may have caught financial items as bullets.
    FEATURE_LINE = re.compile(
        r'\b(?:ensuite|pool|security|electricity|generator|bq|boys quarter'
        r'|elevator|lift|gym|cctv|intercom|parking|kitchen|wardrobe|balcony'
        r'|floor|shower|bath|heater|air condition|a\.?c|dstv|solar|inverter'
        r'|compound|spacious|furnished|smart|cinema|rooftop'
        r'|penthouse|walk.?in|fitted|tiled|marble|granite|hardwood'
        r'|pre.?paid|post.?paid|24\s*hour|24hrs|visitor|lounge|dining'
        r'|swimming|ensuite|all\s+room|ample|interlocked|water\s+heater'
        r'|boys?\s+quarter|store\s+room|family\s+lounge|open\s+plan)\b',
        re.IGNORECASE
    )
    # Extra skip: title/header lines (FOR RENT/SALE, all-caps, metadata)
    SKIP_HEADER = re.compile(
        r'for\s+(?:rent|sale|let|short.?let)|serviced[\.\s]|available|details?'
        r'|\bYTD\b',
        re.IGNORECASE
    )
    title_line = lines[0] if lines else ""
    for line in lines:
        line = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', line).strip()
        # Strip invisible Unicode chars WhatsApp injects anywhere in the line
        clean = re.sub(r'[­​‌‍‎‏    ⁠⁡⁢⁣⁤﻿]', '', line).strip()
        if (clean
                and clean != title_line
                and not BULLET_PAT.match(clean)
                and not SKIP_FINANCIAL.search(clean)
                and not SKIP_HEADER.search(clean)
                and not SKIP_META.search(clean)
                and len(clean) <= 60
                and FEATURE_LINE.search(clean)
                and clean not in raw_bullets):
            raw_bullets.append(clean)

    # ── build formatted description from parsed data ──────────────────────
    prop_stub = {
        "mode": mode, "property_type": property_type, "location": location,
        "bedrooms": bedrooms, "price": price, "features": features,
        "is_land": is_land, "size_sqm": size_sqm,
    }
    description = build_formatted_description(prop_stub, raw_bullets, raw_text=text)

    # ── contact number ───────────────────────────────────────────────────
    contact = ""
    cm = re.search(r'(?:contact|call|tel|phone|whatsapp)[:\s]+(\+?[\d\s\-]{10,15})', text, re.IGNORECASE)
    if cm:
        contact = re.sub(r'\s+', '', cm.group(1)).strip()
    else:
        cm2 = re.search(r'(?<!\d)(\+?234[789]\d{8}|0[789]\d{9})(?!\d)', text)
        if cm2:
            contact = cm2.group(1).strip()

    # ── coordinates ──────────────────────────────────────────────────────
    coordinates = ""
    coord_m = re.search(r'(\d+\.\d+)\s*°?\s*N[,\s]+(\d+\.\d+)\s*°?\s*E', text, re.IGNORECASE)
    if coord_m:
        coordinates = f"{coord_m.group(1)}° N, {coord_m.group(2)}° E"

    return {
        "title":         title,
        "mode":          mode,
        "property_type": property_type,
        "location":      location,
        "bedrooms":      bedrooms,
        "bathrooms":     bathrooms,
        "toilets":       toilets,
        "price":         price,
        "street":        street,
        "description":   description,
        "features":      features,
        "contact":       contact,
        "coordinates":   coordinates,
        "is_land":       is_land,
        "size_sqm":      size_sqm,
        "land_subtype":  land_subtype,
    }


def _join(items: list) -> str:
    """Join a list naturally: 'a, b and c'."""
    items = [str(i) for i in items]
    if not items:  return ""
    if len(items) == 1: return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


# Features that belong to the unit/interior vs shared building amenities
_UNIT_KEYWORDS  = {"ensuite", "kitchen", "wardrobe", "ceiling", "floor", "heater",
                   "balcon", "bedroom", "bathroom", "toilet", "fitted", "pop",
                   "air con", "tiled", "marble", "staircase", "dining", "living"}
_BUILD_KEYWORDS = {"pool", "elevator", "lift", "generator", "cctv", "gym", "parking",
                   "terrace", "rooftop", "security", "intercom", "bq", "boys quarter",
                   "concierge", "reception", "lobby"}


def _categorise(bullets: list):
    """Split feature bullets into unit-level and building/shared amenities."""
    unit, building = [], []
    for b in bullets:
        bl = b.lower()
        if any(k in bl for k in _BUILD_KEYWORDS):
            building.append(b)
        elif any(k in bl for k in _UNIT_KEYWORDS):
            unit.append(b)
        else:
            unit.append(b)   # default to unit
    return unit, building


def generate_overview(prop: dict, raw_bullets: list, raw_text: str = "") -> str:
    """
    Generate a 3-5 line SEO-optimised intro using Claude API.
    Falls back to a basic template if Claude is unavailable.
    """
    beds     = prop.get('bedrooms', '')
    ptype    = prop.get('property_type', 'property')
    loc      = prop.get('location', '')
    mode     = prop.get('mode', 'sale')
    price    = prop.get('price', 0)
    is_land  = prop.get('is_land', False)
    size_sqm = prop.get('size_sqm', 0)

    MODE_PHRASE = {"sale": "for Sale", "rent": "for Rent", "short-let": "for Short Let"}
    mode_str  = MODE_PHRASE.get(mode, "for Sale")
    price_str = f"₦{price:,}" if price else "Price on request"
    size_str  = f"{size_sqm:,}sqm " if size_sqm else ""

    # ── try Claude API ───────────────────────────────────────────────────────
    if _ANTHROPIC_CLIENT:
        try:
            land_subtype = prop.get('land_subtype', 'Land')

            if is_land:
                prompt = f"""You are a senior property copywriter for CW Real Estate, a premium Lagos agency.

Your job: write a compelling, SEO-rich overview paragraph for a LAND listing to be posted on Nigerian property portals (NPC, PropertyPro, PrivateProperty).

RAW LISTING TEXT (WhatsApp):
\"\"\"
{raw_text}
\"\"\"

PARSED CONTEXT:
- Size: {size_str.strip() or 'see listing'}
- Land type: {land_subtype}
- Location: {loc}, Lagos
- Price: {price_str}

INSTRUCTIONS:
1. Write 4–5 flowing sentences (no bullet points, no headers, no markdown)
2. Open with the size, land type, location, and price — make it punchy
3. Pull out the SPECIFIC details from the raw text (title docs, development proposal, JV terms, etc.) — do NOT invent anything not in the source
4. Mention investment angle and why this location is desirable
5. Close with a subtle call to action ("Contact us to schedule a site visit" or similar)
6. Weave in SEO keywords naturally: "land for sale in {loc}", "{loc} Lagos real estate", "{size_str.strip()} plot"
7. Tone: premium, factual, investment-focused
8. Do NOT mention any phone numbers, agent names, or company names
9. Output ONLY the paragraph — nothing else"""
            else:
                prompt = f"""You are a senior property copywriter for CW Real Estate, a premium Lagos agency.

Your job: write a compelling, SEO-rich overview paragraph for a property listing to be posted on Nigerian property portals (NPC, PropertyPro, PrivateProperty).

RAW LISTING TEXT (WhatsApp):
\"\"\"
{raw_text}
\"\"\"

PARSED CONTEXT:
- Type: {beds} Bedroom {ptype}
- Mode: {mode_str}
- Location: {loc}, Lagos
- Price: {price_str}

INSTRUCTIONS:
1. Write 4–5 flowing sentences (no bullet points, no headers, no markdown)
2. Open with a hook: property type, bedrooms, mode, location, and price — make it specific and punchy
3. Pull out the SPECIFIC features and amenities from the raw text — do NOT use generic filler phrases like "beautifully finished" or "tastefully furnished" unless those exact words are in the source
4. Mention estate/building quality cues from the raw text (e.g. smart home, BQ, rooftop, generator capacity, etc.)
5. Close with location appeal and a subtle call to action
6. Weave in SEO keywords: "{beds} bedroom {ptype.lower()} {mode_str.lower()} in {loc}", "{loc} Lagos real estate", "Lagos property"
7. Tone: premium, confident, specific — no vague superlatives
8. Do NOT mention any phone numbers, agent names, or company names
9. Output ONLY the paragraph — nothing else"""

            message = _ANTHROPIC_CLIENT.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text.strip()

        except Exception as e:
            print(f"[Claude API] SEO description failed: {e}", flush=True)
            import traceback; traceback.print_exc()

    # ── fallback: basic template ─────────────────────────────────────────────
    if is_land:
        land_subtype = prop.get('land_subtype', 'Land')
        s1 = f"Prime {size_str}{land_subtype.lower()} for sale in {loc}, Lagos."
        s2 = f"Excellent development opportunity with strong title documentation." if raw_bullets else ""
        return " ".join(s for s in [s1, s2] if s)

    all_feats = raw_bullets if raw_bullets else prop.get('features', [])
    unit_feats, build_feats = _categorise(all_feats) if all_feats else ([], [])

    s1 = f"Beautifully finished {beds} bedroom {ptype.lower()} {mode_str.lower()} in {loc}."
    s2 = f"Each unit features {_join([f.lower() for f in unit_feats])}." if unit_feats else ""
    s3 = f"The development includes {_join([f.lower() for f in build_feats])}." if build_feats else ""
    return " ".join(s for s in [s1, s2, s3] if s)


def parse_financials(raw_text: str, price: int, mode: str = "rent") -> list:
    """
    Extract all financial details from the raw WhatsApp text.
    Returns a list of '• Label: Value' strings.
    """
    lines = []
    t = raw_text or ""

    # Rent / Price / Land Value
    rent_match = re.search(
        r"(?:rent|price|land\s+value)[:\s]+([₦N]?\s*[\d,\.]+(?:\s*(?:million|billion|m|b|k))?"
        r"(?:\s*per\s+(?:annum|year|month|day))?[^\n]*)",
        t, re.IGNORECASE
    )
    if price:
        suffix = ""
        if rent_match:
            raw_val = rent_match.group(1).strip()
            pa = re.search(r"per\s+(?:annum|year|month|day)", raw_val, re.IGNORECASE)
            if pa:
                suffix = f" {pa.group(0)}"
        price_label = "Price" if mode == "sale" else "Rent"
        lines.append(f"• {price_label}: ₦{price:,}{suffix}")
    elif rent_match:
        price_label = "Price" if mode == "sale" else "Rent"
        lines.append(f"• {price_label}: {rent_match.group(1).strip()}")

    # Generic charge patterns — match the VALUE after the label (skip redundant label words)
    # Pattern: label word(s) optionally followed by "fee" then colon/space then value
    charge_patterns = [
        (r"land\s+value[:\s]+([^\n]+)",            "Land Value"),
        (r"premium[:\s]+([^\n]+)",                 "Premium"),
        (r"facilitator['’]?s?\s+fee[:\s]+([^\n]+)", "Facilitator's Fee"),
        (r"agency\s+fee[:\s]+([^\n]+)",            "Agency Fee"),
        (r"agency[:\s]+(?:fee[:\s]+)?([^\n]+)",    "Agency Fee"),
        (r"legal\s+fee[:\s]+([^\n]+)",             "Legal Fee"),
        (r"legal[:\s]+(?:fee[:\s]+)?([^\n]+)",     "Legal Fee"),
        (r"caution\s+fee[:\s]+([^\n]+)",           "Caution Fee"),
        (r"caution[:\s]+(?:fee[:\s]+)?([^\n]+)",   "Caution Fee"),
        (r"service\s+charge[:\s]+([^\n]+)",        "Service Charge"),
        (r"total\s+package[:\s]+([^\n]*\S[^\n]*)", "Total Package"),
        (r"agreement\s+fee[:\s]+([^\n]+)",         "Agreement Fee"),
        (r"agreement[:\s]+([^\n]+)",               "Agreement Fee"),
        (r"commission[:\s]+([^\n]+)",              "Commission"),
    ]
    seen_labels = set()
    for pat, label in charge_patterns:
        if label in seen_labels:
            continue
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(",;")
            if val:  # skip empty values (e.g. "Total Package:" with nothing after)
                lines.append(f"• {label}: {val}")
                seen_labels.add(label)

    return lines


def build_formatted_description(prop: dict, raw_bullets: list, raw_text: str = "") -> str:
    """
    Build the full CW listing description:
      [3-sentence SEO overview]

      Property Features:
      • <every feature from WhatsApp>

      Financial Details:
      • Rent: ₦X per annum
      • Agency Fee: 10%
      • Legal Fee: 10%
      • Caution: ₦1,000,000
      • Service Charge: ₦60,000 monthly
      • Total Package: ₦16,600,000

      📍 Location | 💰 ₦X
    """
    price     = prop.get('price', 0)
    loc       = prop.get('location', '')
    mode      = prop.get('mode', 'rent')
    price_str = f"₦{price:,}" if price else "Price on request"

    overview = generate_overview(prop, raw_bullets, raw_text)

    # All features from WhatsApp (raw_bullets has everything — don't filter)
    feature_bullets = ([f"• {b}" for b in raw_bullets] if raw_bullets
                       else [f"• {f}" for f in prop.get('features', [])])

    # Full financial breakdown from raw text
    fin_lines = parse_financials(raw_text, price, mode)
    if not fin_lines:
        fin_lines = [f"• Price: {price_str}"]

    parts = [overview, ""]
    if feature_bullets:
        parts += ["Property Features:", ""] + feature_bullets + [""]
    parts += ["Financial Details:", ""] + fin_lines + [""]
    parts.append(f"📍 {loc} | 💰 {price_str}")
    return "\n".join(parts)


# ── job runner ───────────────────────────────────────────────────────────────

def emit(q, event_type, data):
    q.put({"type": event_type, **data})


def run_job(job_id, prop, platforms, raw_image_paths, posted_by=""):
    q = JOBS[job_id]["events"]
    airtable_selected = False
    platform_ids = {}
    try:
        emit(q, "log", {"msg": f"Getting next Property ID from Airtable…"})
        with _PID_LOCK:
            # Use property_type as source of truth (user may have edited it)
            pid = get_next_land_pid() if prop.get("property_type") == "Land" else get_next_pid()
            prop["is_land"] = (prop.get("property_type") == "Land")
        prop["ref"] = pid
        emit(q, "pid", {"pid": pid})
        emit(q, "log", {"msg": f"Property ID: {pid}"})

        # ── Enhance all images to HD ─────────────────────────────────────────
        enh_dir = BASE_DIR / "enhanced" / pid
        enh_dir.mkdir(parents=True, exist_ok=True)
        enhanced = []
        if raw_image_paths:
            emit(q, "log", {"msg": f"Enhancing {len(raw_image_paths)} images to HD…"})
            for src in raw_image_paths:
                edest = enh_dir / (Path(src).stem + ".jpg")
                try:
                    enhance_image(src, str(edest))
                    enhanced.append(str(edest))
                except Exception as e:
                    emit(q, "log", {"msg": f"  Enhance failed ({Path(src).name}): {e}"})
                    enhanced.append(src)  # fall back to original

        # ── Watermark — logo only on the first (thumbnail) image ─────────────
        wm_dir = BASE_DIR / "watermarked" / pid
        wm_dir.mkdir(parents=True, exist_ok=True)
        watermarked = []
        if enhanced:
            emit(q, "log", {"msg": f"Watermarking thumbnail…"})
            for i, src in enumerate(enhanced):
                dest = wm_dir / Path(src).name
                if i == 0:
                    try:
                        apply_watermark(src, str(dest), WATERMARK_LOGO, opacity=1.0)
                        watermarked.append(str(dest))
                    except Exception as e:
                        emit(q, "log", {"msg": f"  Watermark failed: {e}"})
                        watermarked.append(src)
                else:
                    import shutil
                    shutil.copy2(src, str(dest))
                    watermarked.append(str(dest))

        # Store for potential retries
        JOBS[job_id]["prop"]        = prop
        JOBS[job_id]["watermarked"] = watermarked

        # Post to each platform in parallel
        # Airtable runs fire-and-forget — image uploads are slow and shouldn't
        # block PP Nigeria / PropertyPro / NPC from completing first.
        import concurrent.futures

        main_platforms     = [p for p in platforms if p != "airtable"]
        airtable_selected  = "airtable" in platforms
        total = len(main_platforms) + (1 if airtable_selected else 0)
        done  = 0

        def post_one(platform):
            poster = PLATFORM_POSTERS[platform]
            creds  = CREDENTIALS[platform]
            emit(q, "log", {"msg": f"  [{PLATFORM_LABELS[platform]}] posting…"})
            imgs = enhanced if platform == "airtable" else watermarked
            if platform != "airtable":
                p = dict(prop)
                p["description"] = f"{pid}\n{prop.get('description', '')}"
            else:
                p = prop
            return platform, poster(p, imgs, creds)

        # Step 1 — post to PP Nigeria, PropertyPro, NPC (fast)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(main_platforms), 1)) as ex:
            futures = {ex.submit(post_one, p): p for p in main_platforms}
            for fut in concurrent.futures.as_completed(futures):
                platform, result = fut.result()
                done += 1
                # Collect platform listing IDs for Airtable record
                lid = result.get("listing_id", "")
                if lid:
                    platform_ids[platform] = str(lid)
                emit(q, "result", {
                    "ref":      pid,
                    "platform": platform,
                    "label":    PLATFORM_LABELS[platform],
                    "success":  result.get("success", False),
                    "url":      result.get("listing_url", ""),
                    "images":   result.get("images_uploaded", 0),
                    "error":    result.get("error", ""),
                    "progress": round(done / total * 100),
                })

        # Step 2 — main platforms done
        if airtable_selected:
            # Show success banner but keep stream open for Airtable result
            emit(q, "partial_done", {"msg": "Posted ✓ — Airtable uploading images…"})
        else:
            emit(q, "done", {"msg": "All done!"})

        # Step 3 — Airtable in background, sends its result then closes stream
        if airtable_selected:
            # Attach metadata before Airtable record is created
            prop["posted_by"]    = posted_by
            prop["platform_ids"] = platform_ids
            def _airtable_bg():
                try:
                    platform, result = post_one("airtable")
                    emit(q, "result", {
                        "ref":      pid,
                        "platform": platform,
                        "label":    PLATFORM_LABELS[platform],
                        "success":  result.get("success", False),
                        "url":      result.get("listing_url", ""),
                        "images":   result.get("images_uploaded", 0),
                        "error":    result.get("error", ""),
                        "progress": 100,
                    })
                    emit(q, "done", {"msg": "All done!"})
                except Exception as e:
                    emit(q, "error", {"msg": f"Airtable: {e}"})
                finally:
                    JOBS[job_id]["done"] = True
                    q.put(None)  # close the stream from background thread
            threading.Thread(target=_airtable_bg, daemon=True).start()

    except Exception as e:
        emit(q, "error", {"msg": str(e)})
    finally:
        JOBS[job_id]["done"] = True
        if not airtable_selected:
            q.put(None)  # only close stream here if Airtable isn't running in background


def run_land_job(job_id, prop, platforms, raw_image_paths, posted_by=""):
    """Post a land listing: build Cloudinary card, download as cover, post everywhere."""
    q = JOBS[job_id]["events"]
    airtable_selected = False
    platform_ids = {}
    try:
        emit(q, "log", {"msg": "Getting next Land Property ID from Airtable…"})
        with _PID_LOCK:
            pid = get_next_land_pid()
        prop["ref"]     = pid
        prop["is_land"] = True
        emit(q, "pid", {"pid": pid})
        emit(q, "log", {"msg": f"Property ID: {pid}"})

        # ── Build Cloudinary land card URLs ──────────────────────────────────
        cld_fields = {
            "Property Title":           prop.get("title", ""),
            "Land Size":                f"{int(prop['size_sqm']):,} sqm" if prop.get("size_sqm") else "",
            "New Price":                prop.get("price_str", ""),
            "Main Neighbourhood Label": [prop.get("location", "")],
            "Property Type":            prop.get("property_type", "Mixed-Use Land"),
        }
        thumb_url = build_land_card_url(cld_fields)
        og_url    = build_land_og_card_url(cld_fields)
        card_hash = land_card_hash(thumb_url)
        prop["land_card_url"]  = thumb_url
        prop["land_og_url"]    = og_url
        prop["land_card_hash"] = card_hash
        emit(q, "log", {"msg": "Land card URL built."})

        # ── Download card JPEG → used as cover photo for NPC/PP ──────────────
        card_dir = BASE_DIR / "enhanced" / pid
        card_dir.mkdir(parents=True, exist_ok=True)
        card_path = str(card_dir / f"land-card-{pid.lower()}.jpg")
        try:
            import urllib.request
            urllib.request.urlretrieve(thumb_url, card_path)
            emit(q, "log", {"msg": "Land card downloaded."})
        except Exception as _e:
            emit(q, "log", {"msg": f"  Card download failed: {_e}"})
            card_path = None

        # ── Enhance real photos ───────────────────────────────────────────────
        enh_dir = card_dir
        enhanced = []
        if raw_image_paths:
            emit(q, "log", {"msg": f"Enhancing {len(raw_image_paths)} images to HD…"})
            for src in raw_image_paths:
                edest = enh_dir / (Path(src).stem + ".jpg")
                try:
                    enhance_image(src, str(edest))
                    enhanced.append(str(edest))
                except Exception as e:
                    emit(q, "log", {"msg": f"  Enhance failed ({Path(src).name}): {e}"})
                    enhanced.append(src)

        # ── Watermark real photos ─────────────────────────────────────────────
        wm_dir = BASE_DIR / "watermarked" / pid
        wm_dir.mkdir(parents=True, exist_ok=True)
        watermarked = []
        if enhanced:
            emit(q, "log", {"msg": "Watermarking photos…"})
            for i, src in enumerate(enhanced):
                dest = wm_dir / Path(src).name
                if i == 0:
                    try:
                        apply_watermark(src, str(dest), WATERMARK_LOGO, opacity=1.0)
                        watermarked.append(str(dest))
                    except Exception as e:
                        emit(q, "log", {"msg": f"  Watermark failed: {e}"})
                        watermarked.append(src)
                else:
                    import shutil
                    shutil.copy2(src, str(dest))
                    watermarked.append(str(dest))

        # NPC/PP: card first (= cover), then agent-uploaded photos
        npc_pp_images = ([card_path] if card_path else []) + watermarked

        JOBS[job_id]["prop"]        = prop
        JOBS[job_id]["watermarked"] = npc_pp_images

        import concurrent.futures
        main_platforms    = [p for p in platforms if p != "airtable"]
        airtable_selected = "airtable" in platforms
        total = len(main_platforms) + (1 if airtable_selected else 0)
        done  = 0

        def post_one_land(platform):
            poster = PLATFORM_POSTERS[platform]
            creds  = CREDENTIALS[platform]
            emit(q, "log", {"msg": f"  [{PLATFORM_LABELS[platform]}] posting…"})
            if platform == "airtable":
                imgs = enhanced  # real photos; card written via URL PATCH inside poster
                p    = prop
            else:
                imgs = npc_pp_images  # card first → NPC cover
                p    = dict(prop)
                p["description"] = f"{pid}\n{prop.get('description', '')}"
            return platform, poster(p, imgs, creds)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(main_platforms), 1)) as ex:
            futures = {ex.submit(post_one_land, p): p for p in main_platforms}
            for fut in concurrent.futures.as_completed(futures):
                platform, result = fut.result()
                done += 1
                lid = result.get("listing_id", "")
                if lid:
                    platform_ids[platform] = str(lid)
                emit(q, "result", {
                    "ref":      pid,
                    "platform": platform,
                    "label":    PLATFORM_LABELS[platform],
                    "success":  result.get("success", False),
                    "url":      result.get("listing_url", ""),
                    "images":   result.get("images_uploaded", 0),
                    "error":    result.get("error", ""),
                    "progress": round(done / total * 100),
                })

        if airtable_selected:
            emit(q, "partial_done", {"msg": "Posted ✓ — Airtable uploading images…"})
        else:
            emit(q, "done", {"msg": "All done!"})

        if airtable_selected:
            prop["posted_by"]    = posted_by
            prop["platform_ids"] = platform_ids
            def _airtable_bg():
                try:
                    platform, result = post_one_land("airtable")
                    emit(q, "result", {
                        "ref":      pid,
                        "platform": platform,
                        "label":    PLATFORM_LABELS[platform],
                        "success":  result.get("success", False),
                        "url":      result.get("listing_url", ""),
                        "images":   result.get("images_uploaded", 0),
                        "error":    result.get("error", ""),
                        "progress": 100,
                    })
                    emit(q, "done", {"msg": "All done!"})
                except Exception as e:
                    emit(q, "error", {"msg": f"Airtable: {e}"})
                finally:
                    JOBS[job_id]["done"] = True
                    q.put(None)
            threading.Thread(target=_airtable_bg, daemon=True).start()

    except Exception as e:
        emit(q, "error", {"msg": str(e)})
    finally:
        JOBS[job_id]["done"] = True
        if not airtable_selected:
            q.put(None)


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/agents")
def agents_list():
    try:
        return jsonify(get_agents())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/parse", methods=["POST"])
def parse():
    text = (request.get_json() or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400
    try:
        parsed = parse_description(text)
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload-images", methods=["POST"])
def upload_images():
    session_id = request.form.get("session_id", "default")
    files = request.files.getlist("images")
    dest = UPLOADS_DIR / session_id
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if f.filename:
            path = dest / f.filename
            f.save(str(path))
            saved.append(str(path))
    return jsonify({"saved": saved, "count": len(saved)})


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html", team_members=TEAM_MEMBERS)


@app.route("/api/verify-pin", methods=["POST"])
def verify_pin():
    body = request.get_json() or {}
    user = body.get("user", "").strip()
    pin  = body.get("pin", "").strip()
    if user not in TEAM_MEMBERS:
        return jsonify({"ok": False, "error": "Unknown user"}), 403
    if pin != TEAM_PIN:
        return jsonify({"ok": False, "error": "Wrong PIN"}), 403
    return jsonify({"ok": True, "user": user})


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/listings")
def api_listings():
    """Return listings from Airtable, optionally filtered by Posted By."""
    user  = request.args.get("user", "").strip()
    limit = min(int(request.args.get("limit", 50)), 200)

    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    fields  = [
        "Property ID", "Property Title", "New Price", "Posted By",
        "NPC CW ID", "NPC Daniel ID", "PP Nigeria ID", "PropertyPro ID",
        "Property Link",
    ]
    params  = {
        "maxRecords": limit,
        "sort[0][field]": "Created Time",
        "sort[0][direction]": "desc",
    }
    for i, f in enumerate(fields):
        params[f"fields[{i}]"] = f
    if user and user != "all":
        params["filterByFormula"] = f"{{Posted By}}='{user}'"

    try:
        r    = req.get(url, headers=headers, params=params, timeout=15)
        data = r.json()
        records = []
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            records.append({
                "id":           rec["id"],
                "created":      rec.get("createdTime", ""),
                "ref":          f.get("Property ID", ""),
                "title":        f.get("Property Title", ""),
                "price":        f.get("New Price", ""),
                "posted_by":    f.get("Posted By", ""),
                "npc_cw_id":    f.get("NPC CW ID", ""),
                "npc_daniel_id":f.get("NPC Daniel ID", ""),
                "ppng_id":      f.get("PP Nigeria ID", ""),
                "propertypro_id":f.get("PropertyPro ID", ""),
                "cwlagos_url":  f.get("Property Link", ""),
            })
        return jsonify({"records": records})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/weekly-report")
def api_weekly_report():
    """Return this week's listing stats per team member."""
    from datetime import datetime, timedelta
    today   = datetime.utcnow()
    monday  = today - timedelta(days=today.weekday())
    monday  = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    iso_mon = monday.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params  = {
        "filterByFormula": f"IS_AFTER(CREATED_TIME(), '{iso_mon}')",
        "fields[0]": "Property ID",
        "fields[1]": "Posted By",
        "maxRecords": 500,
    }
    try:
        r    = req.get(url, headers=headers, params=params, timeout=15)
        data = r.json()
        counts = {m: 0 for m in TEAM_MEMBERS}
        total  = 0
        for rec in data.get("records", []):
            poster = rec.get("fields", {}).get("Posted By", "")
            total += 1
            if poster in counts:
                counts[poster] += 1
        return jsonify({
            "week_start": monday.strftime("%b %d"),
            "total":      total,
            "by_member":  counts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/start-job", methods=["POST"])
def start_job():
    body       = request.get_json() or {}
    prop       = body.get("property", {})
    platforms  = body.get("platforms", list(PLATFORM_POSTERS.keys()))
    session_id = body.get("session_id", "default")
    posted_by  = body.get("posted_by", "")

    if not prop.get("title"):
        return jsonify({"error": "no property data — parse a description first"}), 400

    # Collect uploaded images
    img_dir = UPLOADS_DIR / session_id
    raw_paths = []
    if img_dir.exists():
        raw_paths = sorted([
            str(p) for p in img_dir.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
        ])

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"events": queue.Queue(), "done": False}
    threading.Thread(
        target=run_job,
        args=(job_id, prop, platforms, raw_paths, posted_by),
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in JOBS:
        return "not found", 404

    def generate():
        q = JOBS[job_id]["events"]
        while True:
            try:
                event = q.get(timeout=30)
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
                continue
            if event is None:
                yield 'data: {"type":"close"}\n\n'
                break
            yield f"data: {json.dumps(event)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/retry-platform", methods=["POST"])
def retry_platform():
    body     = request.get_json() or {}
    job_id   = body.get("job_id")
    platform = body.get("platform")

    if not job_id or job_id not in JOBS:
        return jsonify({"error": "Job not found — please post a new listing"}), 404
    if not platform or platform not in PLATFORM_POSTERS:
        return jsonify({"error": f"Unknown platform: {platform}"}), 400

    job = JOBS[job_id]
    prop       = job.get("prop")
    watermarked = job.get("watermarked", [])

    if not prop:
        return jsonify({"error": "No property data stored for this job"}), 400

    # ── Platform-specific pre-fix ─────────────────────────────────────────────
    if platform == "propertypro":
        # Force a fresh login — stale/dropped sessions cause 400 / connection errors
        import agents.poster_propertypro as _pp
        _pp._shared_session     = None
        _pp._shared_credentials = None

    # ── Retry ─────────────────────────────────────────────────────────────────
    try:
        poster = PLATFORM_POSTERS[platform]
        creds  = CREDENTIALS[platform]
        # Prepend PID to description — same as main post flow
        p = dict(prop)
        if platform != "airtable":
            pid_val = prop.get("ref", "")
            orig_desc = prop.get("description", "") or ""
            if pid_val and not orig_desc.startswith(pid_val):
                p["description"] = f"{pid_val}\n{orig_desc}"
        result = poster(p, watermarked, creds)
        return jsonify({
            "platform": platform,
            "label":    PLATFORM_LABELS[platform],
            "success":  result.get("success", False),
            "url":      result.get("listing_url", ""),
            "images":   result.get("images_uploaded", 0),
            "error":    result.get("error", ""),
        })
    except Exception as e:
        return jsonify({
            "platform": platform,
            "label":    PLATFORM_LABELS[platform],
            "success":  False,
            "url":      "",
            "images":   0,
            "error":    str(e),
        })


@app.route("/land")
def land():
    return render_template("land.html")


@app.route("/start-land-job", methods=["POST"])
def start_land_job():
    data        = request.get_json()
    prop        = data.get("property", {})
    platforms   = data.get("platforms", [])
    session_id  = data.get("session_id", "")
    posted_by   = data.get("posted_by", "")

    if not platforms:
        return jsonify({"error": "No platforms selected"}), 400

    # Collect uploaded images for this session
    raw_image_paths = []
    if session_id:
        sess_dir = UPLOADS_DIR / session_id
        if sess_dir.exists():
            raw_image_paths = sorted(
                str(p) for p in sess_dir.iterdir()
                if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            )

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    JOBS[job_id] = {"events": q, "done": False, "prop": None, "watermarked": []}

    threading.Thread(
        target=run_land_job,
        args=(job_id, prop, platforms, raw_image_paths, posted_by),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


if __name__ == "__main__":
    UPLOADS_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    print(f"CW Lagos Listing Portal → http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
