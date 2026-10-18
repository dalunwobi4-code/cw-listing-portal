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

FAILURES_FILE = Path("/root/cw_listing_agent/failures.json")
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

# PID allocation lock — prevents duplicate CW##### when two jobs start simultaneously
_pid_lock    = threading.Lock()
_cw_counter  = None   # None = uninitialized; set on first get_next_pid() call
_cwl_counter = None
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
ADMIN_USER   = "Admin"
ADMIN_PIN    = os.environ.get("ADMIN_PIN", "2468")

# ── Cloudinary land card URL builders ────────────────────────────────────────

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
    """Return next CW##### property ID.
    Always fetches Airtable max so manual posts don't cause duplicate PIDs.
    Takes max(fetched, in-memory) so concurrent portal posts still increment correctly.
    """
    global _cw_counter
    with _pid_lock:
        fetched = _fetch_max_pid("CW")
        if _cw_counter is None:
            _cw_counter = fetched
        else:
            _cw_counter = max(_cw_counter, fetched)
        _cw_counter += 1
        return f"CW{_cw_counter:05d}"


def get_next_land_pid() -> str:
    """Return next CWL#### property ID for land listings. Same staleness fix as get_next_pid."""
    global _cwl_counter
    with _pid_lock:
        fetched = _fetch_max_pid("CWL")
        if _cwl_counter is None:
            _cwl_counter = fetched
        else:
            _cwl_counter = max(_cwl_counter, fetched)
        _cwl_counter += 1
        return f"CWL{_cwl_counter}"


# ── Claude: parse WhatsApp description ──────────────────────────────────────

LOCATIONS = ["Lekki Phase 1", "Victoria Island", "Ikoyi", "Lekki"]

