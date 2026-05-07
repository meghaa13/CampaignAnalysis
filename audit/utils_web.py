import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import pandas as pd

def fetch_page_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        print(f"[FAIL] Failed to fetch page {url}: {e}")
        return ""

def normalize_url(url):
    try:
        parsed = urlparse(url)
        normalized = parsed._replace(fragment="", query="")
        path = normalized.path.rstrip("/")
        normalized = normalized._replace(path=path)
        return urlunparse(normalized)
    except Exception:
        return url

def extract_location_parts(canonical_name):
    """
    Parse Google's canonical geo name format: 'City, Region, Country'
    Handles edge cases: GeoID fallbacks, country-only, region+country.
    """
    name_str = str(canonical_name).strip()

    # GeoID fallback (CSV not loaded) - put in Country, leave rest blank
    if not name_str or name_str.lower().startswith("geoid "):
        return pd.Series({"City": "", "Region": "", "Country": name_str})

    parts = [p.strip() for p in name_str.split(",")]

    if len(parts) >= 3:
        return pd.Series({"City": parts[0], "Region": parts[1], "Country": parts[2]})
    elif len(parts) == 2:
        # e.g. "Maharashtra, India" → region + country, no city
        return pd.Series({"City": "", "Region": parts[0], "Country": parts[1]})
    else:
        # Single part — treat as country
        return pd.Series({"City": "", "Region": "", "Country": parts[0]})

_GEOTARGETS_CACHE = None

def resolve_geo_names_from_csv(geo_ids):
    """
    Resolve geo IDs to human-readable names.
    Uses GEO_LOOKUP_DF from config (already loaded + indexed on 'Criteria ID').
    Falls back to loading CSV directly if config import fails.
    Single source of truth — no double-loading, no path mismatch.
    """
    global _GEOTARGETS_CACHE

    # ── Try using GEO_LOOKUP_DF from config (already loaded at startup) ───────
    if _GEOTARGETS_CACHE is None:
        try:
            try:
                from audit.config import GEO_LOOKUP_DF
            except ImportError:
                from .config import GEO_LOOKUP_DF

            if GEO_LOOKUP_DF is not None and not GEO_LOOKUP_DF.empty:
                _GEOTARGETS_CACHE = {}
                for gid_val, row in GEO_LOOKUP_DF.iterrows():
                    try:
                        gid = int(gid_val)
                    except Exception:
                        continue
                    # Google CSV columns: Name, Canonical Name, Target Type, Country Code
                    name      = str(row.get("Name", row.get("name", "")) or "")
                    canonical = str(row.get("Canonical Name", row.get("canonical_name", name)) or name)
                    ttype     = str(row.get("Target Type", row.get("type", "")) or "")
                    country   = str(row.get("Country Code", row.get("country_code", "")) or "")
                    _GEOTARGETS_CACHE[gid] = {
                        "name":           name,
                        "canonical_name": canonical or name,
                        "type":           ttype,
                        "country_code":   country,
                    }
                print(f"[GEO] Loaded {len(_GEOTARGETS_CACHE)} geotargets from config.GEO_LOOKUP_DF")
            else:
                _GEOTARGETS_CACHE = {}
                print("[GEO] GEO_LOOKUP_DF empty — geo names will show as GeoID fallbacks")
        except Exception as e:
            print(f"[GEO] Could not import GEO_LOOKUP_DF from config: {e}")
            # Hard fallback: load CSV directly
            _GEOTARGETS_CACHE = _load_geo_csv_direct()

    result = {}
    for geo_id in geo_ids:
        try:
            gid = int(geo_id)
            cached = _GEOTARGETS_CACHE.get(gid)
            if cached:
                canonical = cached.get("canonical_name") or cached.get("name") or f"GeoID {gid}"
                result[gid] = {
                    "name":           cached.get("name", f"GeoID {gid}"),
                    "canonical_name": canonical,
                    "type":           cached.get("type", ""),
                    "country_code":   cached.get("country_code", ""),
                }
            else:
                result[gid] = {
                    "name":           f"GeoID {gid}",
                    "canonical_name": f"GeoID {gid}",
                    "type":           "Unknown",
                    "country_code":   "",
                }
        except Exception:
            continue
    return result


def _load_geo_csv_direct():
    """Hard fallback: load geotargets CSV directly (used if config import fails)."""
    import os
    cache = {}
    path = os.environ.get("GEOTARGETS_PATH",
           os.environ.get("GEO_CSV_PATH", "geotargets-2025-07-15.csv"))
    if not os.path.exists(path):
        print(f"[GEO] CSV not found at {path}")
        return cache
    try:
        import pandas as _pd
        df = _pd.read_csv(path, dtype=str)
        id_col       = next((c for c in df.columns if "criteria" in c.lower() or c.lower() == "id"), None)
        name_col     = next((c for c in df.columns if c.lower() in ("name", "location name")), None)
        canonical_col= next((c for c in df.columns if "canonical" in c.lower()), None)
        type_col     = next((c for c in df.columns if "type" in c.lower()), None)
        country_col  = next((c for c in df.columns if "country" in c.lower()), None)
        if id_col and name_col:
            for _, row in df.iterrows():
                try:
                    gid = int(str(row[id_col]).strip())
                except Exception:
                    continue
                name      = str(row.get(name_col, "") or "")
                canonical = str(row.get(canonical_col, name) if canonical_col else name)
                cache[gid] = {
                    "name":           name,
                    "canonical_name": canonical or name,
                    "type":           str(row.get(type_col, "") if type_col else ""),
                    "country_code":   str(row.get(country_col, "") if country_col else ""),
                }
            print(f"[GEO] Loaded {len(cache)} geotargets from CSV directly")
    except Exception as e:
        print(f"[GEO] CSV load error: {e}")
    return cache