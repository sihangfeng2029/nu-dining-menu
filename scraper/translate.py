"""
English -> Simplified Chinese translation with a persistent cache.

Design:
  1. glossary.json  - hand-curated overrides for food terms that machine translation
                      gets wrong or renders awkwardly (e.g. "Kosher Salt" -> "粗盐").
                      Always wins over the machine translation. Edit freely.
  2. data/translations.json - cache of every string translated so far. Dining menus
                      repeat heavily (same ingredients every day), so after the first
                      week almost nothing new needs translating; the cache is committed
                      back to the repo by the GitHub Action.
  3. Google Translate's free web endpoint (no API key) for anything not yet known.
     Strings are sent in newline-joined batches to keep the number of requests low;
     if a batch comes back with a different line count, we fall back to one-by-one.

If translation fails completely (network blocked, rate-limited) the site still builds:
untranslated strings are simply shown in English and retried on the next run.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_PATH = ROOT / "scraper" / "glossary.json"
CACHE_PATH = ROOT / "data" / "translations.json"

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_BATCH_CHARS = 1800  # keep GET URLs comfortably below length limits


def _norm(s: str) -> str:
    """Cache key: case-insensitive, whitespace-collapsed."""
    return re.sub(r"\s+", " ", s).strip().lower()


class Translator:
    def __init__(self) -> None:
        self.glossary = {_norm(k): v for k, v in _load(GLOSSARY_PATH).items()}
        self.cache: dict[str, str] = _load(CACHE_PATH)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"
        self.failed = False  # once the remote service fails hard, stop hammering it

    # ---------------------------------------------------------------- public API
    def lookup(self, text: str) -> str:
        """Return the known translation, or '' if not translated yet."""
        k = _norm(text)
        return self.glossary.get(k) or self.cache.get(k, "")

    def translate_all(self, texts: list[str]) -> None:
        """Translate every unknown string in `texts` and store results in the cache."""
        todo = sorted({t.strip() for t in texts if t and t.strip() and not self.lookup(t)})
        print(f"[translate] {len(todo)} new strings to translate")
        for batch in _batches(todo, MAX_BATCH_CHARS):
            if self.failed:
                break
            out = self._google_batch(batch)
            if out is None or len(out) != len(batch):
                out = [self._google_one(t) for t in batch]
            for src, dst in zip(batch, out):
                if dst:
                    self.cache[_norm(src)] = dst.strip()
            time.sleep(0.4)

    def save(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(dict(sorted(self.cache.items())), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---------------------------------------------------------------- internals
    def _request(self, text: str) -> str | None:
        params = {"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text}
        for attempt in range(3):
            try:
                r = self.session.get(GOOGLE_URL, params=params, timeout=30)
                if r.status_code == 200:
                    # Response: [[["译文","source",...], ...], ...] - one entry per sentence chunk.
                    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
            except Exception:
                time.sleep(2 * (attempt + 1))
        return None

    def _google_batch(self, batch: list[str]) -> list[str] | None:
        res = self._request("\n".join(batch))
        if res is None:
            return None
        return [line.strip() for line in res.split("\n")]

    def _google_one(self, text: str) -> str:
        res = self._request(text)
        if res is None:
            self.failed = True
            print("[translate] remote translation unavailable; will retry next run")
            return ""
        return res


def _load(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _batches(items: list[str], max_chars: int):
    batch: list[str] = []
    size = 0
    for it in items:
        if batch and size + len(it) + 1 > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += len(it) + 1
    if batch:
        yield batch


# ------------------------------------------------------------------ ingredient parsing
def split_ingredients(raw: str) -> list[str]:
    """Split an ingredient statement on top-level commas.

    Commas inside parentheses/brackets belong to a sub-ingredient list, e.g.
    "Mayonnaise (Soybean Oil, Egg Yolk, Vinegar), Celery" -> ["Mayonnaise (Soybean Oil, Egg Yolk, Vinegar)", "Celery"].
    Marker characters the menu system appends (^ = sub-recipe, * = allergen note) are removed.
    """
    raw = raw.replace("^", "").replace("*", "")
    parts, depth, cur = [], 0, []
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" .;")
        if p and p.lower() not in {"contains", "and"}:
            out.append(p)
    # De-duplicate while preserving order (some statements repeat "Water").
    seen, uniq = set(), []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower())
            uniq.append(p)
    return uniq
