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

LOCATIONS = ["Lekki Phase 1", "Victoria Island", "Ikoyi", "Lekki"]

# Aliases map common spelling variants → canonical location name.
# Order matters: more specific entries must come before substrings they contain.
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

    # ── land detection ────────────────────────────────────────────────────
    # Primary: explicit tag the team puts at the top of every land listing
    _LAND_TAG = re.compile(r'^\s*(?:land\s+listing|#land|type\s*:\s*land)\b', re.IGNORECASE | re.MULTILINE)
    # Secondary: genuinely land-specific terms (not sqm — that appears in commercial floor space too)
    _LAND_SIGNALS = re.compile(
        r"\bplot\b|\bdeed\s+of\s+assign\b",
        re.IGNORECASE
    )
    is_land = (bool(_LAND_TAG.search(text))
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
    property_type = "Flat"
    if is_land:
        property_type = "Land"
    else:
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


def run_job(job_id, prop, platforms, raw_image_paths):
    q = JOBS[job_id]["events"]
    airtable_selected = False
    try:
        emit(q, "log", {"msg": f"Getting next Property ID from Airtable…"})
        with _PID_LOCK:
            pid = get_next_land_pid() if prop.get("is_land") else get_next_pid()
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


@app.route("/start-job", methods=["POST"])
def start_job():
    body       = request.get_json() or {}
    prop       = body.get("property", {})
    platforms  = body.get("platforms", list(PLATFORM_POSTERS.keys()))
    session_id = body.get("session_id", "default")

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
        args=(job_id, prop, platforms, raw_paths),
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


if __name__ == "__main__":
    UPLOADS_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    print(f"CW Lagos Listing Portal → http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