# Aliases map common spelling variants → canonical location name.
# Order matters: more specific entries must come before substrings they contain.
# Every alias maps to one of the 4 approved posting zones.
# Non-approved sub-areas collapse to nearest approved zone.
_LOC_ALIASES = {
    "lekki phase one": "Lekki Phase 1",
    "lekki phase 1":   "Lekki Phase 1",
    "lekki phase i":   "Lekki Phase 1",
    "lekki ph 1":      "Lekki Phase 1",
    "lekki ph1":       "Lekki Phase 1",
    "phase 1 lekki":   "Lekki Phase 1",
    "victoria island": "Victoria Island",
    "v.i.":            "Victoria Island",
    "v/i":             "Victoria Island",
    "oniru":           "Victoria Island",
    "eko atlantic":    "Victoria Island",
    "old ikoyi":       "Ikoyi",
    "banana island":   "Ikoyi",
    "ikoyi":           "Ikoyi",
    "osapa london":    "Lekki",
    "osapa":           "Lekki",
    "chevron drive":   "Lekki",
    "chevron":         "Lekki",
    "victoria garden": "Lekki",
    "vgc":             "Lekki",
    "ajah":            "Lekki",
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
    "detached duplex":      "Detached Duplex",
}
FEATURE_KEYWORDS = {
    "All Room Ensuite":   ["all room ensuite", "all rooms ensuite", "en-suite", "ensuite"],
    "Swimming Pool":      ["swimming pool", "pool"],
    "24 Hours Security":  ["24.hour security", "24hrs security", "24 hour security",
                           "round the clock security", "security"],
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
    land_subtype = "Mixed Use Land"
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


def _log_failure(ref, title, platform, label, error, prop=None):
    import time as _t
    try:
        data = []
        if FAILURES_FILE.exists():
            try:
                import json as _j; data = _j.loads(FAILURES_FILE.read_text())
            except: pass
        data = [x for x in data if not (x.get("ref")==ref and x.get("platform")==platform)]
        data.insert(0, {
            "id": uuid.uuid4().hex[:12], "ref": ref, "title": title,
            "platform": platform, "label": label, "error": error,
            "timestamp": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
            "prop": prop or {},
        })
        import json as _j; FAILURES_FILE.write_text(_j.dumps(data[:300], indent=2))
    except: pass

def _clear_failure(ref, platform):
    try:
        if not FAILURES_FILE.exists(): return
        import json as _j
        data = _j.loads(FAILURES_FILE.read_text())
        data = [x for x in data if not (x.get("ref")==ref and x.get("platform")==platform)]
        FAILURES_FILE.write_text(_j.dumps(data, indent=2))
    except: pass

def _run_claude_fix(job_id, platform, error, ref):
    import anthropic as _ant, subprocess as _sp, time as _time
    q = JOBS[job_id]["events"]

    def ev(t, m): emit(q, t, {"msg": str(m)})

    ev("log", "Claude is investigating...")
    poster_map = {
        "propertypro": "agents/poster_propertypro.py",
        "ppng":        "agents/poster_ppng.py",
        "npc_daniel":  "agents/poster_npc.py",
        "npc_cw":      "agents/poster_npc.py",
        "airtable":    "agents/poster_airtable.py",
    }
    poster_code = ""
    pf = poster_map.get(platform, "")
    if pf:
        try:
            with open(f"/root/cw_listing_agent/{pf}") as f: poster_code = f.read()[:4000]
        except: pass
    try:
        logs = _sp.run("journalctl -u cwlistings -n 80 --no-pager",
                       shell=True, capture_output=True, text=True, timeout=10).stdout[-3000:]
    except: logs = ""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key: ev("error", "ANTHROPIC_API_KEY not set"); return
    client = _ant.Anthropic(api_key=api_key, timeout=25.0)
    tools = [
        {"name":"run_bash","description":"Run bash on VPS as root.","input_schema":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
        {"name":"read_file","description":"Read a file.","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
        {"name":"write_file","description":"Write a file.","input_schema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}},
    ]
    NL = chr(10)
    system = ("You are an automated fix agent for the CW Lagos listing portal running at /root/cw_listing_agent. "
              "Diagnose and fix posting failures. Common causes: login session expired, site changed, wrong credentials. "
              "Restart with: systemctl restart cwlistings. Be concise.")
    messages = [{"role":"user","content":
        "Platform failed: "+platform+NL+"Error: "+error+NL+"Ref: "+ref+NL+NL+
        "Recent logs:"+NL+logs+NL+NL+"Poster code:"+NL+poster_code}]
    start = _time.time()
    for _ in range(5):
        if _time.time() - start > 90: ev("done", "Timed out (90s). Check logs manually."); return
        try:
            resp = client.messages.create(model="claude-opus-4-7", max_tokens=2048,
                                          system=system, tools=tools, messages=messages)
        except Exception as e: ev("error", str(e)); return
        for block in resp.content:
            if hasattr(block,"text") and block.text.strip(): ev("log", block.text)
        if resp.stop_reason in ("end_turn","stop_sequence"): ev("done", "Claude finished."); return
        if resp.stop_reason != "tool_use": ev("done", "Done."); return
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use": continue
            ev("log", "-> "+block.name+": "+str(block.input)[:100])
            try:
                if block.name == "run_bash":
                    r = _sp.run(block.input["command"],shell=True,capture_output=True,text=True,timeout=30)
                    out = (r.stdout+r.stderr)[:2000] or "(no output)"
                    ev("log", out[:300])
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":out})
                elif block.name == "read_file":
                    with open(block.input["path"]) as f: content = f.read()[:5000]
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":content})
                elif block.name == "write_file":
                    with open(block.input["path"],"w") as f: f.write(block.input["content"])
                    ev("log","wrote "+block.input["path"])
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":"OK"})
                else:
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":"unknown"})
            except Exception as e:
                tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":str(e),"is_error":True})
                ev("log","error: "+str(e))
        messages.append({"role":"assistant","content":resp.content})
        messages.append({"role":"user","content":tool_results})
    ev("done", "Complete.")


def run_job(job_id, prop, platforms, raw_image_paths, posted_by="", platform_props=None):
    q = JOBS[job_id]["events"]
    airtable_selected = False
    platform_ids = {}

    def _get_prop(platform):
        if not platform_props:
            return prop
        key = "npc" if platform.startswith("npc") else platform
        override = platform_props.get(key, {})
        if not override:
            return prop
        merged = {**prop, **override}
        for k in ("ref", "posted_by", "platform_ids"):
            if k in prop:
                merged[k] = prop[k]
        merged["is_land"] = merged.get("property_type") == "Land"
        return merged

    try:
        emit(q, "log", {"msg": f"Getting next Property ID from Airtable…"})
        with _PID_LOCK:
            # Use property_type as source of truth (user may have edited it)
            pid = get_next_land_pid() if prop.get("property_type") == "Land" else get_next_pid()
            prop["is_land"] = (prop.get("property_type") == "Land")
        prop["ref"] = pid
        emit(q, "pid", {"pid": pid})
        emit(q, "log", {"msg": f"Property ID: {pid}"})

        # Index uploaded photos for duplicate detection immediately (don't wait for nightly backfill)
        def _index_now(paths, ref):
            try:
                from agents.dup_detect import compute_phash_from_path, index_batch
                rows = []
                for p in paths:
                    h = compute_phash_from_path(str(p))
                    if h is not None:
                        rows.append(("", ref, f"local:{ref}:{Path(p).name}", h))
                index_batch(rows)
            except Exception as e:
                print(f"[DUP] index failed for {ref}: {e}", flush=True)
        threading.Thread(target=_index_now, args=(list(raw_image_paths or []), pid), daemon=True).start()

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
                _base = _get_prop(platform)
                p = dict(_base)
                p["description"] = f"{pid}\n{_base.get('description', '')}"
            else:
                p = _get_prop(platform)
            return platform, poster(p, imgs, creds)

        # Step 1 — post to PP Nigeria, PropertyPro, NPC (fast)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(main_platforms), 1)) as ex:
            futures = {ex.submit(post_one, p): p for p in main_platforms}
            for fut in concurrent.futures.as_completed(futures):
                platform, result = fut.result()
                done += 1
                lid = result.get("listing_id", "") or result.get("property_id", "")
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
                _p = _get_prop(platform)
                if result.get("success", False):
                    _clear_failure(_p.get("ref",""), platform)
                else:
                    _log_failure(_p.get("ref",""), _p.get("title",""), platform, PLATFORM_LABELS[platform], result.get("error",""), _p)

        # Step 2 — main platforms done
        if airtable_selected:
            # Show success banner but keep stream open for Airtable result
            emit(q, "partial_done", {"msg": "Posted ✓ — Airtable uploading images…"})
        else:
            emit(q, "done", {"msg": "All done!"})

        # Step 3 — Airtable in background, sends its result then closes stream
        if airtable_selected:
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
                    if result.get("success", False):
                        _clear_failure(prop.get("ref",""), "airtable")
                    else:
                        _log_failure(prop.get("ref",""), prop.get("title",""), "airtable", PLATFORM_LABELS["airtable"], result.get("error",""), prop)
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
        og_hash   = land_card_hash(og_url)
        prop["land_card_url"]  = thumb_url
        prop["land_og_url"]    = og_url
        prop["land_card_hash"] = card_hash
        prop["land_og_hash"]   = og_hash
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


@app.route("/parse-land", methods=["POST"])
def parse_land():
    text = (request.get_json() or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400
    try:
        t = text
        t_low = t.lower()

        # ── title: first non-empty line that isn't a pure emoji/icon line ─────
        lines = [l.strip() for l in t.strip().splitlines() if l.strip()]
        title = ""
        for l in lines:
            clean = re.sub(r"[^\w\s₦\-–—,./']", "", l).strip()
            if len(clean) > 6:
                title = l
                break

        # ── land subtype ──────────────────────────────────────────────────────
        subtype = "Mixed-Use Land"
        for pat, val in [
            (r"residential\s+land",   "Residential Land"),
            (r"commercial\s+land",    "Commercial Land"),
            (r"industrial\s+land",    "Industrial Land"),
            (r"joint\s+venture",      "Joint Venture"),
            (r"\bplot\b",             "Land"),
        ]:
            if re.search(pat, t_low):
                subtype = val
                break
        if re.search(r"mixed.use", t_low):
            subtype = "Mixed-Use Land"

        # ── size ──────────────────────────────────────────────────────────────
        size = ""
        m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sqm|sq\.?\s?m|m2|m²|square\s*met(?:er|re)s?)", t_low)
        if m:
            size = m.group(1).replace(",", "")

        # ── price ─────────────────────────────────────────────────────────────
        price_text = ""
        price_mode = "total"
        pm = re.search(
            r"(?:price|value|cost)[:\s]*([₦N]?\s*[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|m|b|k))?(?:\s*per\s+(?:sqm|sq\.?\s?m|plot|m2))?[^\n]*)",
            t, re.IGNORECASE)
        if pm:
            price_text = pm.group(1).strip()
        else:
            # fallback: first ₦ line
            pm2 = re.search(r"₦\s*[\d,]+[^\n]*", t)
            if pm2:
                price_text = pm2.group(0).strip()
        if re.search(r"per\s+(?:sqm|sq\.?\s?m|m2)", price_text, re.IGNORECASE):
            price_mode = "per_sqm"

        # ── location ─────────────────────────────────────────────────────────
        location = ""
        _HOODS = [
            "banana island","ikoyi","victoria island","vi","lekki phase 1","lekki phase1",
            "lekki","ajah","ibeju lekki","ibeju-lekki","chevron","osapa","jakande",
            "surulere","yaba","maryland","ikeja gra","gbagada","magodo","omole",
        ]
        for hood in _HOODS:
            if hood in t_low:
                location = hood.title().replace("Vi", "Victoria Island").replace("Ibeju Lekki","Ibeju-Lekki").replace("Lekki Phase1","Lekki Phase 1")
                break
        if not location:
            lm = re.search(r"(?:location|address)[:\s]+([^\n]+)", t, re.IGNORECASE)
            if lm:
                location = lm.group(1).strip().split(",")[0].strip()

        # ── title document ────────────────────────────────────────────────────
        title_doc = ""
        for pat, val in [
            (r"governor'?s?\s+consent",      "Governor's Consent"),
            (r"c\s*of\s*o|certificate\s+of\s+occupancy", "C of O"),
            (r"deed\s+of\s+assign",          "Deed of Assignment"),
            (r"registered\s+survey",         "Registered Survey"),
            (r"excision",                    "Excision"),
            (r"freehold",                    "Freehold"),
        ]:
            if re.search(pat, t_low):
                title_doc = val
                break

        # ── description: everything after the first line ──────────────────────
        desc = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

        return jsonify({
            "title":          title,
            "property_type":  subtype,
            "land_size":      size,
            "price_text":     price_text,
            "price_mode":     price_mode,
            "location":       location,
            "title_document": title_doc,
            "description":    desc,
            "is_land":        True,
        })
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


@app.route("/api/duplicate-check", methods=["POST"])
def api_duplicate_check():
    """Warn-only duplicate check. Two layers:
    1. image_matches: same photo reused (phash), labelled strong (2+) / weak (1)
    2. ai_matches: same property re-shot (fingerprint shortlist + Claude photo comparison)
    """
    body = request.get_json() or {}
    session_id = body.get("session_id", "").strip()
    prop = body.get("property") or {}
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    img_dir = UPLOADS_DIR / session_id
    paths = []
    if img_dir.exists():
        paths = sorted(str(p) for p in img_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic"})

    from agents.dup_detect import check_paths_for_duplicates
    ph = check_paths_for_duplicates(paths) if paths else {"matches": [], "hashed": 0, "failed": 0}
    image_matches = ph["matches"][:5]
    for m in image_matches:
        m["strength"] = "strong" if m["matched_images"] >= 2 else "weak"

    ai = {"shortlisted": 0, "matches": [], "error": None}
    if prop and paths:
        try:
            from agents.dup_ai import ai_check
            strong_refs = {m["ref"] for m in image_matches if m["strength"] == "strong"}
            ai = ai_check(_ANTHROPIC_CLIENT, paths, prop, AIRTABLE_BASE_ID, AIRTABLE_TABLE,
                          AIRTABLE_TOKEN, exclude_refs=strong_refs)
        except Exception as e:
            ai = {"shortlisted": 0, "matches": [], "error": str(e)[:200]}

    # Enrich image matches with title / posted-by / created
    refs = [m["ref"] for m in image_matches if m.get("ref")]
    if refs:
        formula = "OR(" + ",".join(f"{{Property ID}}='{r}'" for r in refs) + ")"
        try:
            r = req.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}",
                        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"},
                        params={"maxRecords": 10, "filterByFormula": formula,
                                "fields[0]": "Property ID", "fields[1]": "Property Title",
                                "fields[2]": "Posted By", "fields[3]": "Created Time"},
                        timeout=15).json()
            by_ref = {rec["fields"].get("Property ID", ""): (rec.get("fields", {}), rec.get("createdTime", ""), rec.get("id", ""))
                      for rec in r.get("records", [])}
            for m in image_matches:
                info, ct, rid = by_ref.get(m["ref"], ({}, "", ""))
                m["title"] = info.get("Property Title", "")
                m["posted_by"] = info.get("Posted By", "")
                m["created"] = info.get("Created Time", "") or ct
                m["record_id"] = m.get("record_id") or rid
        except Exception:
            pass
    for m in image_matches + ai["matches"]:
        if m.get("record_id"):
            m["airtable_url"] = f"https://airtable.com/{AIRTABLE_BASE_ID}/tblvHtVR3gMACTY1N/{m['record_id']}"

    return jsonify({"image_matches": image_matches, "ai_matches": ai["matches"],
                    "shortlisted": ai["shortlisted"], "ai_error": ai["error"],
                    "hashed": ph["hashed"]})


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html", team_members=TEAM_MEMBERS)


@app.route("/api/verify-pin", methods=["POST"])
def verify_pin():
    body = request.get_json() or {}
    user = body.get("user", "").strip()
    pin  = body.get("pin", "").strip()
    if user == ADMIN_USER:
        if pin != ADMIN_PIN:
            return jsonify({"ok": False, "error": "Wrong PIN"}), 403
        return jsonify({"ok": True, "user": user, "is_admin": True})
    if user not in TEAM_MEMBERS:
        return jsonify({"ok": False, "error": "Unknown user"}), 403
    if pin != TEAM_PIN:
        return jsonify({"ok": False, "error": "Wrong PIN"}), 403
    return jsonify({"ok": True, "user": user})


@app.route("/api/failures")
def api_failures():
    try:
        import json as _j
        data = _j.loads(FAILURES_FILE.read_text()) if FAILURES_FILE.exists() else []
    except: data = []
    return jsonify(data)

@app.route("/api/clear-failure", methods=["POST"])
def api_clear_failure():
    body = request.get_json() or {}
    _clear_failure(body.get("ref",""), body.get("platform",""))
    return jsonify({"ok": True})

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/listings")
def api_listings():
    user  = request.args.get("user", "").strip()
    limit = min(int(request.args.get("limit", 50)), 200)
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    fields  = ["Property ID", "Property Title", "New Price", "Posted By",
               "NPC CW ID", "NPC Daniel ID", "PP Nigeria ID", "PropertyPro ID", "Property Link"]
    params  = {"maxRecords": limit, "sort[0][field]": "Created Time", "sort[0][direction]": "desc"}
    for i, f in enumerate(fields):
        params[f"fields[{i}]"] = f
    if user and user != "all":
        params["filterByFormula"] = f"{{Posted By}}=\'{user}\'"
    try:
        r    = req.get(url, headers=headers, params=params, timeout=15)
        data = r.json()
        records = []
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            records.append({
                "id": rec["id"], "created": rec.get("createdTime", ""),
                "ref": f.get("Property ID", ""), "title": f.get("Property Title", ""),
                "price": f.get("New Price", ""), "posted_by": f.get("Posted By", ""),
                "npc_cw_id": f.get("NPC CW ID", ""), "npc_daniel_id": f.get("NPC Daniel ID", ""),
                "ppng_id": f.get("PP Nigeria ID", ""), "propertypro_id": f.get("PropertyPro ID", ""),
                "cwlagos_url": f.get("Property Link", ""),
            })
        return jsonify({"records": records})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/weekly-report")
def api_weekly_report():
    from datetime import datetime, timedelta
    today  = datetime.utcnow()
    monday = today - timedelta(days=today.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    iso_mon = monday.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params  = {"filterByFormula": f"IS_AFTER(CREATED_TIME(), \'{iso_mon}\')",
               "fields[0]": "Property ID", "fields[1]": "Posted By", "maxRecords": 500}
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
        return jsonify({"week_start": monday.strftime("%b %d"), "total": total, "by_member": counts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _require_admin():
    pin = request.headers.get("X-Admin-Pin", "")
    if pin != ADMIN_PIN:
        return jsonify({"error": "Admin authorization required"}), 403
    return None


# ---- Comments (admin feedback on listings, with @tagging) ----
COMMENTS_FILE = "/root/cw_listing_agent/comments.json"
_comments_lock = threading.Lock()

def _load_comments():
    try:
        with open(COMMENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_comments(items):
    with open(COMMENTS_FILE, "w") as f:
        json.dump(items[-500:], f)

@app.route("/api/comments")
def api_comments():
    pid    = request.args.get("pid", "").strip()
    tagged = request.args.get("tagged", "").strip()
    unres  = request.args.get("unresolved", "") == "1"
    with _comments_lock:
        items = _load_comments()
    if pid:
        items = [c for c in items if c.get("pid", "").upper() == pid.upper()]
    if tagged:
        items = [c for c in items if c.get("tag", "") == tagged]
    if unres:
        items = [c for c in items if not c.get("resolved")]
    return jsonify({"comments": list(reversed(items))[:100]})

@app.route("/api/comments", methods=["POST"])
def api_comments_post():
    body   = request.get_json() or {}
    pid    = str(body.get("pid", "")).strip()
    text   = str(body.get("text", "")).strip()
    author = str(body.get("author", "")).strip()
    tag    = str(body.get("tag", "")).strip()
    title  = str(body.get("title", "")).strip()
    if not pid or not text or not author:
        return jsonify({"error": "pid, text and author required"}), 400
    item = {"id": uuid.uuid4().hex[:10], "pid": pid, "title": title, "text": text[:1000],
            "author": author, "tag": tag, "ts": time.time(), "resolved": False}
    with _comments_lock:
        items = _load_comments()
        items.append(item)
        _save_comments(items)
    return jsonify({"ok": True, "comment": item})

@app.route("/api/comments/resolve", methods=["POST"])
def api_comments_resolve():
    body = request.get_json() or {}
    cid  = str(body.get("id", "")).strip()
    with _comments_lock:
        items = _load_comments()
        for c in items:
            if c.get("id") == cid:
                c["resolved"] = True
        _save_comments(items)
    return jsonify({"ok": True})

# ---- PID search (Boosting tab) ----
@app.route("/api/pid-search")
def api_pid_search():
    pid = request.args.get("pid", "").strip().upper()
    if not pid:
        return jsonify({"error": "pid required"}), 400
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    fields  = ["Property ID", "Property Title", "New Price", "Posted By",
               "NPC CW ID", "NPC Daniel ID", "PP Nigeria ID", "PropertyPro ID", "Property Link"]
    params  = {"maxRecords": 5, "filterByFormula": f"SEARCH('{pid}', UPPER({{Property ID}}))"}
    for i, f in enumerate(fields):
        params[f"fields[{i}]"] = f
    try:
        r = req.get(url, headers=headers, params=params, timeout=15)
        recs = []
        for rec in r.json().get("records", []):
            f = rec.get("fields", {})
            recs.append({
                "id": rec["id"], "created": rec.get("createdTime", ""),
                "ref": f.get("Property ID", ""), "title": f.get("Property Title", ""),
                "price": f.get("New Price", ""), "posted_by": f.get("Posted By", ""),
                "npc_cw_id": f.get("NPC CW ID", ""), "npc_daniel_id": f.get("NPC Daniel ID", ""),
                "ppng_id": f.get("PP Nigeria ID", ""), "propertypro_id": f.get("PropertyPro ID", ""),
                "cwlagos_url": f.get("Property Link", ""),
            })
        for rec in recs:
            rec["npc_status"] = {}
            for acct, field in (("npc_cw", "npc_cw_id"), ("npc_daniel", "npc_daniel_id")):
                if rec.get(field):
                    try:
                        rec["npc_status"][acct] = _npc_status(acct, rec[field])
                    except Exception:
                        rec["npc_status"][acct] = {"boostable": False, "reason": "Couldn't check NPC right now",
                                                   "boosted_recently": False, "last_boosted": ""}
        return jsonify({"records": recs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---- Boost recommendations: rank sweep across core searches -> candidates ----
RECO_FILE = "/root/cw_listing_agent/recommendations.json"
_reco_lock = threading.Lock()
_reco_running = {"flag": False}

BED_OPTIONS = {"flats-apartments": (1, 2, 3, 4), "houses": (2, 3, 4, 5)}  # NPC bedroom filter is exact
RECO_SEARCHES = [
    {"location": loc, "mode": mode, "category": cat, "bedrooms": b}
    for loc in ("Ikoyi", "Victoria Island", "Lekki", "Lekki Phase 1")
    for cat in ("flats-apartments", "houses")
    for mode in ("sale", "rent")
    for b in BED_OPTIONS[cat]
]

def _beds_of(v):
    if isinstance(v, list):
        v = v[0] if v else ""
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else None

def _search_label(bedrooms, location, category, mode):
    return (f"{bedrooms} bedroom " if bedrooms else "") + \
        f"{location} \u00b7 {CATEGORY_LABEL.get(category, category.title())} \u00b7 {'Rent' if mode == 'rent' else 'Sale'}"

_area_cache = {}  # location -> (ts, records)

def _area_records(location):
    """All our Airtable listings in an area that are on NPC (cached 10 min per sweep)."""
    hit = _area_cache.get(location)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    fields  = ["Property ID", "Property Title", "New Price", "Posted By", "NPC CW ID", "NPC Daniel ID",
               "Name (from Listing Status)", "Property Type", "Name (from Bedroom)"]
    params  = {"pageSize": 100,
               "filterByFormula": f"AND({{Main Neighborhood}}='{location}', OR({{NPC CW ID}}!='', {{NPC Daniel ID}}!=''))",
               "sort[0][field]": "Created Time", "sort[0][direction]": "desc"}
    for i, f in enumerate(fields):
        params[f"fields[{i}]"] = f
    recs, off = [], None
    for _ in range(30):
        if off:
            params["offset"] = off
        d = req.get(url, headers=headers, params=params, timeout=30).json()
        recs.extend(d.get("records", []))
        off = d.get("offset")
        if not off:
            break
    _area_cache[location] = (time.time(), recs)
    return recs
CATEGORY_LABEL = {"flats-apartments": "Apartments", "houses": "Houses"}
_HOUSE_WORDS = ("duplex", "semi", "terrace", "detached", "bungalow", "house", "mansion", "townhouse", "maisonette")

def _category_guess(ptype):
    p = (ptype or "").lower()
    if "land" in p:
        return "land"
    return "houses" if any(w in p for w in _HOUSE_WORDS) else "flats-apartments"

def _recently_boosted_keys(days=4):
    from agents.booster_npc import _load_boosts
    cutoff = time.time() - days * 86400
    return {b.get("listing_id", "") for b in _load_boosts()
            if b.get("success") and b.get("ts", 0) > cutoff}


_npc_checkers = {}
_npc_checker_lock = threading.Lock()


def _socket_janitor_loop():
    """Every 5 min: free unreferenced sessions and drop idle pooled proxy connections.
    Closing a session only empties its connection pool; it reconnects on next use."""
    import gc
    from agents.poster_npc import LIVE_SESSIONS
    while True:
        time.sleep(300)
        try:
            gc.collect()
            for sess in list(LIVE_SESSIONS):
                try:
                    sess.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[JANITOR] {e}", flush=True)

def _get_checker(acct, fresh=False):
    from agents.booster_npc import NpcChecker
    with _npc_checker_lock:
        hit = _npc_checkers.get(acct)
        if hit and not fresh and time.time() - hit[0] < 600:
            return hit[1]
        chk = NpcChecker(CREDENTIALS[acct])
        if hit:
            try:
                hit[1].s.close()
            except Exception:
                pass
        _npc_checkers[acct] = (time.time(), chk)
        return chk

def _npc_status(acct, listing_id):
    """Live NPC status with one automatic re-login if the cached session went stale."""
    try:
        return _get_checker(acct).status(listing_id)
    except Exception:
        return _get_checker(acct, fresh=True).status(listing_id)

def _reco_candidates(location, mode, ranking_refs, recent_keys, premium, limit=5, category="flats-apartments", bedrooms=None):
    """Newest CW listings in this location+mode that NPC will accept a boost for
    (on an account's Premium list, not boosted on NPC in 24h, not boosted by us in 4 days).
    Returns (candidates, stats)."""
    recs = _area_records(location)
    want = "rent" if mode == "rent" else "sale"
    out = []
    stats = {"in_search": 0, "premium": 0, "boosted_recently": 0, "not_premium": [], "ranking": 0}
    for rec in recs:
        f = rec.get("fields", {})
        if want not in " ".join(f.get("Name (from Listing Status)", [])).lower():
            continue
        ids = {"npc_cw": str(f.get("NPC CW ID", "") or ""), "npc_daniel": str(f.get("NPC Daniel ID", "") or "")}
        npc_cat = next((premium.get(a, {}).get(l, {}).get("category") for a, l in ids.items()
                        if l and premium.get(a, {}).get(l, {}).get("category")), "")
        if (npc_cat or _category_guess(f.get("Property Type"))) != category:
            continue
        if bedrooms and _beds_of(f.get("Name (from Bedroom)")) != bedrooms:
            continue
        stats["in_search"] += 1
        ref = f.get("Property ID", "")
        if ref in ranking_refs:
            stats["ranking"] += 1
            continue
        ids = {"npc_cw": str(f.get("NPC CW ID", "") or ""), "npc_daniel": str(f.get("NPC Daniel ID", "") or "")}
        ok, on_premium, recent = {}, False, False
        for acct, lid in ids.items():
            st = premium.get(acct, {}).get(lid) if lid else None
            if not st:
                continue
            on_premium = True
            if st["boosted_recently"] or any(k in recent_keys for k in (f"{acct}:{lid}", lid)):
                recent = True
                continue
            ok[acct] = True
        if on_premium:
            stats["premium"] += 1
        if not ok:
            if recent:
                stats["boosted_recently"] += 1
            elif not on_premium and len(stats["not_premium"]) < 8:
                stats["not_premium"].append({"ref": ref, "title": f.get("Property Title", "")})
            continue
        if len(out) < limit:
            out.append({
                "ref": ref, "title": f.get("Property Title", ""),
                "price": f.get("New Price", ""), "posted_by": f.get("Posted By", ""),
                "npc_cw_id": ids["npc_cw"], "npc_daniel_id": ids["npc_daniel"],
                "npc_cw_ok": bool(ok.get("npc_cw")), "npc_daniel_ok": bool(ok.get("npc_daniel")),
                "recently_boosted": False,
            })
    return out, stats


_SEARCH_AREAS = {"Lekki": ("Lekki", "Lekki Phase 1")}  # NPC's Lekki search includes Lekki Phase 1

def _reco_from_npc(sc, cards, page1_ids, recent_keys, id_to_ref, limit=None):
    """Every boostable property for one search, straight from NPC's Premium cards
    (both accounts). One candidate per property (same CW PID or same title+price+area).
    Sorted by photo count then newest so you can eyeball the good-looking ones first."""
    areas = _SEARCH_AREAS.get(sc["location"], (sc["location"],))
    props = {}
    for c in cards:
        if (c.get("area") not in areas or c.get("mode") != sc["mode"]
                or c.get("category") != sc["category"] or c.get("bedrooms") != sc["bedrooms"]):
            continue
        ref = id_to_ref.get(c["id"], "")
        key = ref or f"{(c.get('title') or '').lower()}|{c.get('price')}|{c.get('area')}"
        props.setdefault(key, {"ref": ref, "cards": {}})["cards"][c["account"]] = c
    stats = {"premium": len(props), "on_page_1": 0, "boosted_recently": 0}
    out = []
    for key, pr in props.items():
        cs = pr["cards"]
        if any(c["id"] in page1_ids for c in cs.values()):
            stats["on_page_1"] += 1
            continue
        ok = {a: c for a, c in cs.items() if c.get("boostable")}
        if not ok:
            continue
        any_c = max(cs.values(), key=lambda c: c.get("photo_count", 0))
        # first account with a public URL wins (both accounts point at the same listing)
        pub_id, pub_slug, pub_path, pub_mode, pub_cat = "", "", "", "", ""
        for c in cs.values():
            if c.get("url_slug") and c.get("path"):
                pub_id = c["id"]; pub_slug = c["url_slug"]; pub_path = c["path"]
                pub_mode = c.get("mode", ""); pub_cat = c.get("category", "")
                break
        public_url = ""
        if pub_id and pub_slug:
            tx = "for-rent" if pub_mode == "rent" else "for-sale"
            public_url = f"https://nigeriapropertycentre.com/{tx}/{pub_cat}/lagos/{pub_path}/{pub_id}-{pub_slug}"
        out.append({
            "ref": pr["ref"] or any_c.get("npc_ref") or f"NPC {any_c['id']}",
            "in_airtable": bool(pr["ref"]),
            "title": any_c.get("title", ""), "price": any_c.get("price", ""),
            "photo_count": any_c.get("photo_count", 0),
            "thumb": any_c.get("thumb", ""),
            "public_url": public_url,
            "last_boosted": ", ".join(sorted({c.get("last_boosted") for c in cs.values() if c.get("last_boosted")})),
            "npc_cw_id": cs["npc_cw"]["id"] if "npc_cw" in cs else "",
            "npc_daniel_id": cs["npc_daniel"]["id"] if "npc_daniel" in cs else "",
            "npc_cw_ok": "npc_cw" in ok, "npc_daniel_ok": "npc_daniel" in ok,
            "recently_boosted": False, "_newest": max(int(c["id"]) for c in cs.values()),
        })
    out.sort(key=lambda x: (-x.get("photo_count", 0), -x["_newest"]))
    for x in out:
        x.pop("_newest", None)
    return (out[:limit] if limit else out), stats

def _run_reco_sweep():
    from agents.booster_npc import check_ranking
    from concurrent.futures import ThreadPoolExecutor
    result = {"ts": time.time(), "status": "running", "searches": [], "phase": "Reading Premium listings from NPC"}
    with _reco_lock:
        with open(RECO_FILE, "w") as f:
            json.dump(result, f)
    def _prem(acct):
        """Premium list for one account: 3 tries with a fresh login; if NPC still can't be
        read, fall back to the last good list (saved after every successful read)."""
        cache_p = f"/root/cw_listing_agent/premium_cache_{acct}.json"
        last = None
        for attempt in range(3):
            try:
                lst = _get_checker(acct, fresh=True).premium_listings()
                if any(c.get("boostable") for c in lst):
                    with open(cache_p, "w") as f:
                        json.dump({"ts": time.time(), "cards": lst}, f)
                    return acct, lst, None
                last = "NPC returned no boostable Premium listings"
            except Exception as e:
                last = str(e)[:120]
            print(f"[RECO] premium list attempt {attempt + 1} failed for {acct}: {last}", flush=True)
            time.sleep(20)
        try:
            with open(cache_p) as f:
                cached = json.load(f)
            when = time.strftime("%d %b %H:%M", time.localtime(cached["ts"]))
            return acct, cached["cards"], f"couldn't read NPC live ({last}), using its Premium list from {when}"
        except Exception:
            return acct, [], f"couldn't read NPC ({last}), no recommendations for this account"
    with ThreadPoolExecutor(max_workers=2) as ex:
        got = list(ex.map(_prem, ["npc_cw", "npc_daniel"]))
    prem_lists = {a: lst for a, lst, _ in got}
    result["premium_notes"] = {a: n for a, _, n in got if n}
    cards = [dict(c, account=a) for a, lst in prem_lists.items() for c in lst]
    # Photo scoring removed 2026-09-16 — pick from the full pool per search and use
    # the arrow gallery on the dashboard to eyeball photos yourself.
    from agents.booster_npc import _fetch_our_npc_ids, save_extra_our_ids
    id_to_ref = _fetch_our_npc_ids()
    save_extra_our_ids({c["id"]: id_to_ref.get(c["id"]) or c.get("npc_ref") or f"NPC {c['id']}" for c in cards})
    result["premium_counts"] = {a: sum(1 for c in lst if c.get("boostable")) for a, lst in prem_lists.items()}
    result["total_searches"] = len(RECO_SEARCHES)
    _area_cache.clear()
    result["phase"] = "Checking searches"
    with _reco_lock:
        with open(RECO_FILE, "w") as f:
            json.dump(result, f)
    recent_keys = _recently_boosted_keys()
    for sc in RECO_SEARCHES:
        entry = {"location": sc["location"], "mode": sc["mode"], "category": sc["category"],
                 "bedrooms": sc["bedrooms"],
                 "category_label": CATEGORY_LABEL.get(sc["category"], sc["category"]),
                 "label": _search_label(sc["bedrooms"], sc["location"], sc["category"], sc["mode"])}
        try:
            rank = check_ranking(sc["location"], sc["mode"], sc["bedrooms"], category=sc["category"])
            entry.update({"rank_status": rank.get("status", "?"),
                          "cw_count": rank.get("cw_count", 0),
                          "top5_count": rank.get("top5_count", 0),
                          "url": rank.get("url", "")})
            ranking_refs = {c.get("ref", "") for c in rank.get("cw_positions", []) if c.get("ref")}
            if rank.get("status") != "DOMINATING":
                entry["candidates"], entry["stats"] = _reco_from_npc(
                    sc, cards, {str(x) for x in rank.get("result_ids", [])}, recent_keys, id_to_ref)
            else:
                entry["candidates"], entry["stats"] = [], {}
        except Exception as e:
            entry["error"] = str(e)[:200]
            entry["candidates"] = []
        result["searches"].append(entry)
        with _reco_lock:
            with open(RECO_FILE, "w") as f:
                json.dump(result, f)
    result["status"] = "done"
    with _reco_lock:
        with open(RECO_FILE, "w") as f:
            json.dump(result, f)
    _reco_running["flag"] = False

@app.route("/api/admin/recommendations")
def api_recommendations():
    err = _require_admin()
    if err: return err
    try:
        with open(RECO_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"status": "never_run", "searches": []})

@app.route("/api/admin/recommendations/run", methods=["POST"])
def api_recommendations_run():
    err = _require_admin()
    if err: return err
    if _reco_running["flag"]:
        return jsonify({"ok": True, "already_running": True})
    _reco_running["flag"] = True
    threading.Thread(target=_run_reco_sweep, daemon=True).start()
    return jsonify({"ok": True})

# ---- Boost verification: check page 1 15 min after each boost ----
_verify_lock = threading.Lock()

def _boost_meta_key(platform, listing_id):
    return f"{platform}:{listing_id}"

def _load_verify_meta():
    """Extra per-boost metadata stored alongside boosts.json (location/mode + verify result)."""
    try:
        with open("/root/cw_listing_agent/boost_verify.json") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_verify_meta(m):
    with open("/root/cw_listing_agent/boost_verify.json", "w") as f:
        json.dump(m, f)

def _lookup_listing_context(platform, listing_id):
    """Get location + sale/rent for a listing so we know what search to check."""
    field_map = {"npc_cw": "NPC CW ID", "npc_daniel": "NPC Daniel ID",
                 "ppng": "PP Nigeria ID", "propertypro": "PropertyPro ID"}
    field = field_map.get(platform)
    if not field:
        return None
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params = {"maxRecords": 1, "filterByFormula": f"{{{field}}}='{listing_id}'",
              "fields[0]": "Property ID", "fields[1]": "Property Title",
              "fields[2]": "Main Neighborhood", "fields[3]": "Name (from Listing Status)"}
    try:
        r = req.get(url, headers=headers, params=params, timeout=15).json()
        rec = (r.get("records") or [None])[0]
        if not rec:
            return None
        f = rec.get("fields", {})
        loc_field = f.get("Main Neighborhood")
        location = ""
        if isinstance(loc_field, list) and loc_field:
            # linked record → resolve to name via Name (from Listing Status)? no — grab Main Neighbourhood Label
            pass
        # simpler: use Property Title parsing OR pull Main Neighbourhood Label from same rec
        params2 = {"maxRecords": 1, "filterByFormula": f"{{{field}}}='{listing_id}'",
                   "fields[0]": "Main Neighbourhood Label"}
        r2 = req.get(url, headers=headers, params=params2, timeout=15).json()
        rec2 = (r2.get("records") or [None])[0]
        if rec2:
            lab = rec2.get("fields", {}).get("Main Neighbourhood Label")
            if isinstance(lab, list) and lab:
                location = str(lab[0])
            elif isinstance(lab, str):
                location = lab
        status = " ".join(f.get("Name (from Listing Status)", [])).lower()
        mode = "rent" if "rent" in status else "sale"
        pt, beds = "", None
        try:
            ff = req.get(url, headers=headers, params={"maxRecords": 1, "filterByFormula": f"{{{field}}}='{listing_id}'",
                         "fields[0]": "Property Type", "fields[1]": "Name (from Bedroom)"},
                         timeout=15).json()["records"][0]["fields"]
            pt, beds = ff.get("Property Type", ""), _beds_of(ff.get("Name (from Bedroom)"))
        except Exception:
            pass
        return {"location": location, "mode": mode, "ref": f.get("Property ID", ""),
                "title": f.get("Property Title", ""), "ptype": pt, "beds": beds}
    except Exception:
        return None

def _verify_boost(platform, listing_id, ref, attempt=1):
    """Is the boosted listing on page 1 of its own NPC search (area, sale/rent, type, bedrooms)?
    Facts come from the listing's NPC card; Airtable is only a fallback."""
    from agents.booster_npc import check_ranking
    key = _boost_meta_key(platform, listing_id)
    result = {"verified_at": time.time(), "attempt": attempt}
    if platform not in ("npc_cw", "npc_daniel"):
        result.update({"status": "unsupported", "detail": f"Verification not built for {platform}"})
    else:
        st = {}
        try:
            st = _npc_status(platform, listing_id)
        except Exception:
            pass
        loc, mode, cat, beds = st.get("area"), st.get("mode"), st.get("category"), st.get("bedrooms")
        if not (loc and mode and cat):
            ctx = _lookup_listing_context(platform, listing_id) or {}
            loc = loc or ctx.get("location")
            mode = mode or ctx.get("mode")
            cat = cat or (_category_guess(ctx.get("ptype")) if ctx else None)
            beds = beds or ctx.get("beds")
        if not (loc and mode and cat):
            result.update({"status": "no_context", "detail": "Couldn't tell which search this listing belongs to"})
        else:
            try:
                beds = beds if beds in BED_OPTIONS.get(cat, ()) else None
                rank = check_ranking(loc, mode, beds, CREDENTIALS["npc_cw"], category=cat)
                ids = [str(x) for x in rank.get("result_ids", [])]
                base = {"location": loc, "mode": mode, "category": cat, "bedrooms": beds,
                        "category_label": CATEGORY_LABEL.get(cat, cat), "url": rank.get("url", ""),
                        "title": st.get("title", "")}
                if str(listing_id) in ids:
                    result.update(base, status="on_page_1", position=ids.index(str(listing_id)) + 1)
                else:
                    result.update(base, status="not_yet" if attempt == 1 else "not_on_page_1")
            except Exception as e:
                result.update({"status": "check_failed", "detail": str(e)[:200]})
    with _verify_lock:
        meta = _load_verify_meta()
        meta.setdefault(key, []).append(result)
        _save_verify_meta(meta)


def _schedule_verify(platform, listing_id, ref):
    """No-op: _verify_loop picks up every successful boost from boosts.json,
    so pending checks survive portal restarts."""
    return


def _verify_loop():
    """Every minute: 15 min after a successful NPC boost run check 1; if not on page 1,
    run check 2 at 30 min. Driven by boosts.json + boost_verify.json, not in-memory timers."""
    from agents.booster_npc import _load_boosts
    time.sleep(60)
    while True:
        try:
            now = time.time()
            meta = _load_verify_meta()
            for b in _load_boosts():
                if not b.get("success") or now - b.get("ts", 0) > 48 * 3600:
                    continue
                lid = str(b.get("listing_id", ""))
                platform, listing_id = lid.split(":", 1) if ":" in lid else ("npc_cw", lid)
                if platform not in ("npc_cw", "npc_daniel"):
                    continue
                runs = [r for r in meta.get(f"{platform}:{listing_id}", []) if r.get("verified_at", 0) >= b["ts"]]
                age = now - b["ts"]
                if not runs and age >= 15 * 60:
                    _verify_boost(platform, listing_id, b.get("ref", ""), 1)
                elif runs and runs[-1].get("status") == "not_yet" and len(runs) == 1 and age >= 30 * 60:
                    _verify_boost(platform, listing_id, b.get("ref", ""), 2)
        except Exception as e:
            print(f"[VERIFY] loop error: {e}", flush=True)
        time.sleep(60)

if os.environ.get("CW_BACKGROUND", "1") != "0":
    threading.Thread(target=_verify_loop, daemon=True).start()
    threading.Thread(target=_socket_janitor_loop, daemon=True).start()


@app.route("/api/admin/recent-boosts")
def api_recent_boosts():
    err = _require_admin()
    if err: return err
    from agents.booster_npc import _load_boosts
    cutoff = time.time() - 48 * 3600
    boosts = [b for b in _load_boosts() if b.get("ts", 0) > cutoff]
    boosts.sort(key=lambda b: b.get("ts", 0), reverse=True)
    verify = _load_verify_meta()
    out = []
    stats = {"total": 0, "verified_on_page_1": 0, "not_on_page_1": 0, "pending": 0}
    for b in boosts:
        lid = str(b.get("listing_id", ""))
        # listing_id in boosts.json is "platform:id" for new entries, plain id for old NPC-only
        if ":" in lid:
            platform, listing_id = lid.split(":", 1)
        else:
            platform, listing_id = "npc_cw", lid
        vrec = (verify.get(lid) or verify.get(f"{platform}:{listing_id}") or [])
        latest = vrec[-1] if vrec else None
        row = {
            "platform": platform, "listing_id": listing_id, "ref": b.get("ref", ""),
            "user": b.get("user", ""), "ts": b.get("ts", 0),
            "success": b.get("success", False), "detail": b.get("detail", ""),
            "verify": latest,
        }
        out.append(row)
        if b.get("success"):
            stats["total"] += 1
            if latest and latest.get("status") == "on_page_1":
                stats["verified_on_page_1"] += 1
            elif latest and latest.get("status") == "not_on_page_1":
                stats["not_on_page_1"] += 1
            else:
                stats["pending"] += 1
    _enrich_boost_rows(out)
    return jsonify({"boosts": out, "stats": stats})



_BOOST_INFO_FILE = "/root/cw_listing_agent/boost_info.json"

def _load_boost_info():
    try:
        with open(_BOOST_INFO_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_boost_info(key, st):
    info = _load_boost_info()
    info[key] = {k: st.get(k) for k in ("title", "area", "mode", "category", "bedrooms", "npc_ref", "price")}
    with open(_BOOST_INFO_FILE, "w") as f:
        json.dump(info, f)

_boost_info_cache = {}  # ref -> (ts, info)

def _enrich_boost_rows(rows):
    """Add property title and the NPC search each boosted listing belongs to.
    Search comes from the page-1 check when there is one (NPC's own category),
    otherwise from Airtable (area, sale/rent, property type)."""
    from agents.booster_npc import LOC_SLUGS, BASE_URL as NPC
    now = time.time()
    need = sorted({r["ref"] for r in rows if r.get("ref")
                   and (r["ref"] not in _boost_info_cache or now - _boost_info_cache[r["ref"]][0] > 600)})
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        formula = "OR(" + ",".join(f"{{Property ID}}='{x}'" for x in chunk) + ")"
        params = {"pageSize": 100, "filterByFormula": formula}
        for j, f in enumerate(["Property ID", "Property Title", "Main Neighbourhood Label",
                               "Name (from Listing Status)", "Property Type", "Name (from Bedroom)"]):
            params[f"fields[{j}]"] = f
        try:
            d = req.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}",
                        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"}, params=params, timeout=20).json()
            for rec in d.get("records", []):
                f = rec.get("fields", {})
                lab = f.get("Main Neighbourhood Label") or [""]
                status = " ".join(f.get("Name (from Listing Status)", [])).lower()
                _boost_info_cache[f.get("Property ID", "")] = (now, {
                    "title": f.get("Property Title", ""),
                    "location": lab[0] if isinstance(lab, list) else lab,
                    "mode": "rent" if "rent" in status else "sale",
                    "category": _category_guess(f.get("Property Type")),
                    "beds": _beds_of(f.get("Name (from Bedroom)")),
                })
        except Exception as e:
            print(f"[RECENT] airtable enrich failed: {e}", flush=True)
    saved = _load_boost_info()
    for r in rows:
        info = _boost_info_cache.get(r.get("ref"), (0, {}))[1]
        if not info:
            bi = saved.get(f"{r.get('platform')}:{r.get('listing_id')}", {})
            info = {"title": bi.get("title", ""), "location": bi.get("area", ""), "mode": bi.get("mode", ""),
                    "category": bi.get("category", ""), "beds": bi.get("bedrooms")}
        v = r.get("verify") or {}
        loc = v.get("location") or info.get("location", "")
        mode = v.get("mode") or info.get("mode", "")
        cat = v.get("category") or info.get("category", "")
        r["title"] = info.get("title", "") or v.get("title", "")
        if loc and mode and cat:
            tx = "for-rent" if mode == "rent" else "for-sale"
            slug = LOC_SLUGS.get(loc, loc.lower().replace(" ", "-"))
            beds = v.get("bedrooms") if "bedrooms" in v else info.get("beds")
            beds = beds if beds in BED_OPTIONS.get(cat, ()) else None
            r["search"] = {
                "key": f"{beds or ''}|{loc}|{cat}|{mode}", "location": loc, "category": cat, "mode": mode,
                "bedrooms": beds, "label": _search_label(beds, loc, cat, mode),
                "url": v.get("url") or f"{NPC}/{tx}/{cat}/lagos/{slug}" + (f"?bedrooms={beds}" if beds else ""),
            }
        else:
            r["search"] = None

@app.route("/api/admin/recheck-boost", methods=["POST"])
def api_recheck_boost():
    err = _require_admin()
    if err: return err
    body = request.get_json() or {}
    platform = body.get("platform", "npc_cw")
    listing_id = str(body.get("listing_id", ""))
    ref = body.get("ref", "")
    if not listing_id:
        return jsonify({"error": "listing_id required"}), 400
    threading.Thread(target=_verify_boost, args=(platform, listing_id, ref, 2), daemon=True).start()
    return jsonify({"ok": True})

# ---- Auto scan scheduler: every 4 hours 9am-9pm Africa/Lagos (UTC+1) ----
def _reco_scheduler_loop():
    from datetime import datetime, timezone, timedelta
    LAGOS = timezone(timedelta(hours=1))
    last_hour_run = None
    while True:
        try:
            now = datetime.now(LAGOS)
            hour = now.hour
            # 9,13,17,21 → four scans a day
            if hour in (9, 13, 17, 21) and now.minute < 5:
                key = f"{now.date()}-{hour}"
                if key != last_hour_run and not _reco_running["flag"]:
                    _reco_running["flag"] = True
                    threading.Thread(target=_run_reco_sweep, daemon=True).start()
                    last_hour_run = key
        except Exception:
            pass
        time.sleep(120)  # check every 2 min

if os.environ.get("CW_BACKGROUND", "1") != "0":
    threading.Thread(target=_reco_scheduler_loop, daemon=True).start()

@app.route("/api/admin/boost-log/clear", methods=["POST"])
def api_boost_log_clear():
    """Clear boost log entries. Body: {mode: "all"} or {mode: "selected", ids: [ts,ts,...]}
    ids are unix ts values that uniquely identify boost log rows."""
    err = _require_admin()
    if err: return err
    body = request.get_json() or {}
    mode = body.get("mode", "selected")
    from agents.booster_npc import _load_boosts, _save_boosts
    if mode == "all":
        _save_boosts([])
        # also wipe verification metadata
        try: open("/root/cw_listing_agent/boost_verify.json", "w").write("{}")
        except Exception: pass
        return jsonify({"ok": True, "cleared": "all"})
    ids = set(body.get("ids", []) or [])
    if not ids:
        return jsonify({"error": "no ids provided"}), 400
    boosts = _load_boosts()
    kept = [b for b in boosts if b.get("ts") not in ids]
    _save_boosts(kept)
    # also drop matching verify entries by listing_id from removed rows
    removed_keys = {b.get("listing_id") for b in boosts if b.get("ts") in ids}
    try:
        with open("/root/cw_listing_agent/boost_verify.json") as f:
            vm = json.load(f)
        for k in list(vm.keys()):
            if k in removed_keys:
                del vm[k]
        with open("/root/cw_listing_agent/boost_verify.json", "w") as f:
            json.dump(vm, f)
    except Exception: pass
    return jsonify({"ok": True, "removed": len(boosts) - len(kept)})


@app.route("/api/admin/rank-check", methods=["POST"])
def api_rank_check():
    err = _require_admin()
    if err: return err
    body = request.get_json() or {}
    from agents.booster_npc import check_ranking
    try:
        result = check_ranking(
            body.get("location", "Ikoyi"),
            body.get("mode", "sale"),
            body.get("bedrooms") or None,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502




# ---- NPC photo gallery: all photos for one listing, straight from its public page ----
_PHOTO_CACHE = {}   # (platform, lid) -> (ts, [urls])

@app.route("/api/admin/npc-photos")
def api_npc_photos():
    """Every photo url for one NPC listing, extracted from its public page HTML."""
    err = _require_admin()
    if err: return err
    lid = str(request.args.get("id", "")).strip()
    plat = request.args.get("platform", "npc_cw")
    if not lid.isdigit() or plat not in ("npc_cw", "npc_daniel"):
        return jsonify({"error": "id + platform (npc_cw|npc_daniel) required"}), 400
    hit = _PHOTO_CACHE.get((plat, lid))
    if hit and time.time() - hit[0] < 900:
        return jsonify({"id": lid, "photos": hit[1]})
    public = request.args.get("public_url", "").strip()
    from agents.poster_npc import make_session
    sess = make_session(CREDENTIALS[plat].get("proxy_url", ""))
    try:
        # 1) if the caller passed the public URL, use it directly
        # 2) otherwise ask the account's checker for a card and rebuild it
        if not public:
            try:
                st = _npc_status(plat, lid)
                slug = st.get("url_slug") or ""
                path = st.get("path") or ""
                mode = st.get("mode") or "sale"
                cat  = st.get("category") or "flats-apartments"
                if slug and path:
                    tx = "for-rent" if mode == "rent" else "for-sale"
                    public = f"https://nigeriapropertycentre.com/{tx}/{cat}/lagos/{path}/{lid}-{slug}"
            except Exception:
                pass
        if not public:
            return jsonify({"error": "Couldn\u2019t work out this listing\u2019s public URL"}), 502
        r = sess.get(public, timeout=40)
        if r.status_code != 200:
            return jsonify({"error": f"NPC returned HTTP {r.status_code}"}), 502
        text = r.text.replace("\\/", "/").replace("\\u0022", '"')
        text2 = text.replace('\\/', '/').replace('\/', '/')
        pat = re.compile(r"https://images\.nigeriapropertycentre\.com/properties/images/" + re.escape(lid)
                         + r"/[A-Za-z0-9._-]+?\.(?:webp|jpe?g|png)")
        urls = []
        seen = set()
        for src in (text, text2):
            for m in pat.finditer(src):
                u = m.group(0)
                if u not in seen:
                    seen.add(u); urls.append(u)
        _PHOTO_CACHE[(plat, lid)] = (time.time(), urls)
        return jsonify({"id": lid, "photos": urls, "public_url": public})
    finally:
        try: sess.close()
        except Exception: pass

@app.route("/api/admin/boost", methods=["POST"])
def api_boost():
    err = _require_admin()
    if err: return err
    body = request.get_json() or {}
    listing_id = str(body.get("listing_id", "")).strip()
    ref        = body.get("ref", "")
    user       = body.get("user", "Admin")
    platform   = body.get("platform", "npc_cw")
    if not listing_id:
        return jsonify({"error": "listing_id required"}), 400
    if platform not in ("npc_cw", "npc_daniel"):
        return jsonify({"error": f"Boosting only supported on NPC. '{platform}' not available."}), 400
    from agents.booster_npc import boost_guard, boost_listing, log_boost
    guard_key = f"{platform}:{listing_id}"
    acct = "npc_daniel" if platform == "npc_daniel" else "npc_cw"
    label = "NPC2" if acct == "npc_daniel" else "NPC"
    try:
        st = _npc_status(acct, listing_id)
    except Exception as e:
        return jsonify({"error": f"Couldn't reach NPC to check {ref}. Nothing was boosted; try again in a minute."}), 503
    if not st["boostable"]:
        return jsonify({"error": f"{ref} can't be boosted on {label}: {st['reason']}. No boost was used.",
                        "blocked": True, "reason": st["reason"]}), 409
    try:
        remaining = _get_checker(acct).boost(listing_id)
        log_boost(guard_key, ref, user, True, f"remaining={remaining}")
        _save_boost_info(guard_key, st)
        _schedule_verify(platform, listing_id, ref)
        return jsonify({"ok": True, "remaining": remaining})
    except Exception as e:
        log_boost(guard_key, ref, user, False, str(e))
        return jsonify({"error": str(e)}), 502


@app.route("/api/admin/boost-log")
def api_boost_log():
    err = _require_admin()
    if err: return err
    from agents.booster_npc import _load_boosts
    return jsonify({"boosts": list(reversed(_load_boosts()))[:100]})


@app.route("/start-job", methods=["POST"])
def start_job():
    body       = request.get_json() or {}
    prop       = body.get("property", {})
    platforms  = body.get("platforms", list(PLATFORM_POSTERS.keys()))
    session_id = body.get("session_id", "default")
    posted_by      = body.get("posted_by", "")
    platform_props = body.get("platform_props", {}) or None

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
        kwargs={"platform_props": platform_props},
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



@app.route("/retry-with-prop", methods=["POST"])
def retry_with_prop():
    body           = request.get_json() or {}
    platform       = body.get("platform")
    prop           = body.get("prop") or {}
    platform_props = body.get("platform_props") or {}
    session_id     = body.get("session_id") or "default"
    posted_by      = body.get("posted_by") or ""

    if not platform or platform not in PLATFORM_POSTERS:
        return jsonify({"error": f"Unknown platform: {platform}"}), 400
    if not prop.get("title"):
        return jsonify({"error": "No property data in request"}), 400

    # Collect images from session upload dir (still on disk after service restart)
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
        args=(job_id, prop, [platform], raw_paths, posted_by),
        kwargs={"platform_props": platform_props or None},
        daemon=True
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/report-to-claude", methods=["POST"])
def report_to_claude():
    body     = request.get_json() or {}
    platform = body.get("platform", "")
    error    = body.get("error", "")
    ref      = body.get("ref", "")
    job_id   = "rtc_" + uuid.uuid4().hex[:8]
    JOBS[job_id] = {"events": queue.Queue()}
    threading.Thread(target=_run_claude_fix, args=(job_id, platform, error, ref), daemon=True).start()
    return jsonify({"job_id": job_id})


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
    app.run(host="0.0.0.0", port=5001, debug=False)
