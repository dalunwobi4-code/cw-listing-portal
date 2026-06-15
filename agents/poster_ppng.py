"""
PP Nigeria (privateproperty.ng) listing poster
Handles: create listing + upload watermarked images in same session

Confirmed working flow:
1. Login → get CSRF
2. POST /property-post → get property_id
3. GET /property-pictures/{id} → get UUID
4. For each image: POST /upload-picture (AJAX) with same session
5. POST /property-pictures/{id} with same UUID + CSRF → saves all staged images
"""

import requests, re, os, time, threading
import urllib3
import cloudscraper

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Serialize logins — PP Nigeria rejects concurrent logins from the same account
_login_lock = threading.Lock()

BASE_URL = "https://privateproperty.ng"

AREA_CODES = {
    # ── Lekki (axis 6) ───────────────────────────────────────────────────────
    "Lekki":             {"area": "33",       "axis": "6",   "state": "5"},
    "Lekki Phase 1":     {"area": "33",       "axis": "6",   "state": "5"},
    "Lekki Phase 2":     {"area": "417",      "axis": "6",   "state": "5"},
    "Chevron":           {"area": "114",      "axis": "6",   "state": "5"},
    "Chevron Drive":     {"area": "114",      "axis": "6",   "state": "5"},
    "Agungi":            {"area": "162",      "axis": "6",   "state": "5"},
    "Idado":             {"area": "3005",     "axis": "6",   "state": "5"},
    "Igbo Efon":         {"area": "20566",    "axis": "6",   "state": "5"},
    "Ikate":             {"area": "599",      "axis": "6",   "state": "5"},
    "Ikate Elegushi":    {"area": "599",      "axis": "6",   "state": "5"},
    "Ikota":             {"area": "7",        "axis": "6",   "state": "5"},
    "Ilasan":            {"area": "11571209", "axis": "6",   "state": "5"},
    "Jakande":           {"area": "5397",     "axis": "6",   "state": "5"},
    "Lafiaji":           {"area": "3213",     "axis": "6",   "state": "5"},
    "Lekki Expressway":  {"area": "515",      "axis": "6",   "state": "5"},
    "Orchid":            {"area": "515",      "axis": "6",   "state": "5"},
    "Orchid Road":       {"area": "515",      "axis": "6",   "state": "5"},
    "Nicon Town":        {"area": "11571222", "axis": "6",   "state": "5"},
    "Ologolo":           {"area": "2258",     "axis": "6",   "state": "5"},
    "Oral Estate":       {"area": "11571225", "axis": "6",   "state": "5"},
    "Osapa":             {"area": "106",      "axis": "6",   "state": "5"},
    "Osapa London":      {"area": "11571252", "axis": "6",   "state": "5"},
    "VGC":               {"area": "390",      "axis": "6",   "state": "5"},
    "Victoria Garden City": {"area": "390",   "axis": "6",   "state": "5"},
    # ── Ajah (axis 62) ───────────────────────────────────────────────────────
    "Ajah":              {"area": "63",       "axis": "62",  "state": "5"},
    "Sangotedo":         {"area": "221",      "axis": "62",  "state": "5"},
    "Abraham Adesanya":  {"area": "2164",     "axis": "62",  "state": "5"},
    "Awoyaya":           {"area": "1925",     "axis": "62",  "state": "5"},
    "Thomas Estate":     {"area": "2230",     "axis": "62",  "state": "5"},
    "Badore":            {"area": "8760",     "axis": "62",  "state": "5"},
    "Abijo":             {"area": "1701",     "axis": "62",  "state": "5"},
    "Lakowe":            {"area": "11568799", "axis": "62",  "state": "5"},
    # ── Ikoyi (axis 491) ─────────────────────────────────────────────────────
    "Ikoyi":             {"area": "993",      "axis": "491", "state": "5"},
    "Old Ikoyi":         {"area": "11570822", "axis": "491", "state": "5"},
    "Banana Island":     {"area": "492",      "axis": "491", "state": "5"},
    "Osborne Foreshore": {"area": "11570825", "axis": "491", "state": "5"},
    "Parkview":          {"area": "3201",     "axis": "491", "state": "5"},
    "Falomo":            {"area": "65902",    "axis": "491", "state": "5"},
    # ── Victoria Island (axis 11) ─────────────────────────────────────────────
    "Victoria Island":   {"area": "12",       "axis": "11",  "state": "5"},
    "VI":                {"area": "12",       "axis": "11",  "state": "5"},
    "Eko Atlantic":      {"area": "11572076", "axis": "11",  "state": "5"},
    "Oniru":             {"area": "574",      "axis": "11",  "state": "5"},
    "Adeola Odeku":      {"area": "11572021", "axis": "11",  "state": "5"},
}

