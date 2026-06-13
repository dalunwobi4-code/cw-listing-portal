#!/usr/bin/env python3
"""
CW Lagos Property Listing Orchestrator
Posts properties to PP Nigeria, PropertyPro, NPC, and Airtable in parallel.

Target: 10 properties across 4 platforms in <30 minutes

Usage:
    python3 orchestrate.py properties.json
    python3 orchestrate.py properties.json --platforms ppng,npc,airtable
    python3 orchestrate.py properties.json --dry-run
"""

import json
import os
import sys
import time
import argparse
import concurrent.futures
from datetime import datetime
from pathlib import Path

# Add agents dir to path
sys.path.insert(0, str(Path(__file__).parent / "agents"))

from watermark import watermark_batch
from poster_ppng import post_property as post_ppng
from poster_propertypro import post_property as post_propertypro
from poster_npc import post_property as post_npc
from poster_airtable import post_property as post_airtable

# ─── Credentials ────────────────────────────────────────────────────────────

CREDENTIALS = {
    "ppng": {
        "email": "listings@cwlagos.com",
        "password": "Listings@1234",
        "proxy_url": "http://b2ff1e853ef6c57a:bg2WA1iQu9y4fI3F@res.proxy-seller.com:10000"
    },
    "propertypro": {
        "email": "listings@cwlagos.com",
        "password": "Listings@1234",
        "proxy_url": "http://b2ff1e853ef6c57a:bg2WA1iQu9y4fI3F@res.proxy-seller.com:10000"
    },
    "npc": {
        "email": "daniel@cwlagos.com",
        "password": "Listings@1234",
        "proxy_url": "http://b2ff1e853ef6c57a:bg2WA1iQu9y4fI3F@res.proxy-seller.com:10000"
    },
    "airtable": {
        "token": "patYRSlAoegACNY0G.a570b143f611e31e0203e31af4524821e706d3e8bb59b181bc14871c35c6f8c5"
    },
    "anthropic": {
        "api_key": "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"
    }
}

WATERMARK_LOGO = "/Users/apple/Downloads/CW Logo White.png"
RAW_IMAGES_DIR = "/Users/apple/cw_listing_agent/raw_images"
WATERMARKED_DIR = "/Users/apple/cw_listing_agent/watermarked"
LOGS_DIR = "/Users/apple/cw_listing_agent/logs"

# ─── Platform poster map ─────────────────────────────────────────────────────

PLATFORM_POSTERS = {
    "ppng":        post_ppng,
    "propertypro": post_propertypro,
    "npc":         post_npc,
    "airtable":    post_airtable,
}

# ─── Core functions ──────────────────────────────────────────────────────────

