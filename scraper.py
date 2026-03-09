"""
Badminton Availability Checker
Checks: Whitechapel Sports Centre & Mile End (Be Well) + Walthamstow (Better/GLL)
Saves results to data/availability.json for the dashboard to read.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = Path("data/availability.json")
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

VENUES = [
    {
        "id": "whitechapel",
        "name": "Whitechapel Sports Centre",
        "system": "bewell",
        "url": "https://be-well.org.uk/centres/whitechapel-sports-centre/",
        "booking_url": "https://be-well.org.uk/book/",
        "address": "Durward Street, Whitechapel, E1 5BA",
    },
    {
        "id": "mile_end",
        "name": "Mile End Leisure Centre",
        "system": "bewell",
        "url": "https://be-well.org.uk/centres/mile-end-leisure-centre-and-stadium/",
        "booking_url": "https://be-well.org.uk/book/",
        "address": "190 Mile End Road, Tower Hamlets, E1 4AJ",
    },
    {
        "id": "walthamstow",
        "name": "Walthamstow Leisure Centre",
        "system": "better",
        "url": "https://www.better.org.uk/leisure-centre/london/waltham-forest/walthamstow-leisure-centre/badminton",
        "booking_url": "https://bookings.better.org.uk/location/walthamstow-leisure-centre/badminton-40-mins/",
        "address": "Markhouse Road, Walthamstow, E17 8BD",
    },
]


# ---------------------------------------------------------------------------
# Better (GLL / Walthamstow) scraper
# ---------------------------------------------------------------------------

def scrape_better(venue: dict, days_ahead: int = 7) -> list[dict]:
    """
    Scrape the Better booking page for available badminton slots.
    Better uses a calendar-style page at:
      https://bookings.better.org.uk/location/<slug>/<activity>/<YYYY-MM-DD>/by-time
    """
    slots = []
    base = venue["booking_url"].rstrip("/")

    for day_offset in range(days_ahead):
        date = (datetime.now() + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        url = f"{base}/{date}/by-time"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # Better renders slots as <li> or <div> elements with class containing 'slot'
            # They may also appear as data in a <script> tag — we try both
            slot_elements = soup.select("[class*='slot']:not([class*='slot-picker'])")
            if not slot_elements:
                slot_elements = soup.select(".activity-card, .session-card, li.bookable")

            for el in slot_elements:
                time_el = el.select_one("time, .time, [class*='time']")
                avail_el = el.select_one("[class*='available'], [class*='space'], [class*='remaining']")
                booked_class = any(
                    c in el.get("class", []) for c in ["full", "booked", "unavailable", "sold-out"]
                )
                if booked_class:
                    continue

                time_str = time_el.get_text(strip=True) if time_el else "See site"
                avail_str = avail_el.get_text(strip=True) if avail_el else "Available"

                slots.append({
                    "date": date,
                    "time": time_str,
                    "spaces": avail_str,
                    "book_url": url,
                })

            # Rate-limit courtesy
            time.sleep(1)

        except requests.RequestException as exc:
            print(f"  [WARN] Better request failed for {date}: {exc}")

    return slots


# ---------------------------------------------------------------------------
# Be Well (Tower Hamlets – Whitechapel & Mile End) scraper
# ---------------------------------------------------------------------------

def scrape_bewell(venue: dict, days_ahead: int = 7) -> list[dict]:
    """
    Be Well uses a booking system embedded via an iframe or redirect.
    We fetch their timetable/book page and look for badminton sessions.
    """
    slots = []

    # Be Well embeds a third-party booking widget (often Gladstone/Legend).
    # We try their /book/ page filtered to badminton.
    booking_urls = [
        f"https://be-well.org.uk/book/?location={venue['id'].replace('_', '-')}&activity=badminton",
        "https://be-well.org.uk/book/",
    ]

    for url in booking_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Look for an iframe src — the real booking calendar is often inside
            iframe = soup.find("iframe")
            if iframe and iframe.get("src"):
                iframe_url = iframe["src"]
                if not iframe_url.startswith("http"):
                    iframe_url = "https://be-well.org.uk" + iframe_url
                resp2 = requests.get(iframe_url, headers=HEADERS, timeout=15)
                soup = BeautifulSoup(resp2.text, "html.parser")

            # Parse any session rows that mention badminton
            rows = soup.find_all(
                lambda tag: tag.name in ["tr", "li", "div"]
                and "badminton" in tag.get_text(strip=True).lower()
            )

            for row in rows:
                text = row.get_text(" ", strip=True)
                # Skip if it says fully booked / no spaces
                if any(w in text.lower() for w in ["full", "no spaces", "sold out"]):
                    continue
                slots.append({
                    "date": "See site",
                    "time": text[:120],
                    "spaces": "Check site",
                    "book_url": url,
                })
            break  # got something, stop trying fallback URLs
        except requests.RequestException as exc:
            print(f"  [WARN] Be Well request failed: {exc}")

    # If we found nothing from HTML scraping, return an info slot directing user to site
    if not slots:
        slots.append({
            "date": "Live",
            "time": "Check the Be Well website for live badminton slots",
            "spaces": "Unknown — site uses dynamic booking",
            "book_url": venue["booking_url"],
        })

    return slots


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def check_all_venues() -> dict:
    results = {
        "last_updated": datetime.now().isoformat(),
        "venues": [],
    }

    for venue in VENUES:
        print(f"Checking {venue['name']}...")
        try:
            if venue["system"] == "better":
                slots = scrape_better(venue)
            else:
                slots = scrape_bewell(venue)
        except Exception as exc:
            print(f"  [ERROR] {exc}")
            slots = []

        results["venues"].append({
            "id": venue["id"],
            "name": venue["name"],
            "address": venue["address"],
            "booking_url": venue["booking_url"],
            "system": venue["system"],
            "slots_found": len(slots),
            "slots": slots,
            "status": "ok" if slots else "no_slots",
        })
        print(f"  → {len(slots)} slot(s) found")

    return results


def main():
    print(f"\n🏸 Badminton Availability Check — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    data = check_all_venues()
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\n✅ Results saved to {OUTPUT_FILE}")
    return data


if __name__ == "__main__":
    main()
