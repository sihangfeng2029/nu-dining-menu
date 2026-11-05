"""
Fetch Northwestern dining-hall menus from the DineOnCampus public API (apiv4).

Endpoints were discovered by inspecting the network traffic of
https://dineoncampus.com/northwestern/whats-on-the-menu/... (Nov 2026):

  GET /sites/{SITE_ID}/locations-public?for_menus=true&locale=en
      -> {"buildings": [{"buildingName": ..., "locations": [{"id","name","slug",...}]}], ...}
  GET /locations/{LOCATION_ID}/periods/?date=YYYY-MM-DD
      -> {"periods": [{"id","name","slug"}...], "menu": {...default period menu...}}
  GET /locations/{LOCATION_ID}/menu?date=YYYY-MM-DD&period={PERIOD_ID}
      -> {"period": {"name", "categories": [{"name", "items": [{"name","desc","ingredients",
                                                               "calories","portion","filters":[{"name"}]}]}]}}

Period ids are generated per location AND per date, so we must call /periods for
every (location, date) pair before calling /menu.

The output of this module is a plain-English, normalized structure; translation is
done in a separate step (translate.py) so that scraping and translation can fail and
be retried independently.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

API_BASE = "https://apiv4.dineoncampus.com"
SITE_ID = "5acea5d8f3eeb60b08c5a50d"  # Northwestern University

# The five residential dining commons on the Evanston campus. We resolve them by
# slug at runtime (ids are looked up from the API) but keep this list so the page
# order is stable and so a new/renamed location does not silently replace one.
DINING_HALL_SLUGS = [
    "allison-dining-commons",
    "sargent-dining-commons",
    "foster-walker-plex-east",
    "foster-walker-plex-west",
    "elder-dining-commons",
]

# Only these meal periods are kept (the user asked for lunch and dinner).
WANTED_PERIODS = ["lunch", "dinner"]

# Browser-like headers. The API is the same one the public website calls from the
# browser, so presenting the same Origin/Referer keeps us indistinguishable from a
# normal page view and avoids being treated as an unknown client.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://dineoncampus.com",
    "Referer": "https://dineoncampus.com/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _get_json(path: str, params: dict[str, Any] | None = None, retries: int = 4) -> Any:
    """GET a JSON endpoint with exponential back-off.

    The menu API occasionally answers slowly or with a transient 5xx while menus are
    being generated, so a small retry loop makes the daily job far more reliable.
    """
    url = API_BASE + path
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=40)
            if resp.status_code == 200:
                return resp.json()
            last_err = RuntimeError(f"HTTP {resp.status_code} for {resp.url}")
        except requests.RequestException as exc:  # network error, timeout, ...
            last_err = exc
        time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def get_dining_halls() -> list[dict[str, str]]:
    """Return [{"id", "name", "slug"}] for the five dining commons, in fixed order."""
    data = _get_json(f"/sites/{SITE_ID}/locations-public", {"for_menus": "true", "locale": "en"})
    by_slug: dict[str, dict[str, str]] = {}
    for building in data.get("buildings", []):
        for loc in building.get("locations", []):
            by_slug[loc.get("slug", "")] = {
                "id": loc["id"],
                "name": loc.get("displayName") or loc.get("name"),
                "slug": loc.get("slug", ""),
            }
    for loc in data.get("standaloneLocations", []) or []:
        by_slug.setdefault(loc.get("slug", ""), {
            "id": loc["id"], "name": loc.get("displayName") or loc.get("name"), "slug": loc.get("slug", ""),
        })
    halls = [by_slug[s] for s in DINING_HALL_SLUGS if s in by_slug]
    if not halls:
        raise RuntimeError("No dining halls found - the API layout may have changed.")
    return halls


def _clean_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the website shows; drop the bulky nutrient table except calories."""
    return {
        "name": (raw.get("name") or "").strip(),
        "desc": (raw.get("desc") or "").strip(),
        "ingredients": (raw.get("ingredients") or "").strip(),
        "calories": raw.get("calories"),
        "portion": raw.get("portion") or "",
        # "filters" mixes allergens (Milk, Gluten...) and diet labels (Vegan, Vegetarian...).
        # The trailing "*" on some names (e.g. "Celery*") is a footnote marker on the
        # source site; strip it so the glossary lookup matches.
        "tags": [f.get("name", "").strip().rstrip("*").strip() for f in raw.get("filters", []) if f.get("name")],
    }


def get_menu_for_day(hall: dict[str, str], date: str) -> dict[str, Any]:
    """Return {"lunch": [stations...], "dinner": [stations...]} for one hall on one date.

    A missing period (hall closed, weekend brunch instead of lunch, menu not yet
    published) simply yields no key rather than an error.
    """
    result: dict[str, Any] = {}
    periods_resp = _get_json(f"/locations/{hall['id']}/periods/", {"date": date})
    periods = periods_resp.get("periods", []) or []

    for wanted in WANTED_PERIODS:
        # Match by slug first, then by name; weekends often call lunch "Brunch".
        period = next((p for p in periods if (p.get("slug") or "").lower() == wanted), None)
        if period is None:
            period = next((p for p in periods if (p.get("name") or "").lower() == wanted), None)
        if period is None and wanted == "lunch":
            period = next((p for p in periods if "brunch" in (p.get("name") or "").lower()), None)
        if period is None:
            continue

        menu = _get_json(f"/locations/{hall['id']}/menu", {"date": date, "period": period["id"]})
        categories = (menu.get("period") or {}).get("categories", []) or []
        stations = []
        for cat in sorted(categories, key=lambda c: c.get("sortOrder", 0)):
            items = [_clean_item(i) for i in sorted(cat.get("items", []), key=lambda i: i.get("sortOrder", 0))]
            items = [i for i in items if i["name"]]
            if items:
                stations.append({"name": (cat.get("name") or "").strip(), "items": items})
        if stations:
            result[wanted] = {"label": period.get("name") or wanted.title(), "stations": stations}
        time.sleep(0.5)  # be polite to the API
    return result


def fetch_day(date: str) -> dict[str, Any]:
    """Fetch every dining hall for one date. Individual hall failures are recorded, not fatal."""
    halls = get_dining_halls()
    out = {"date": date, "halls": []}
    for hall in halls:
        try:
            meals = get_menu_for_day(hall, date)
            error = None
        except Exception as exc:  # keep going so one broken hall doesn't blank the page
            meals, error = {}, str(exc)
        out["halls"].append({"slug": hall["slug"], "name": hall["name"], "meals": meals, "error": error})
    return out


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")
    print(json.dumps(fetch_day(day), ensure_ascii=False, indent=2))