def load_properties(path: str) -> list:
    """Load property data from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    # Accept either a list or {"properties": [...]}
    if isinstance(data, list):
        return data
    return data.get("properties", [])


def watermark_all(properties: list) -> dict:
    """
    Watermark all property images (parallel, up to 5 at once).
    Returns {ref: [watermarked_img_paths]}
    """
    print("\n[STEP 1] Watermarking images...")
    t0 = time.time()
    results = watermark_batch(
        properties,
        base_source_dir=RAW_IMAGES_DIR,
        base_dest_dir=WATERMARKED_DIR,
        logo_path=WATERMARK_LOGO if os.path.exists(WATERMARK_LOGO) else None
    )
    elapsed = time.time() - t0
    total_imgs = sum(len(v) for v in results.values())
    print(f"[WATERMARK] Done: {total_imgs} images in {elapsed:.1f}s")
    return results


def post_property_to_platform(platform: str, prop: dict, image_paths: list) -> dict:
    """Post a single property to a single platform."""
    poster = PLATFORM_POSTERS[platform]
    creds = CREDENTIALS[platform]
    try:
        result = poster(prop, image_paths, creds)
    except Exception as e:
        result = {
            "ref": prop.get("ref", ""),
            "platform": platform,
            "success": False,
            "error": str(e)
        }
    return result


def post_all_platforms_for_property(prop: dict, image_paths: list, platforms: list) -> dict:
    """Post one property to all platforms in parallel. Returns all results."""
    ref = prop.get("ref", "?")
    print(f"\n[POSTING] {ref}: {prop.get('title', '')} → {', '.join(platforms)}")

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        futures = {
            executor.submit(post_property_to_platform, platform, prop, image_paths): platform
            for platform in platforms
        }
        for future in concurrent.futures.as_completed(futures):
            platform = futures[future]
            result = future.result()
            results[platform] = result

    return results


def load_prewartermarked(properties: list) -> dict:
    """Load already-watermarked images from the watermarked/ folder (skip re-processing)."""
    results = {}
    for prop in properties:
        ref = prop["ref"]
        folder = Path(WATERMARKED_DIR) / ref
        if folder.exists():
            imgs = sorted([str(f) for f in folder.iterdir() if f.suffix.lower() in ('.jpg', '.jpeg', '.png')])
            results[ref] = imgs
            print(f"[WATERMARK] {ref}: {len(imgs)} pre-watermarked images loaded")
        else:
            results[ref] = []
            print(f"[WATERMARK] {ref}: folder not found at {folder}")
    return results


def run_batch(properties: list, platforms: list, max_parallel_properties: int = 3,
              dry_run: bool = False, skip_watermark: bool = False) -> dict:
    """
    Post all properties to all platforms.
    Parallelism:
    - Up to max_parallel_properties properties at a time
    - Each property → all platforms in parallel simultaneously
    """
    t_start = time.time()
    all_results = {}

    # Step 1: Watermark all images (or load pre-watermarked)
    if dry_run:
        watermarked = {p["ref"]: [] for p in properties}
    elif skip_watermark:
        print("\n[STEP 1] Loading pre-watermarked images (skipping watermark step)...")
        watermarked = load_prewartermarked(properties)
    else:
        watermarked = watermark_all(properties)

    # Step 2: Post to platforms in parallel batches
    print(f"\n[STEP 2] Posting {len(properties)} properties to {len(platforms)} platforms...")
    print(f"  Parallel batch size: {max_parallel_properties} properties at a time")
    print(f"  Platforms: {', '.join(platforms)}")

    if dry_run:
        print("\n[DRY RUN] Would post the following:")
        for prop in properties:
            imgs = watermarked.get(prop["ref"], [])
            print(f"  {prop['ref']}: {prop['title']} | {len(imgs)} images → {', '.join(platforms)}")
        return {}

    def process_one_property(prop):
        ref = prop["ref"]
        imgs = watermarked.get(ref, [])
        results = post_all_platforms_for_property(prop, imgs, platforms)
        return ref, results

    # Process in batches for rate limiting
    for i in range(0, len(properties), max_parallel_properties):
        batch = properties[i:i + max_parallel_properties]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel_properties) as executor:
            futures = {executor.submit(process_one_property, prop): prop["ref"] for prop in batch}
            for future in concurrent.futures.as_completed(futures):
                ref, results = future.result()
                all_results[ref] = results

        # Small delay between batches
        if i + max_parallel_properties < len(properties):
            time.sleep(5)

    # Step 3: Summary
    elapsed = time.time() - t_start
    success_count = sum(
        1 for results in all_results.values()
        for result in results.values()
        if result.get("success")
    )
    total_count = len(properties) * len(platforms)

    print(f"\n{'='*60}")
    print(f"DONE in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Success: {success_count}/{total_count} platform posts")
    print()

    for ref, results in all_results.items():
        print(f"  {ref}:")
        for platform, r in results.items():
            status = "✓" if r.get("success") else "✗"
            url = r.get("listing_url", r.get("images_url", ""))
            err = f" | ERROR: {r.get('error', '')}" if not r.get("success") else ""
            imgs = f" | {r.get('images_uploaded', '?')} imgs" if r.get("success") else ""
            print(f"    [{platform}] {status}{imgs}{err}")
            if url:
                print(f"       {url}")

    # Save log
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = f"{LOGS_DIR}/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_path, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": elapsed,
            "properties": len(properties),
            "platforms": platforms,
            "results": all_results
        }, f, indent=2)
    print(f"\nLog saved: {log_path}")

    return all_results


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CW Lagos Property Listing Orchestrator")
    parser.add_argument("properties_file", help="JSON file with property data")
    parser.add_argument("--platforms", default="ppng,propertypro,npc,airtable",
                        help="Comma-separated list of platforms (default: all)")
    parser.add_argument("--parallel", type=int, default=3,
                        help="Max properties to post in parallel (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be posted without actually posting")
    parser.add_argument("--skip-watermark", action="store_true",
                        help="Skip watermarking step (use pre-watermarked images from watermarked/ folder)")
    args = parser.parse_args()

    properties = load_properties(args.properties_file)
    platforms = [p.strip() for p in args.platforms.split(",")]

    print(f"CW Lagos Property Listing Orchestrator")
    print(f"Properties: {len(properties)}")
    print(f"Platforms: {', '.join(platforms)}")
    print(f"Parallel: {args.parallel} properties at a time")

    run_batch(properties, platforms, max_parallel_properties=args.parallel,
              dry_run=args.dry_run, skip_watermark=args.skip_watermark)


if __name__ == "__main__":
    main()
