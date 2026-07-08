"""
Airtable poster — uploads original (non-watermarked) images to Lagos Properties table.
Watermark is applied by the Framer frame on cwlagos.com, not here.

Table structure:
- 'Property ID'    → CW reference number (CW08494 etc.) — used to find the record
- 'Thumbnail Image' → first/primary image
- 'Image 1' through 'Image 7' → remaining images (max 8 total)

API endpoints:
- Records: GET https://api.airtable.com/v0/{baseId}/{table}
- Attachments: POST https://content.airtable.com/v0/{baseId}/{recordId}/{fieldId}/uploadAttachment
"""

import requests, os, time, base64

BASE_ID = "appui0GIVsqalToEa"
TABLE = "Lagos Properties"
API = "https://api.airtable.com/v0"
CONTENT_API = "https://content.airtable.com/v0"

# ── Linked record IDs ────────────────────────────────────────────────────────

LISTING_STATUS = {
    "sale":      "recGfYIASKLnzADV2",   # For Sale
    "rent":      "rec9cXDBpwqq6URo6",   # For Rent
    "short-let": "rec9cXDBpwqq6URo6",   # For Rent (closest match)
    "land":      "recGzNMNc3Z6uQNoc",   # Land
}

# Main neighborhoods (Neigborhoods table) — used when location is itself a main area
MAIN_NEIGHBORHOOD_IDS = {
    "lekki phase 1":   "recwrX2yZpHmCybue",
    "lekki phase 2":   "recwrX2yZpHmCybue",
    "lekki":           "recT8VY2sQLAl7y7n",
    "victoria island": "recZ4mpm64vHO4PHx",
    "vi":              "recZ4mpm64vHO4PHx",
    "ikoyi":           "recJDjo6ORlluSgGF",
}

# Sub neighborhoods → (sub_record_id, main_neighborhood_record_id)
SUB_NEIGHBORHOOD_IDS = {
    "ikate":                ("rec1IsdwSRZyWyAUD", "recT8VY2sQLAl7y7n"),   # → Lekki
    "parkview":             ("rec4QybAeVcGEAePi", "recJDjo6ORlluSgGF"),   # → Ikoyi
    "lekki county":         ("rec7pcB1iRI3yMW5p", "recT8VY2sQLAl7y7n"),   # → Lekki
    "osapa":                ("rec7t6UsFE3krtlBv", "recT8VY2sQLAl7y7n"),   # → Lekki
    "osapa london":         ("rec7t6UsFE3krtlBv", "recT8VY2sQLAl7y7n"),   # → Lekki
    "ikota":                ("recAbb59JTidMqHZr", "recT8VY2sQLAl7y7n"),   # → Lekki
    "vgc":                  ("recCBk1ATmNAxdT6H", "recT8VY2sQLAl7y7n"),   # → Lekki
    "victoria garden city": ("recCBk1ATmNAxdT6H", "recT8VY2sQLAl7y7n"),   # → Lekki
    "hampton bay estate":   ("recFlO6ZtNY2c7zgv", "recT8VY2sQLAl7y7n"),   # → Lekki
    "victory park estate":  ("recGAyX2GepOdCBaF", None),
    "orchid":               ("recGtPOsSQYQEgo4C", "recT8VY2sQLAl7y7n"),   # → Lekki
    "orchid road":          ("recGtPOsSQYQEgo4C", "recT8VY2sQLAl7y7n"),   # → Lekki
    "old ikoyi":            ("recJ5dVS8J5FkLVjO", "recJDjo6ORlluSgGF"),   # → Ikoyi
    "harris drive":         ("recMMFpWN346d6Oj5", None),
    "pinnock beach estate": ("recPZraq4OYZ9BBja", "recT8VY2sQLAl7y7n"),   # → Lekki
    "osborne foreshore":    ("recUOpJ59cJmpnnbo", "recJDjo6ORlluSgGF"),   # → Ikoyi
    "lekki phase 1":        ("recUUyTsrOzxMBcyE", "recwrX2yZpHmCybue"),   # → Lekki Phase 1
    "lekki phase 2":        ("recUUyTsrOzxMBcyE", "recwrX2yZpHmCybue"),   # → Lekki Phase 1
    "ilasan":               ("recVhmlJKHXwLxi8N", "recT8VY2sQLAl7y7n"),   # → Lekki
    "oral estate":          ("recWTeuhOsmeK392c", "recT8VY2sQLAl7y7n"),   # → Lekki
    "adeola odeku":         ("recWd0bWRYBa4rqZX", "recZ4mpm64vHO4PHx"),   # → VI
    "eko atlantic":         ("recegA3s192DyEuxQ", "recZ4mpm64vHO4PHx"),   # → VI
    "admiralty way":        ("recgoMnNJqqFOSbPF", "recwrX2yZpHmCybue"),   # → Lekki Phase 1
    "ajah":                 ("reciQB5ZOiZDQ1hci", "recT8VY2sQLAl7y7n"),   # → Lekki
    "chevron":              ("reclH3Dc06ZZLnCYE", "recT8VY2sQLAl7y7n"),   # → Lekki
    "chevron drive":        ("reclH3Dc06ZZLnCYE", "recT8VY2sQLAl7y7n"),   # → Lekki
    "oniru":                ("reclT5ncEt87XLMCi", "recZ4mpm64vHO4PHx"),   # → VI
    "ologolo":              ("reclv3WYVQE7J27Xh", "recT8VY2sQLAl7y7n"),   # → Lekki
    "banana island":        ("recmsira6KmKD3xps", "recJDjo6ORlluSgGF"),   # → Ikoyi
    "beach resort estate":  ("recn8Mj08RUmvRVmD", None),
    "ibeju lekki":          ("recodbyk4ibspETA0", "recT8VY2sQLAl7y7n"),   # → Lekki
    "cowrie creek estate":  ("recqM1JVV6s9Skvij", "recT8VY2sQLAl7y7n"),   # → Lekki
    "victoria crest estate":("recr2E3i8YITLHM51", None),
    "idado":                ("recsJ2v7Dj85MW4dv", "recT8VY2sQLAl7y7n"),   # → Lekki
    "sangotedo":            ("recwt5m9BZddbINgk", "recT8VY2sQLAl7y7n"),   # → Lekki
    "freedom way":          ("recxvaekY8SMo7TAZ", "recwrX2yZpHmCybue"),   # → Lekki Phase 1
}

