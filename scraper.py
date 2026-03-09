"""
Badminton Availability Checker
- Whitechapel & Mile End: GladstoneGo API (fetches anonymous JWT first)
- Walthamstow: Better GLL
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = Path("data/availability.json")
OUTPUT_FILE.parent.mkdir(exist_ok=True)

GLADSTONE_BASE = "https://towerhamletscouncil.gladstonego.cloud"
GLADSTONE_SESSIONS = f"{GLADSTONE_BASE}/api/availability/V2/sessions"

VENUES = [
    {
        "id": "whitechapel",
        "name": "Whitechapel Sports Centre",
        "system": "gladstone",
        "address": "Durward Street, Whitechapel, E1 5BA",
        "info_url": "https://be-well.org.uk/centres/whitechapel-sports-centre/",
        "booking_url": f"{GLADSTONE_BASE}/book",
        "activities": [
            {"id": "WACT000010", "label": "40 min", "site": "WSC"},
            {"id": "WACT000011", "label": "60 min", "site": "WSC"},
        ],
    },
    {
        "id": "mile_end",
        "name": "Mile End Leisure Centre",
        "system": "gladstone",
        "address": "190 Mile End Road, Tower Hamlets, E1 4AJ",
        "info_url": "https://be-well.org.uk/centres/mile-end-leisure-centre-and-stadium/",
        "booking_url": f"{GLADSTONE_BASE}/book",
        "activities": [
            {"id": "MACT000009,MACT000010", "label": "40 min", "site": "MEPLS"},
            {"id": "MACT000011", "label": "60 min", "site": "MEPLS"},
        ],
    },
    {
        "id": "walthamstow",
        "name": "Walthamstow Leisure Centre",
        "system": "better",
        "address": "Markhouse Road, Walthamstow, E17 8BD",
        "info_url": "https://www.better.org.uk/leisure-centre/london/waltham-forest/walthamstow-leisure-centre",
        "booking_url": "https://bookings.better.org.uk/location/walthamstow-leisure-centre/",
        "activities": [
            {"slug": "badminton-40-mins", "label": "40 min"},
            {"slug": "badminton-60-mins", "label": "60 min"},
        ],
    },
]


# ---------------------------------------------------------------------------
# GladstoneGo — get anonymous JWT by visiting the site first
# ---------------------------------------------------------------------------

def get_gladstone_session() -> requests.Session:
    """
    Create a requests Session that has the anonymous JWT cookie,
    exactly as a real browser would get it by visiting /book.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": f"{GLADSTONE_BASE}/book",
        "X-Use-Sso": "1",
    })

    # Try the anonymous login endpoint first
    try:
        resp = session.post(
            f"{GLADSTONE_BASE}/api/login/anonymous",
            json={},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token") or data.get("token") or data.get("jwt")
            if token:
                session.cookies.set("Jwt", token, domain="towerhamletscouncil.gladstonego.cloud")
                print("  [AUTH] Got anonymous JWT via /api/login/anonymous")
                return session
    except Exception as exc:
        print(f"  [WARN] Anonymous login endpoint failed: {exc}")

    # Fallback: visit the booking page and let cookies set naturally
    try:
        resp = session.get(f"{GLADSTONE_BASE}/book", timeout=15)
        print(f"  [AUTH] Visited /book, cookies: {list(session.cookies.keys())}")
    except Exception as exc:
        print(f"  [WARN] Could not visit /book: {exc}")

    # Last resort: try fetching JWT from the config/init endpoint
    for endpoint in [
        "/api/config",
        "/api/site/config",
        "/mwsapi/site/en-GB/centre/getSiteList",
    ]:
        try:
            resp = session.get(f"{GLADSTONE_BASE}{endpoint}", timeout=10)
            if resp.status_code == 200:
                print(f"  [AUTH] Hit {endpoint} successfully")
                break
        except Exception:
            pass

    return session


def scrape_gladstone(venue: dict, days_ahead: int = 7) -> list[dict]:
    slots = []
    date_from = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    session = get_gladstone_session()

    for activity in venue["activities"]:
        params = {
            "webBookableOnly": "true",
            "siteIds": activity["site"],
            "activityIds": activity["id"],
            "dateFrom": date_from,
        }
        try:
            resp = session.get(GLADSTONE_SESSIONS, params=params, timeout=15)
            print(f"  [API] {activity['label']} status={resp.status_code} size={len(resp.content)}b")

            if resp.status_code != 200:
                continue

            data = resp.json()
            sessions = data if isinstance(data, list) else data.get("data", data.get("sessions", []))
            print(f"  [API] {activity['label']} returned {len(sessions)} session(s)")

            for s in sessions:
                start_raw = (
                    s.get("startTime")
                    or s.get("startDateTime")
                    or s.get("start", "")
                )
                if not start_raw:
                    continue

                try:
                    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    if start_dt.date() > (datetime.now().date() + timedelta(days=days_ahead)):
                        continue
                    date_str = start_dt.strftime("%Y-%m-%d")
                    time_str = start_dt.strftime("%H:%M")
                except Exception:
                    date_str = "See site"
                    time_str = start_raw[:16]

                spaces = s.get("spaces", s.get("availableSpaces", s.get("spacesAvailable")))
                if spaces is not None:
                    try:
                        if int(spaces) == 0:
                            continue
                        spaces_str = f"{spaces} space{'s' if int(spaces) != 1 else ''}"
                    except (ValueError, TypeError):
                        spaces_str = str(spaces)
                else:
                    spaces_str = "Available"

                slots.append({
                    "date": date_str,
                    "duration": activity["label"],
                    "time": time_str,
                    "spaces": spaces_str,
                    "book_url": venue["booking_url"],
                })

            time.sleep(0.5)

        except Exception as exc:
            print(f"  [ERROR] {activity['label']}: {exc}")

    slots.sort(key=lambda s: (s["date"], s["time"]))
    return slots


# ---------------------------------------------------------------------------
# Better (GLL) — Walthamstow
# ---------------------------------------------------------------------------

def scrape_better(venue: dict, days_ahead: int = 7) -> list[dict]:
    slots = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    base = venue["booking_url"].rstrip("/")

    for activity in venue["activities"]:
        slug = activity["slug"]
        for day_offset in range(days_ahead):
            date = (datetime.now() + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            url = f"{base}/{slug}/{date}/by-time"
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                slot_elements = (
                    soup.select("[class*='slot']:not([class*='slot-picker'])")
                    or soup.select(".activity-card, .session-card, li.bookable")
                )
                for el in slot_elements:
                    booked = any(
                        c in " ".join(el.get("class", [])).lower()
                        for c in ["full", "booked", "unavailable", "sold-out"]
                    )
                    if booked:
                        continue
                    time_el = el.select_one("time, .time, [class*='time']")
                    avail_el = el.select_one("[class*='available'], [class*='space'], [class*='remaining']")
                    slots.append({
                        "date": date,
                        "duration": activity["label"],
                        "time": time_el.get_text(strip=True) if time_el else "See site",
                        "spaces": avail_el.get_text(strip=True) if avail_el else "Available",
                        "book_url": url,
                    })
                time.sleep(0.8)
            except Exception as exc:
                print(f"  [WARN] Better {date} ({activity['label']}): {exc}")

    slots.sort(key=lambda s: (s["date"], s["time"]))
    return slots


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def check_all_venues() -> dict:
    results = {"last_updated": datetime.now().isoformat(), "venues": []}
    for venue in VENUES:
        print(f"\nChecking {venue['name']}...")
        try:
            if venue["system"] == "gladstone":
                slots = scrape_gladstone(venue)
                note = "Live data from GladstoneGo API"
            else:
                slots = scrape_better(venue)
                note = "Live data from Better GLL"
        except Exception as exc:
            slots = []
            note = f"Error: {exc}"
        print(f"  -> {len(slots)} slot(s) found")
        results["venues"].append({
            "id": venue["id"],
            "name": venue["name"],
            "address": venue["address"],
            "system": venue["system"],
            "info_url": venue["info_url"],
            "booking_url": venue["booking_url"],
            "note": note,
            "slots_found": len(slots),
            "slots": slots,
        })
    return results


def main():
    print(f"\nBadminton Check - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    data = check_all_venues()
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nDone. Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
