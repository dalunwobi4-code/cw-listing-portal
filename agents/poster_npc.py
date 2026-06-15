"""
Nigeria Property Centre (nigeriapropertycentre.com) listing poster

Confirmed working flow:
1. Login: POST /login with _token + email + password
2. Create listing: POST /account/listings with all fields + agent_id
   → redirects to /account/listings/{id}/images
3. Upload images: POST /ajax/listings/{id}/images with files[] (one at a time)
   → returns JSON with image names/URLs
4. Finalize: POST /account/listings/{id}/images/task with task=saveOrder
   → redirects back to images page (done)

Key: agent_id must be included (from create page hidden input: 86702)
"""

import requests, re, os, time, json
import urllib3
import cloudscraper

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://nigeriapropertycentre.com"

LOCATION_CODES = {
    "Lekki Phase 1": {"sid": "2", "lid": "3", "slid": "669",
                      "propertyLocation": "Lekki Phase 1, Lekki, Lagos"},
    "Lekki Phase 2": {"sid": "2", "lid": "3", "slid": "",
                      "propertyLocation": "Lekki Phase 2, Lekki, Lagos"},
    "Victoria Island": {"sid": "2", "lid": "2", "slid": "",
                        "propertyLocation": "Victoria Island (VI), Lagos"},
    "VI":              {"sid": "2", "lid": "2", "slid": "",
                        "propertyLocation": "Victoria Island (VI), Lagos"},
    "Ikoyi":           {"sid": "2", "lid": "20", "slid": "",
                        "propertyLocation": "Ikoyi, Lagos"},
    "Old Ikoyi":       {"sid": "2", "lid": "20", "slid": "1017",
                        "propertyLocation": "Old Ikoyi, Ikoyi, Lagos"},
    "Banana Island":   {"sid": "2", "lid": "20", "slid": "682",
                        "propertyLocation": "Banana Island, Ikoyi, Lagos"},
    "Lekki":           {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Lekki, Lagos"},
    "Chevron Drive":   {"sid": "2", "lid": "3", "slid": "671",
                        "propertyLocation": "Chevron Drive, Lekki, Lagos"},
    "Chevron":         {"sid": "2", "lid": "3", "slid": "671",
                        "propertyLocation": "Chevron Drive, Lekki, Lagos"},
    "Oniru":           {"sid": "2", "lid": "2", "slid": "",
                        "propertyLocation": "Oniru, Victoria Island, Lagos"},
    "Osapa":           {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Osapa London, Lekki, Lagos"},
    "Osapa London":    {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Osapa London, Lekki, Lagos"},
    "Ajah":            {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Ajah, Lagos"},
    "Ikate":           {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Ikate, Lekki, Lagos"},
    "Orchid":          {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Orchid Road, Lekki, Lagos"},
    "VGC":             {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Victoria Garden City (VGC), Lekki, Lagos"},
    "Eko Atlantic":    {"sid": "2", "lid": "2", "slid": "",
                        "propertyLocation": "Eko Atlantic, Victoria Island, Lagos"},
    "Idado":           {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Idado, Lekki, Lagos"},
    "Agungi":          {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Agungi, Lekki, Lagos"},
    "Sangotedo":       {"sid": "2", "lid": "3", "slid": "",
                        "propertyLocation": "Sangotedo, Ajah, Lagos"},
}

TYPE_CODES = {
    # ── Flat / Apartment (tid=1) ──────────────────────────────────────────────
    "Flat":                  {"tid": "1", "stid": ""},
    "Apartment":             {"tid": "1", "stid": ""},
    "Studio":                {"tid": "1", "stid": ""},
    # ── House / Duplex / Bungalow (tid=2) ────────────────────────────────────
    "House":                 {"tid": "2", "stid": ""},
    "Duplex":                {"tid": "2", "stid": ""},
    "Detached Duplex":       {"tid": "2", "stid": ""},
    "Semi-detached Duplex":  {"tid": "2", "stid": ""},
    "Terraced Duplex":       {"tid": "2", "stid": ""},
    "Bungalow":              {"tid": "2", "stid": ""},
}

MODE_MAP = {
    "sale":      "2",  # For Sale
    "rent":      "1",  # For Rent
    "short_let": "4",  # Short Let
    "short-let": "4",  # Short Let (hyphen variant from parser)
}


def make_session(proxy_url: str = ""):
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s


def login(session, email, password):
    r = session.get(f"{BASE_URL}/login")
    m = re.search(r'name="_token" value="([^"]+)"', r.text)
    if not m:
        snippet = r.text[:400].replace("\n", " ")
        raise Exception(f"NPC: login page missing _token (geo-block?). Status {r.status_code}. Page: {snippet}")
    csrf = m.group(1)
    r2 = session.post(f"{BASE_URL}/login", data={
        "_token": csrf,
        "email": email,
        "password": password
    }, allow_redirects=True)
    if "dashboard" in r2.url or "account" in r2.url:
        return True
    raise Exception(f"NPC login failed: {r2.url}")


def create_listing(session, prop: dict) -> tuple:
    """Create listing. Returns (listing_id, images_url)."""
    r = session.get(f"{BASE_URL}/account/listings/create")
    m = re.search(r'name="_token"\s+value="([^"]+)"', r.text)
    if not m:
        snippet = r.text[:400].replace("\n", " ")
        raise Exception(f"NPC: listings/create page missing _token (session expired?). Status {r.status_code}. Page: {snippet}")
    token = m.group(1)
    agent_id = re.search(r'name="agent_id"[^>]*value="([^"]+)"', r.text)
    agent_name = re.search(r'name="agent"[^>]*value="([^"]+)"', r.text)

    location = LOCATION_CODES.get(prop.get("location", "Lekki Phase 1"),
                                   LOCATION_CODES["Lekki Phase 1"])
    mode = prop.get("mode", "sale")
    cid = MODE_MAP.get(mode, "2")
    ptype = TYPE_CODES.get(prop.get("property_type", "Flat"),
                           TYPE_CODES.get("House", {"tid": "2", "stid": ""})
                           if any(w in prop.get("property_type", "") for w in ("Duplex", "Bungalow", "House"))
                           else TYPE_CODES["Flat"])

    data = {
        "_token": token,
        "task": "save",
        "event_task": "",
        "agent_id": agent_id.group(1) if agent_id else "86702",
        "agent": agent_name.group(1) if agent_name else "cw real estate -lekki",
        "add_edit_photos": "0",
        "event_code": "",
        "published": "1",
        "name": prop["title"],
        "available": "0",
        "cid": cid,
        "tid": ptype["tid"],
        "stid": ptype.get("stid", ""),
        "sid": location["sid"],
        "lid": location["lid"],
        "slid": location["slid"],
        "propertyLocation": location["propertyLocation"],
        "currency": "NGN",
        "price": str(prop.get("price", 0)),
        "price_qualifier": "0",
        "bedrooms": str(prop.get("bedrooms", 2)),
        "bathrooms": str(prop.get("bathrooms", 2)),
        "toilets": str(prop.get("toilets", prop.get("bathrooms", 2))),
        "service_charge": "",
        "service_charge_qualifier": "0",
        "service_charge_currency": "NGN",
        "text": prop.get("description", ""),
        "address": prop.get("street", ""),
        "area": "",
        "garage": "",
        "n": "",
        "requested_location": "",
    }

    r2 = session.post(f"{BASE_URL}/account/listings", data=data, allow_redirects=False)

    # Expect redirect to /account/listings/{id}/images
    location_header = r2.headers.get("Location", "")
    match = re.search(r"/account/listings/(\d+)/images", location_header)
    if not match:
        # Try following the redirect
        r3 = session.get(location_header, allow_redirects=True) if location_header else r2
        match = re.search(r"/account/listings/(\d+)", r3.url)

    if not match:
        raise Exception(f"NPC: Could not get listing ID. Location: {location_header}")

    listing_id = match.group(1)
    return listing_id, f"{BASE_URL}/account/listings/{listing_id}/images"


def upload_images(session, listing_id: str, image_paths: list) -> int:
    """Upload images to listing. Returns count uploaded."""
    r = session.get(f"{BASE_URL}/account/listings/{listing_id}/images")
    meta_csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
    if meta_csrf:
        session.headers["X-CSRF-TOKEN"] = meta_csrf.group(1)

    uploaded = 0
    for img_path in image_paths:
        for attempt in range(4):
            try:
                with open(img_path, "rb") as f:
                    resp = session.post(
                        f"{BASE_URL}/ajax/listings/{listing_id}/images",
                        files=[("files[]", (os.path.basename(img_path), f, "image/jpeg"))],
                        headers={
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{BASE_URL}/account/listings/{listing_id}/images",
                            "Accept": "application/json"
                        },
                        timeout=60
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("files"):
                        uploaded += 1
                        break
            except Exception as e:
                if attempt < 3:
                    time.sleep(2)
                else:
                    print(f"  [NPC] Failed to upload {os.path.basename(img_path)}: {e}")
        time.sleep(0.5)

    # Finalize image order
    r2 = session.get(f"{BASE_URL}/account/listings/{listing_id}/images")
    token = re.search(r'name="_token"\s+value="([^"]+)"', r2.text)
    if token:
        session.post(
            f"{BASE_URL}/account/listings/{listing_id}/images/task",
            data={
                "_token": token.group(1),
                "task": "saveOrder",
                "upload_type": "0",
                "loc_long": "",
                "loc_lat": "",
                "redirect": f"{BASE_URL}/account/listings/{listing_id}/images"
            },
            headers={"Referer": f"{BASE_URL}/account/listings/{listing_id}/images"},
            allow_redirects=False
        )

    return uploaded


def get_public_url(session, listing_id: str) -> str:
    """Try to extract the public listing URL from the NPC management page."""
    try:
        r = session.get(f"{BASE_URL}/account/listings/{listing_id}", timeout=15)
        m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        # Look for a link to the public listing page
        m = re.search(r'href=["\'](' + re.escape(BASE_URL) + r'/(?:listing|listings|property)[^"\']+)["\']', r.text)
        if m: return m.group(1)
    except Exception:
        pass
    return ""


def post_property(prop: dict, image_paths: list, credentials: dict) -> dict:
    """Full flow: login → create → upload images → return result."""
    result = {"ref": prop.get("ref", ""), "platform": "npc", "success": False}
    proxy_url = credentials.get("proxy_url", "")
    session = make_session(proxy_url)

    try:
        login(session, credentials["email"], credentials["password"])
        listing_id, images_url = create_listing(session, prop)
        result["listing_id"] = listing_id
        result["images_url"] = images_url

        if image_paths:
            count = upload_images(session, listing_id, image_paths)
            result["images_uploaded"] = count

        result["success"] = True
        result["listing_url"] = f"{BASE_URL}/account/listings/{listing_id}"
        print(f"[NPC] ✓ {prop.get('ref')} → ID {listing_id} | {result.get('images_uploaded', 0)} images | {result['listing_url']}")

    except Exception as e:
        result["error"] = str(e)
        print(f"[NPC] ✗ {prop.get('ref')}: {e}")

    return result