BEDROOM_IDS = {
    1: "recXiSYjrzgrZCaCw",
    2: "recsTcIT7W6zJSpJ8",
    3: "recP3Ztosy2GGveqP",
    4: "receZWCuDEyyaIiNv",
    5: "recHziRkdHjzeIih5",
    6: "recXByvpwmHB9O0Su",
    7: "rec2CYBcUC82DO9id",
    8: "recTKyrGnD7gigI4W",
    9: "rechgXScRcaa3nSF2",
}

PROPERTY_TYPE_MAP = {
    "Apartment":            "Apartment",
    "Flat":                 "Apartment",
    "Studio":               "Apartment",
    "Semi-detached Duplex": "Semi Detached",
    "Terraced Duplex":      "Terrace",
    "Detached Duplex":      "Detached Duplex",
    "Duplex":               "Detached Duplex",
    "Bungalow":             "Detached Duplex",
    "House":                "Detached Duplex",
    "Penthouse":            "Penthouse",
    # Land subtypes
    "Mixed Use Land":       "Mixed Use Land",
    "Commercial Land":      "Commercial Land",
    "Residential Land":     "Residential Land",
    "Joint Venture":        "Joint Venture",
    "Industrial Land":      "Industrial Land",
}

# Field IDs (from schema)
FIELD_IDS = {
    "Thumbnail Image": "fldlmg9AnneaxtPvH",
    "Image 1": "fldm51ndNlhLWMjJi",
    "Image 2": "fldMfqip8goKYh6L8",
    "Image 3": "fldxL7bs0Y9gp9uoU",
    "Image 4": "fldNwMb3aYDAJAvF0",
    "Image 5": "flda3Eb6x5XrbmAFW",
    "Image 6": "fld7HtcLjOjDMM9ZJ",
    "Image 7": "fldoM6B6ASB5pXCtI",
}

