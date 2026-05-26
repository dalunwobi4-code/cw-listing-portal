# CW Lagos Property Listing Agent

Posts properties to PP Nigeria, PropertyPro, NPC, and Airtable in parallel.

## Architecture

```
orchestrate.py
├── Step 1: watermark.py → applies CW logo to all images (parallel, 5 at a time)
└── Step 2: post in parallel
    ├── poster_ppng.py       → privateproperty.ng
    ├── poster_propertypro.py → propertypro.ng
    ├── poster_npc.py        → nigeriapropertycentre.com
    └── poster_airtable.py   → Airtable Lagos Properties table
```

## Directory Structure

```
cw_listing_agent/
├── orchestrate.py          # Main orchestrator
├── agents/
│   ├── config.py           # Shared config & location codes
│   ├── watermark.py        # Image watermarking
│   ├── poster_ppng.py      # PP Nigeria poster
│   ├── poster_propertypro.py # PropertyPro poster
│   ├── poster_npc.py       # NPC poster
│   └── poster_airtable.py  # Airtable poster
├── raw_images/             # Original property images (CW08494/, CW08495/, ...)
├── watermarked/            # Auto-generated watermarked images
├── data/
│   └── properties_template.json  # Sample property data format
└── logs/                   # Run logs (JSON)
```

## Quick Start

### 1. Prepare images
```bash
mkdir -p ~/cw_listing_agent/raw_images/CW08494
# Copy raw images into CW08494/ folder (any JPG/PNG/WebP)
```

### 2. Create properties JSON
Edit `data/properties_template.json` with your property details.

### 3. Run

**All platforms:**
```bash
cd ~/cw_listing_agent
python3 orchestrate.py data/properties_template.json
```

**Specific platforms:**
```bash
python3 orchestrate.py data/properties_template.json --platforms ppng,npc
```

**Dry run (preview only):**
```bash
python3 orchestrate.py data/properties_template.json --dry-run
```

**Control parallelism:**
```bash
python3 orchestrate.py data/properties_template.json --parallel 5
```

## Performance

| Properties | Platforms | Expected Time |
|-----------|-----------|---------------|
| 10        | 4         | ~20-25 min    |
| 10        | 2         | ~12-15 min    |
| 5         | 4         | ~12-15 min    |

The bottleneck is image uploads (~1-2s per image per platform). With 10 properties × 8 images × 4 platforms = 320 uploads. At 3 properties in parallel × 4 platforms in parallel = 12 concurrent uploads at peak.

## Platform Notes

### PP Nigeria (✅ Fully Working)
- Flow: login → create → AJAX upload (`/upload-picture`) → save in same session
- Key: images must be uploaded and saved in the SAME session (UUID coupling)
- SSL: uses `verify=False` + retry adapter for BAD_RECORD_MAC errors

### PropertyPro (⚠️ Partial - 1 image per listing)
- Flow: login → POST /property-post → redirect to /property-edit/{id}
- Image upload: `/upload-picture-new` (AJAX) returns count but only 1 image saves
- **TODO**: investigate `/property-pictures/{id}` save form field requirements

### NPC (✅ Fully Working)
- Flow: login → POST /account/listings (with agent_id!) → images page
- Image upload: POST `/ajax/listings/{id}/images` with files[]
- Finalize: POST `/account/listings/{id}/images/task` with task=saveOrder
- Key: Must include `agent_id=86702` and `agent=cw real estate -lekki` in create POST

### Airtable (✅ Working - needs record matching)
- Upload: POST to content.airtable.com per-field attachment endpoint
- Find record: searches by ref (CW08494) across common field names

## Properties JSON Format

```json
[
  {
    "ref": "CW08494",
    "title": "2 Bedroom Apartment For Sale in Lekki Phase 1",
    "mode": "sale",
    "property_type": "Flat",
    "location": "Lekki Phase 1",
    "bedrooms": 2,
    "bathrooms": 2,
    "toilets": 2,
    "price": 180000000,
    "street": "",
    "description": "Full description here...",
    "features": ["All Room Ensuite", "Swimming Pool", "24 Hours Security"]
  }
]
```

### Available Locations
- Lekki Phase 1
- Victoria Island
- Ikoyi / Old Ikoyi
- Banana Island
- Chevron Drive
- Lekki

### Available Modes
- `sale`
- `rent`
- `short_let`

### Available Property Types
- Flat / Apartment
- House
- Semi-detached Duplex
- Terraced Duplex
- Detached Duplex

### Available Features
- All Room Ensuite
- Swimming Pool
- 24 Hours Security
- Gym
- Generator
- Boys Quarter
- Elevator
- CCTV
