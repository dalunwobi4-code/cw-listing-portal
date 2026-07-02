"""
PropertyPro (propertypro.ng) listing poster

Known working flow:
- Login: POST /login
- Create listing: POST /property-post → redirect to /property-edit/{id}?isnewposting=new-posting
- Image upload: POST /upload-picture-new (AJAX, one file at a time returns "1")
- Save images: POST /property-pictures/{id} with uuid, primaryImage, deleteImages

IMPORTANT: Do NOT use urllib3 Retry adapter — it causes PropertyPro to return 400 on
property creation. Use a plain requests.Session() with no retry adapter.

IMPORTANT: PropertyPro requires a valid/active plan to post listings. If the account's
plan is expired or quota is exhausted, rent listings will return 400. Sale listings may
still work temporarily. Renew the plan at propertypro.ng/subscription to fix this.

NOTE: Multi-image upload — only 1 image persists after save (known PropertyPro bug).
"""

import requests, re, os, time, threading
import urllib3
import cloudscraper

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# PropertyPro rejects concurrent logins from the same account.
# We use a shared session (logged in once) and serialize ALL property postings.
# Image uploads can still run in parallel after listing creation.
_post_lock = threading.Lock()
_shared_session = None
_shared_credentials = None

BASE_URL = "https://propertypro.ng"

AREA_CODES = {
    "Lekki Phase 1": {"area": "350", "axis": "12", "state": "1"},
    "Chevron Drive":  {"area": "282", "axis": "12", "state": "1"},
    "Lekki":          {"area": "350", "axis": "12", "state": "1"},
    "Victoria Island": {"area": "341","axis": "12", "state": "1"},
    "Ikoyi":          {"area": "349", "axis": "12", "state": "1"},
}

TYPE_CODES = {
    "Flat":                  {"type": "6",  "stype": ""},
    "Apartment":             {"type": "6",  "stype": ""},
    "Studio":                {"type": "6",  "stype": ""},
    "Semi-detached Duplex":  {"type": "13", "stype": "7"},
    "Terraced Duplex":       {"type": "13", "stype": "8"},
    "Detached Duplex":       {"type": "13", "stype": "9"},
    "Duplex":                {"type": "13", "stype": ""},
    "Bungalow":              {"type": "13", "stype": ""},
    "House":                 {"type": "13", "stype": ""},
    "Land":                  {"type": "21", "stype": ""},
}

FEATURE_CODES = {
    "All Room Ensuite":  "feat[16]",
    "Swimming Pool":     "feat[2]",
    "24 Hours Security": "feat[66]",
    "Gym":               "feat[63]",
    "Generator":         "feat[27]",
    "Boys Quarter":      "feat[4]",
    "Elevator":          "feat[19]",
    "CCTV":              "feat[70]",
    "Intercom":          "feat[57]",
}


def make_session(proxy_url: str = ""):
    # cloudscraper mimics Chrome TLS fingerprint — bypasses Cloudflare on Railway's IP
    # NOTE: No retry adapter — PropertyPro returns 400 on property creation with retries
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s