# Order to fill image slots
IMAGE_SLOT_ORDER = [
    "Thumbnail Image",
    "Image 1", "Image 2", "Image 3", "Image 4",
    "Image 5", "Image 6", "Image 7",
]


def find_record(token: str, ref: str):
    """Find the Airtable record by Property ID (e.g. CW08494)."""
    r = requests.get(
        f"{API}/{BASE_ID}/{TABLE}",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "filterByFormula": f"{{Property ID}}='{ref}'",
            "maxRecords": 1
        },
        timeout=15
    )
    records = r.json().get("records", [])
    return records[0] if records else None


def _bedroom_range(bedrooms: int) -> str:
    if bedrooms <= 2:   return "1-2 Bedrooms"
    if bedrooms <= 5:   return "3-5 Bedrooms"
    if bedrooms <= 10:  return "6-10 Bedrooms"
    return "11+ Bedrooms"


def _price_range(price: int) -> str:
    if price < 50_000_000:    return "Under ₦50M"
    if price < 200_000_000:   return "₦50M – ₦200M"
    if price < 500_000_000:   return "₦200M – ₦500M"
    if price < 1_000_000_000: return "₦500M – ₦1B"
    return "Above ₦1B"


def create_record(token: str, prop: dict) -> str:
    """Create a new Airtable record for this property. Returns record_id."""
    price     = prop.get("price", 0)
    price_str = f"₦{price:,}" if price else ""
    mode      = prop.get("mode", "sale")
    location  = prop.get("location", "")
    bedrooms  = prop.get("bedrooms", 0)
    ptype     = prop.get("property_type", "")
    loc_lower = location.lower()

    fields = {
        "Property ID":    prop.get("ref", ""),
        "Property Title": prop.get("title", ""),
        "Overview":       prop.get("description", ""),
        "New Price":      price_str,
    }

    # Listing Status (linked record) — derive from property_type so edits carry through
    is_land = prop.get("property_type", "") == "Land"
    status_key = "land" if is_land else mode
    status_id = LISTING_STATUS.get(status_key)
    if status_id:
        fields["Listing Status"] = [status_id]

    # Neighborhood: check sub-neighborhoods first, then main neighborhoods
    sub_info = SUB_NEIGHBORHOOD_IDS.get(loc_lower)
    if sub_info:
        sub_id, main_id = sub_info
        if sub_id:
            fields["Sub Neighborhood"] = [sub_id]
        if main_id:
            fields["Main Neighborhood"] = [main_id]
    else:
        main_id = MAIN_NEIGHBORHOOD_IDS.get(loc_lower)
        if main_id:
            fields["Main Neighborhood"] = [main_id]

    # Bedroom (linked record)
    bed_id = BEDROOM_IDS.get(int(bedrooms) if bedrooms else 0)
    if bed_id:
        fields["Bedroom"] = [bed_id]

    # Bedroom Range (singleSelect)
    if bedrooms:
        fields["Bedroom Range"] = _bedroom_range(int(bedrooms))

    # Price Range (singleSelect)
    if price:
        fields["Price Range"] = _price_range(int(price))

    # Property Type (singleSelect)
    type_key = prop.get("land_subtype", "Mixed Use Land") if is_land else ptype
    airtable_type = PROPERTY_TYPE_MAP.get(type_key)
    if airtable_type:
        fields["Property Type"] = airtable_type

    # Agent (linked record)
    agent_id = prop.get("agent_id")
    if agent_id:
        fields["Agent"] = [agent_id]

    # Contact number
    contact = prop.get("contact", "")
    if contact:
        fields["Contact"] = contact

    # Coordinates / Pinpoint
    coordinates = prop.get("coordinates", "")
    if coordinates:
        fields["Pinpoint"] = coordinates

    r = requests.post(
        f"{API}/{BASE_ID}/{TABLE}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": fields},
        timeout=15
    )
    data = r.json()
    if "id" not in data:
        raise Exception(f"Airtable create failed: {data}")
    return data["id"]


