"""
CW Lagos Property Listing Agent - Shared Config
"""

CREDENTIALS = {
    "privateproperty_ng": {
        "email": "",
        "password": "",
        "base_url": "https://privateproperty.ng"
    },
    "propertypro": {
        "email": "",
        "password": "",
        "base_url": "https://propertypro.ng"
    },
    "npc": {
        "email": "",
        "password": "",
        "base_url": "https://nigeriapropertycenter.com"
    },
    "airtable": {
        "token": "",
        "base_id": "appui0GIVsqalToEa",
        "table_name": "Lagos Properties"
    }
}

# PP Nigeria location codes
PPNG_AREAS = {
    "Lekki Phase 1": "33",
    "Chevron Drive": "114",
    "Lekki": "6",   # axis
    "Lagos": "5",   # state
}

PPNG_TYPES = {
    "Flat": {"type": "214", "stype": ""},
    "House": {"type": "3", "stype": ""},
    "Semi-detached Duplex": {"type": "3", "stype": "16981000"},
    "Terraced Duplex": {"type": "3", "stype": "16981001"},
    "Detached Duplex": {"type": "3", "stype": "16981003"},
}

PPNG_MODES = {"sale": "sale", "rent": "rent", "short_let": "short-let"}

# PropertyPro location codes
PP_AREAS = {
    "Lekki Phase 1": "350",
    "Chevron Drive": "282",
    "Lekki": "12",   # axis
    "Lagos": "1",    # state
}

PP_TYPES = {
    "Flat": {"type": "6", "stype": ""},
    "House": {"type": "13", "stype": ""},
    "Semi-detached Duplex": {"type": "13", "stype": "7"},
}

PP_MODES = {"sale": "sale", "rent": "rent"}

WATERMARK_LOGO = "/Users/apple/Downloads/CW Logo White.png"