def login(session, email, password):
    # GET login page first to grab CSRF token and establish cookies
    r0 = session.get(f"{BASE_URL}/login", allow_redirects=True)
    # Try to extract CSRF token (PropertyPro may require it on POST)
    csrf_m = (re.search(r'name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']', r0.text) or
              re.search(r'name=["\']csrfToken["\'][^>]+value=["\']([^"\']+)["\']', r0.text) or
              re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', r0.text))
    data = {
        "email": email,
        "password": password,
        "keepAlive": "on",
        "lastUrl": ""
    }
    if csrf_m:
        data["_token"] = csrf_m.group(1)
    r = session.post(f"{BASE_URL}/login", data=data, allow_redirects=True)
    if "dashboard" in r.url or "profile" in r.url:
        return True
    snippet = r.text[:300].replace("\n", " ")
    raise Exception(f"PropertyPro login failed: {r.url} | Page: {snippet}")


def create_listing(session, prop: dict) -> tuple:
    """Create listing. Returns (property_id, edit_url, uuid)."""
    location = AREA_CODES.get(prop.get("location", "Lekki Phase 1"),
                               AREA_CODES["Lekki Phase 1"])
    ptype = TYPE_CODES.get(prop.get("property_type", "Flat"), TYPE_CODES["Flat"])

    data = {
        "title": prop["title"],
        "mode": prop.get("mode", "sale"),
        "state": location["state"],
        "axis": location["axis"],
        "area": location["area"],
        "type": ptype["type"],
        "stype": ptype["stype"],
        "price": str(prop.get("price", 0)),
        "priceCurrency": "NAIRA",
        "appendTo": (prop.get("appendTo") or "YEAR") if prop.get("mode") in ("rent", "short_let", "short-let") else "",
        "bedroom": str(prop.get("bedrooms", 2)),
        "bathroom": str(prop.get("bathrooms", 2)),
        "toilet": str(prop.get("toilets", prop.get("bathrooms", 2))),
        "description": prop.get("description", ""),
        "uuid": "",
        "primaryImage": "0",
        "deleteImages": "",
    }

    for feature in prop.get("features", []):
        code = FEATURE_CODES.get(feature)
        if code:
            data[code] = "on"

    r = session.post(f"{BASE_URL}/property-post", data=data, allow_redirects=True)

    match = re.search(r"/property-edit/(\d+)", r.url)
    if not match:
        match = re.search(r"/property-edit/(\d+)", r.text)
    if not match:
        raise Exception(f"PropertyPro: Could not get property ID. URL: {r.url}")

    property_id = match.group(1)
    edit_url = f"{BASE_URL}/property-edit/{property_id}?isnewposting=new-posting"

    # Get UUID from edit page — try multiple attribute orderings
    r2 = session.get(edit_url)
    uuid_match = (
        re.search(r'id="uuid"[^>]+value="([^"]+)"', r2.text) or
        re.search(r'name="uuid"[^>]+value="([^"]+)"', r2.text) or
        re.search(r'value="([^"]+)"[^>]+id="uuid"', r2.text) or
        re.search(r'value="([^"]+)"[^>]+name="uuid"', r2.text) or
        re.search(r'["\']uuid["\']\s*:\s*["\']([^"\']+)["\']', r2.text)
    )
    uuid = uuid_match.group(1) if uuid_match else ""
    if not uuid:
        print(f"[PROPERTYPRO] WARNING: UUID not found for {property_id} — images will be skipped")

    return property_id, edit_url, uuid


def upload_images(session, property_id: str, edit_url: str, uuid: str,
                  image_paths: list, title: str) -> int:
    """Upload images one at a time. Returns count uploaded."""
    uploaded = 0
    for i, img_path in enumerate(image_paths):
        name = os.path.basename(img_path)
        for attempt in range(2):
            try:
                with open(img_path, "rb") as f:
                    resp = session.post(
                        f"{BASE_URL}/upload-picture-new",
                        data={
                            "indexes": str(i),
                            "file_types": "new",
                            "file_names": name,
                            "index": str(i),
                            "title": title,
                            "uuid": uuid,
                            "primaryIndex": "0",
                            "deletedImages": ""
                        },
                        files=[("files", (name, f, "image/jpeg"))],
                        headers={
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": edit_url
                        },
                        timeout=20
                    )
                if resp.status_code == 200:
                    uploaded += 1
                    break
            except Exception as e:
                if attempt < 1:
                    time.sleep(1)
        time.sleep(0.5)

    # Save images
    save_resp = session.post(
        f"{BASE_URL}/property-pictures/{property_id}",
        data={"uuid": uuid, "primaryImage": "0", "deleteImages": ""},
        headers={"Referer": edit_url},
        allow_redirects=True
    )

    return uploaded


def _get_session(credentials: dict, force_new: bool = False) -> requests.Session:
    """Return a shared logged-in session (login once, reuse for all properties)."""
    global _shared_session, _shared_credentials
    if force_new or _shared_session is None or _shared_credentials != credentials.get("email"):
        _shared_session = make_session(credentials.get("proxy_url", ""))
        login(_shared_session, credentials["email"], credentials["password"])
        _shared_credentials = credentials.get("email")
    return _shared_session


def get_public_url(session, property_id: str) -> str:
    """Try to extract the public listing URL from the property edit page."""
    try:
        r = session.get(f"{BASE_URL}/property-edit/{property_id}", timeout=15)
        m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', r.text)
        if m: return m.group(1)
        m = re.search(r'href=["\'](' + re.escape(BASE_URL) + r'/(?:listing|property|real-estate)/[^"\']+)["\']', r.text)
        if m: return m.group(1)
    except Exception:
        pass
    return ""


def post_property(prop: dict, image_paths: list, credentials: dict) -> dict:
    """Full flow: login → create → upload images → return result.
    Listing creation is serialized (PropertyPro rejects concurrent posts from same account).
    Image uploads run in parallel after listing is created.
    """
    result = {"ref": prop.get("ref", ""), "platform": "propertypro", "success": False}

    for attempt in range(2):
        try:
            # Serialize ALL listing creation — concurrent sessions for same account cause 400 errors
            with _post_lock:
                force_new = attempt > 0  # force fresh login on retry
                session = _get_session(credentials, force_new=force_new)
                property_id, edit_url, uuid = create_listing(session, prop)
                time.sleep(0.5)  # brief pause between sequential listings

            result["property_id"] = property_id
            result["edit_url"] = edit_url

            # Image uploads can run after listing creation
            if image_paths and uuid:
                count = upload_images(session, property_id, edit_url, uuid, image_paths, prop["title"])
                result["images_uploaded"] = count

            result["success"] = True
            result["listing_url"] = f"{BASE_URL}/property-edit/{property_id}"
            print(f"[PROPERTYPRO] ✓ {prop.get('ref')} → ID {property_id} | {result.get('images_uploaded', 0)} images | {result['listing_url']}")
            break  # success — exit retry loop

        except requests.exceptions.ConnectionError as e:
            if attempt == 0:
                print(f"[PROPERTYPRO] Connection dropped, retrying with fresh session…")
                time.sleep(2)
                continue
            result["error"] = f"Connection error: {e}"
            print(f"[PROPERTYPRO] ✗ {prop.get('ref')}: {result['error']}")
        except Exception as e:
            result["error"] = str(e)
            print(f"[PROPERTYPRO] ✗ {prop.get('ref')}: {e}")
            break

    return result
