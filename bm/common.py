"""Shared paths, HTTP session and name normalization for the by-law monitor."""
from __future__ import annotations

import json
import os
import time
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests
import sys

sys.stdout.reconfigure(encoding="utf-8")

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data"
CONFIG_FILE = PROJECT / "config.local.json"


def _load_config() -> dict:
    """Machine paths live in config.local.json (not in git); see config.example.json."""
    if not CONFIG_FILE.exists():
        raise SystemExit(f"{CONFIG_FILE} not found. Copy config.example.json to "
                         "config.local.json and set the folders for this machine.")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))


def _config_path(cfg: dict, key: str) -> Path:
    """A folder from the config. A relative path is taken from the project folder."""
    value = cfg.get(key)
    if not value:
        raise SystemExit(f'"{key}" is not set in {CONFIG_FILE}; see config.example.json.')
    p = Path(value)
    return p if p.is_absolute() else Path(os.path.normpath(PROJECT / p))


_CFG = _load_config()
MAPS = _config_path(_CFG, "maps_dir")     # the by-law library: one folder per municipality
OUTPUT = MAPS / "$Bylaw-Monitor"          # register, staging, digests (user-facing)
STAGING = OUTPUT / "Staging"
MB_PARCELSEARCH = _config_path(_CFG, "mb_parcelsearch_dir")   # checkout of manitoba-opendata-parcelsearch
MAIN_JS = MB_PARCELSEARCH / "web" / "src" / "main.js"
MUNI_GEOJSON = MB_PARCELSEARCH / "web" / "public" / "mb-municipalities.geojson"

ARCGIS = "https://services.arcgis.com/mMUesHYPkXjaFGfS/arcgis/rest/services"
ZONING_URL = f"{ARCGIS}/Manitoba_Zoning_By_Laws/FeatureServer/0"
DEVPLAN_URL = f"{ARCGIS}/Manitoba_Development_Plan_Designations/FeatureServer/0"

# Maps folders that are not a municipality (or are out of scope).
NON_MUNI_FOLDERS = {
    ".claude", "Aboriginal and Northern Affairs", "Manitoba", "MLS", "Saskatchewan",
    "Test", "Winnipeg", "zMisc Maps-Consolidations", "Whiteshell Provincial Park",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"})
    return s


def today() -> str:
    return date.today().strftime("%Y%m%d")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for attempt in range(10):          # Dropbox briefly locks files it is syncing
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1 + attempt)


_TYPE_WORDS = r"\b(rm|r\.m\.|mun|municipality|town|city|village|lgd|of|the|rural|local government district)\b"


def norm_muni(name: str | None) -> str:
    """Loose key for matching municipality names across open data, folders and websites.

    'Norfolk-Treherne' / 'NORFOLK TREHERNE (MUNICIPALITY)' / 'Norfolk Treherne' -> 'norfolktreherne'
    'St. Clements, RM' / 'ST CLEMENTS (RM)' -> 'stclements';  'Ste. Anne' -> 'steanne'
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bsainte\b", "ste", s)
    s = re.sub(_TYPE_WORDS, " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def norm_bylaw(num: str | None) -> str:
    """Canonical by-law number for comparison: '08-2023' -> '8-2023', '6/05' -> '6-05', 'No. 1709-2013' -> '1709-2013'."""
    if not num:
        return ""
    s = str(num).strip().lower()
    if s in ("<null>", "null", "n/a", "none"):
        return ""
    s = re.sub(r"^(by-?law|no\.?|#|\s)+", "", s)
    s = re.sub(r"(?<=\d)[a-z]+$", "", s)          # '2/99P' -> '2/99
    s = re.sub(r"[\s/_.]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return "-".join(p.lstrip("0") or "0" for p in s.split("-"))


def bylaw_variants(num: str | None) -> set[str]:
    """Spellings a by-law number may take in a filename or PDF, e.g. '8-18' also as '8-2018', '818'."""
    n = norm_bylaw(num)
    if not n:
        return set()
    out = {n}
    parts = n.split("-")
    if len(parts) == 2:
        a, b = parts
        if len(b) == 2 and b.isdigit():
            out.add(f"{a}-{'20' if int(b) < 50 else '19'}{b}")
        if len(b) == 4 and b.isdigit():
            out.add(f"{a}-{b[2:]}")
        if len(a) == 4 and a.isdigit() and len(b) <= 3:   # '2021-04' (year first)
            out.add(f"{b}-{a}")
    return out