TYPE_CODES = {
    "Flat":                  {"type": "214", "stype": ""},
    "Apartment":             {"type": "214", "stype": ""},
    "Studio":                {"type": "214", "stype": ""},
    "Semi-detached Duplex":  {"type": "3",   "stype": "16981000"},
    "Terraced Duplex":       {"type": "3",   "stype": "16981001"},
    "Detached Duplex":       {"type": "3",   "stype": "16981003"},
    "Duplex":                {"type": "3",   "stype": ""},
    "Bungalow":              {"type": "3",   "stype": ""},
    "House":                 {"type": "3",   "stype": ""},
}

FEATURE_CODES = {
    "All Room Ensuite":   "feat[12]",
    "Swimming Pool":      "feat[2]",
    "24 Hours Security":  "feat[66]",
    "Gym":                "feat[19]",
    "Generator":          "feat[15]",
    "Boys Quarter":       "feat[4]",
    "Elevator":           "feat[27]",
    "CCTV":               "feat[70]",
    "Intercom":           "feat[57]",
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
    m = re.search(r'name="csrfToken" value="([^"]+)"', r.text)
    if not m:
        snippet = r.text[:400].replace("\n", " ")
        raise Exception(f"PP Nigeria: login page missing CSRF token (geo-block?). Status {r.status_code}. Page: {snippet}")
    csrf = m.group(1)
    r2 = session.post(f"{BASE_URL}/login", data={
        "csrfToken": csrf,
        "email": email,
        "password": password
    }, allow_redirects=True)
    if "dashboard" in r2.url or "profile" in r2.url:
        return True
    raise Exception(f"PP Nigeria login failed: {r2.url}")


def create_listing(session, prop: dict) -> str:
    """Create the property listing. Returns property_id."""
    r = session.get(f"{BASE_URL}/property-post")
    m = re.search(r'name="csrfToken" value="([^"]+)"', r.text)
    if not m:
        snippet = r.text[:400].replace("\n", " ")
        raise Exception(f"PP Nigeria: property-post page missing CSRF (not logged in?). Status {r.status_code}. Page: {snippet}")
    csrf = m.group(1)

    location = AREA_CODES.get(prop.get("location", "Lekki Phase 1"),
                               AREA_CODES["Lekki Phase 1"])
    ptype = TYPE_CODES.get(prop.get("property_type", "Flat"), TYPE_CODES["Flat"])

    data = {
        "csrfToken": csrf,
        "title": prop["title"],
        "mode": prop.get("mode", "sale"),  # sale | rent | short-let
        "type": ptype["type"],
        "stype": ptype["stype"],
        "bedroom": str(prop.get("bedrooms", 2)),
        "bathroom": str(prop.get("bathrooms", 2)),
        "toilet": str(prop.get("toilets", prop.get("bathrooms", 2))),
        "state": location["state"],
        "axis": location["axis"],
        "area": location["area"],
        "price": str(prop.get("price", 0)),
        "priceCurrency": "NAIRA",
        "appendTo": (prop.get("appendTo") or "YEAR") if prop.get("mode") in ("rent", "short-let", "short_let") else "",
        "description": prop.get("description", ""),
        "street": prop.get("street", ""),
    }

    # Add features
    for feature in prop.get("features", []):
        code = FEATURE_CODES.get(feature)
        if code:
            data[code] = "on"

    r2 = session.post(f"{BASE_URL}/property-post", data=data, allow_redirects=True)

    # Extract property ID from redirect URL: /property-pictures/{id}
    match = re.search(r"/property-pictures/(\d+)", r2.url)
    if not match:
        # Try in response body
        match = re.search(r"/property-pictures/(\d+)", r2.text)
    if not match:
        raise Exception(f"PP Nigeria: Could not get property ID. URL: {r2.url}")

    return match.group(1)


def upload_images(session, property_id: str, image_paths: list, title: str) -> int:
    """Upload images to an existing listing. Returns count uploaded."""
    # Get UUID from pictures page
    r = session.get(f"{BASE_URL}/property-pictures/{property_id}")
    uuid_match = re.search(r'name="uuid" value="([^"]+)"', r.text)
    if not uuid_match:
        raise Exception(f"PP Nigeria: Could not get UUID for property {property_id}")
    uuid = uuid_match.group(1)

    uploaded = 0
    for i, img_path in enumerate(image_paths):
        for attempt in range(5):
            try:
                with open(img_path, "rb") as f:
                    resp = session.post(
                        f"{BASE_URL}/upload-picture",
                        data={"index": str(i), "title": title, "uuid": uuid},
                        files=[("file-0", (os.path.basename(img_path), f, "image/jpeg"))],
                        headers={
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"{BASE_URL}/property-pictures/{property_id}"
                        },
                        allow_redirects=False,
                        timeout=60
                    )
                if resp.status_code == 200:
                    uploaded += 1
                    break
            except Exception as e:
                if attempt < 4:
                    time.sleep(3)
                else:
                    print(f"  [PPNG] Failed to upload {os.path.basename(img_path)}: {e}")
        time.sleep(0.8)

    # SAVE — must use same session
    r2 = session.get(f"{BASE_URL}/property-pictures/{property_id}")
    save_m = re.search(r'name="csrfToken" value="([^"]+)"', r2.text)
    if not save_m:
        return uploaded  # images already uploaded, skip save step
    save_csrf = save_m.group(1)
    session.post(
        f"{BASE_URL}/property-pictures/{property_id}",
        data={"csrfToken": save_csrf, "uuid": uuid},
        allow_redirects=True
    )

    return uploaded


def get_public_url(session, property_id: str) -> str:
    """Try to extract the public listing URL from the property management page."""
    try:
        r = session.get(f"{BASE_URL}/property-pictures/{property_id}", timeout=15)
        # Look for canonical URL first
        m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        # Look for og:url
        m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        # Look for a "view" or "preview" link
        m = re.search(r'href=["\'](' + re.escape(BASE_URL) + r'/(?:property|listing|real-estate)/[^"\']+)["\']', r.text)
        if m: return m.group(1)
    except Exception:
        pass
    return ""


def post_property(prop: dict, image_paths: list, credentials: dict) -> dict:
    """
    Full flow: login → create → upload images → return result.
    prop keys: title, mode, property_type, location, bedrooms, bathrooms,
               price, description, street, features (list)
    """
    result = {"ref": prop.get("ref", ""), "platform": "ppng", "success": False}
    count = 0
    proxy_url = credentials.get("proxy_url", "")

    try:
        # Stagger logins — PP Nigeria rejects concurrent logins from same account
        with _login_lock:
            session = make_session(proxy_url)
            login(session, credentials["email"], credentials["password"])
            property_id = create_listing(session, prop)
            time.sleep(1)  # release lock after listing is created

        result["property_id"] = property_id
        result["url"] = f"{BASE_URL}/property-pictures/{property_id}"

        if image_paths:
            count = upload_images(session, property_id, image_paths, prop["title"])
            result["images_uploaded"] = count

        result["success"] = True
        result["listing_url"] = f"{BASE_URL}/property-edit/{property_id}"
        print(f"[PPNG] ✓ {prop.get('ref')} → ID {property_id} | {count} images | {result['listing_url']}")

    except Exception as e:
        result["error"] = str(e)
        print(f"[PPNG] ✗ {prop.get('ref')}: {e}")

    return result
