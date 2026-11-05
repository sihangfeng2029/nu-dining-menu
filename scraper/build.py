"""
Daily pipeline: scrape -> translate -> write JSON -> assemble static site.

Run:  python scraper/build.py               # today + next 2 days (America/Chicago)
      python scraper/build.py 2026-11-03    # specific date(s)
      python scraper/build.py --offline     # rebuild site from existing JSON only

Output layout (everything under site/ is what GitHub Pages serves):
  data/menus/YYYY-MM-DD.json   translated menus, committed to the repo (history)
  data/translations.json       translation cache, committed to the repo
  site/index.html              the single-page viewer
  site/data/index.json         list of available dates
  site/data/YYYY-MM-DD.json    copies of data/menus for the viewer to fetch
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from translate import Translator, split_ingredients  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MENU_DIR = ROOT / "data" / "menus"
SITE_DIR = ROOT / "site"
WEB_DIR = ROOT / "web"
KEEP_DAYS_ON_SITE = 14  # older days stay in git history / data/menus but are not listed
DAYS_AHEAD = 2          # menus are usually published a few days in advance

CHICAGO = ZoneInfo("America/Chicago")


def translate_day(raw: dict, tr: Translator) -> dict:
    """Attach Chinese translations to every user-visible string of one day's menu."""
    # 1) Collect all strings first so the translator can batch them efficiently.
    texts: list[str] = []
    for hall in raw["halls"]:
        for meal in hall["meals"].values():
            for st in meal["stations"]:
                texts.append(st["name"])
                for it in st["items"]:
                    texts.append(it["name"])
                    if _desc_is_useful(it):
                        texts.append(it["desc"])
                    texts.extend(split_ingredients(it["ingredients"]))
                    texts.extend(it["tags"])
    tr.translate_all(texts)

    # 2) Rebuild the structure with {en, zh} pairs.
    def pair(s: str) -> dict:
        return {"en": s, "zh": tr.lookup(s)}

    out = {"date": raw["date"], "generated": datetime.now(CHICAGO).isoformat(timespec="minutes"), "halls": []}
    for hall in raw["halls"]:
        meals = {}
        for key, meal in hall["meals"].items():
            stations = []
            for st in meal["stations"]:
                items = []
                for it in st["items"]:
                    items.append({
                        "name": pair(it["name"]),
                        "desc": pair(it["desc"]) if _desc_is_useful(it) else None,
                        "ingredients": [pair(x) for x in split_ingredients(it["ingredients"])],
                        "tags": [pair(t) for t in it["tags"]],
                        "calories": it["calories"],
                        "portion": it["portion"],
                    })
                stations.append({"name": pair(st["name"]), "items": items})
            meals[key] = {"label": meal["label"], "stations": stations}
        out["halls"].append({"slug": hall["slug"], "name": hall["name"], "meals": meals, "error": hall.get("error")})
    return out


def _desc_is_useful(it: dict) -> bool:
    """Descriptions that merely repeat the dish name are dropped (saves screen space)."""
    d = (it.get("desc") or "").strip()
    return bool(d) and d.lower() != it["name"].strip().lower()


RAW_DIR = ROOT / "raw"


def load_raw(d: str) -> dict:
    """Load a raw (untranslated) menu dump uploaded to raw/YYYY-MM-DD.json.

    The DineOnCampus API sits behind a Cloudflare bot check that blocks cloud
    servers (GitHub Actions gets HTTP 403), so the raw data is collected in a real
    browser on demand and committed to raw/. If no raw file exists we fall back to a
    direct fetch, which only works from networks the API accepts.
    """
    f = RAW_DIR / f"{d}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    from fetch_menu import fetch_day  # imported lazily so --offline needs no network libs
    return fetch_day(d)


def expand_bundles() -> None:
    """Unpack raw/*.b64 bundles into raw/YYYY-MM-DD.json.

    A bundle is base64(gzip(JSON {"YYYY-MM-DD": day, ...})). The browser collector
    uploads this compact form through the GitHub web editor (~50 KB instead of
    ~500 KB of JSON per two days).
    """
    import base64
    import gzip

    if not RAW_DIR.exists():
        return
    for b in sorted(RAW_DIR.glob("*.b64")):
        days = json.loads(gzip.decompress(base64.b64decode(b.read_text().strip())))
        for d, day in days.items():
            (RAW_DIR / f"{d}.json").write_text(json.dumps(day, ensure_ascii=False), encoding="utf-8")
        print(f"[build] expanded {b.name}: {', '.join(days)}")


def scrape_and_store(dates: list[str]) -> None:
    tr = Translator()
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    for d in dates:
        print(f"[build] loading {d}")
        try:
            raw = load_raw(d)
        except Exception as exc:
            print(f"[build] fetch failed for {d}: {exc}")
            continue
        has_food = any(h["meals"] for h in raw["halls"])
        target = MENU_DIR / f"{d}.json"
        if not has_food and target.exists():
            # Don't overwrite a good menu with an empty one (e.g. transient API outage).
            print(f"[build] {d}: no menu returned, keeping previous file")
            continue
        day = translate_day(raw, tr)
        target.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
        tr.save()  # save after every day so progress survives a later crash
    tr.save()


def retranslate_existing(tr: Translator) -> None:
    """Fill in Chinese for strings that were left blank in earlier runs (e.g. translator was down)."""
    for f in sorted(MENU_DIR.glob("*.json")):
        day = json.loads(f.read_text(encoding="utf-8"))
        missing = []

        def walk(o):
            if isinstance(o, dict):
                if set(o.keys()) == {"en", "zh"} and not o["zh"] and o["en"]:
                    missing.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(day)
        if not missing:
            continue
        tr.translate_all([m["en"] for m in missing])
        for m in missing:
            m["zh"] = tr.lookup(m["en"])
        f.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    tr.save()


def assemble_site() -> None:
    """Copy the viewer and the recent menus into site/ for GitHub Pages."""
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    (SITE_DIR / "data").mkdir(parents=True)
    shutil.copy(WEB_DIR / "index.html", SITE_DIR / "index.html")
    (SITE_DIR / ".nojekyll").write_text("")

    today = datetime.now(CHICAGO).date()
    cutoff = today - timedelta(days=KEEP_DAYS_ON_SITE)
    dates = []
    for f in sorted(MENU_DIR.glob("*.json")):
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= cutoff:
            shutil.copy(f, SITE_DIR / "data" / f.name)
            dates.append(f.stem)
    (SITE_DIR / "data" / "index.json").write_text(
        json.dumps({"dates": dates, "updated": datetime.now(CHICAGO).isoformat(timespec="minutes")}),
        encoding="utf-8",
    )
    print(f"[build] site assembled with {len(dates)} day(s)")


def main(argv: list[str]) -> None:
    offline = "--offline" in argv
    dates = [a for a in argv if not a.startswith("--")]
    if not offline:
        expand_bundles()
        if not dates:
            # Re-process recent raw dumps (git checkouts reset mtimes, so go by date).
            # Older days are already translated in data/menus and are left alone.
            since = (datetime.now(CHICAGO).date() - timedelta(days=1)).isoformat()
            dates = sorted(f.stem for f in RAW_DIR.glob("*.json") if f.stem >= since) if RAW_DIR.exists() else []
        scrape_and_store(dates)
        retranslate_existing(Translator())
    assemble_site()


if __name__ == "__main__":
    main(sys.argv[1:])