def _make_slug(title: str) -> str:
    """Convert a property title to a URL slug (lowercase, hyphens)."""
    import re as _re
    slug = title.lower()
    slug = _re.sub(r"[^\w\s-]", "", slug)
    slug = _re.sub(r"[\s_]+", "-", slug.strip())
    slug = _re.sub(r"-+", "-", slug)
    return slug.strip("-")


def get_property_link(token: str, record_id: str, prop: dict = None) -> str:
    """Read the auto-generated Property Link formula field from the record.
    Retries once after a short delay to allow Airtable formula to compute.
    Falls back to constructing URL from property title + ref if field is empty.
    """
    for attempt in range(3):
        try:
            r = requests.get(
                f"{API}/{BASE_ID}/{TABLE}/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields[]": "Property Link"},
                timeout=15
            )
            raw = r.json().get("fields", {}).get("Property Link", "")
            if raw:
                if not raw.startswith("http"):
                    raw = "https://" + raw
                return raw
        except Exception:
            pass
        time.sleep(1.5)

    # Fallback: construct from title + ref (matches cwlagos formula pattern)
    if prop:
        slug = _make_slug(prop.get("title", ""))
        ref  = prop.get("ref", "").lower()
        if slug and ref:
            return f"https://cwlagos.com/property/{slug}-{ref}"
    return ""


def upload_attachment(token: str, record_id: str, field_id: str, image_path: str) -> dict:
    """Upload a single image to a specific attachment field using base64 JSON."""
    url = f"{CONTENT_API}/{BASE_ID}/{record_id}/{field_id}/uploadAttachment"
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json={
            "contentType": "image/png" if image_path.lower().endswith(".png") else "image/jpeg",
            "filename": filename,
            "file": b64
        },
        timeout=60
    )
    if r.status_code in (200, 201):
        return {"success": True, "field_id": field_id}
    return {"success": False, "error": r.text, "status": r.status_code}


def post_property(prop: dict, image_paths: list, credentials: dict) -> dict:
    """
    Upload watermarked images to Airtable Lagos Properties record.
    Fills Thumbnail Image, Image 1, Image 2 ... Image 7 in order.
    Max 8 images total.
    """
    token = credentials["token"]
    result = {"ref": prop.get("ref", ""), "platform": "airtable", "success": False}

    try:
        # Find or create the record
        record = find_record(token, prop.get("ref", ""))
        if record:
            record_id = record["id"]
            # Update agent link if provided
            agent_id = prop.get("agent_id")
            if agent_id:
                requests.patch(
                    f"{API}/{BASE_ID}/{TABLE}/{record_id}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"fields": {"Agent": [agent_id]}}
                )
        else:
            record_id = create_record(token, prop)

        result["record_id"] = record_id

        # Upload images into slots (max 8)
        slots = IMAGE_SLOT_ORDER[:len(image_paths)]
        uploaded = 0

        for slot_name, img_path in zip(slots, image_paths):
            field_id = FIELD_IDS[slot_name]
            for attempt in range(3):
                r = upload_attachment(token, record_id, field_id, img_path)
                if r.get("success"):
                    uploaded += 1
                    break
                elif attempt < 2:
                    time.sleep(2)
                else:
                    print(f"  [AIRTABLE] Failed {slot_name}: {r.get('error', '')[:80]}")
            time.sleep(0.3)

        result["images_uploaded"] = uploaded
        result["success"] = True  # record created/updated successfully

        # Build the public URL directly — same formula as cwlagos.com Property Link field:
        # https://cwlagos.com/property/{title-slug}-{pid-lowercase}
        # Constructing locally avoids any Airtable formula timing/API issues.
        slug     = _make_slug(prop.get("title", ""))
        ref_low  = prop.get("ref", "").lower()
        listing_url = (f"https://cwlagos.com/property/{slug}-{ref_low}"
                       if slug and ref_low else "")
        result["listing_url"] = listing_url

        print(f"[AIRTABLE] ✓ {prop.get('ref')} → record {record_id} | {uploaded}/{len(slots)} images | {listing_url}")

    except Exception as e:
        result["error"] = str(e)
        print(f"[AIRTABLE] ✗ {prop.get('ref')}: {e}")

    return result
