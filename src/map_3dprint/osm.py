"""Geocoding (Nominatim) and map data download (Overpass) from OpenStreetMap."""

from __future__ import annotations

import hashlib
import json
import math
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import requests
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import linemerge, polygonize, unary_union

USER_AGENT = "map-3dprint/0.1 (+https://github.com/ayy-em/map-3dprint)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
CACHE_DIR = Path.home() / ".cache" / "map-3dprint"

# Approximate real-world road widths in metres, per OSM highway=* class.
ROAD_WIDTHS_M = {
    "motorway": 22, "motorway_link": 10,
    "trunk": 20, "trunk_link": 10,
    "primary": 18, "primary_link": 9,
    "secondary": 15, "secondary_link": 8,
    "tertiary": 12, "tertiary_link": 7,
    "residential": 9, "unclassified": 9, "living_street": 7,
    "road": 8, "service": 5, "busway": 7,
}
PATH_WIDTHS_M = {
    "pedestrian": 6, "footway": 3, "path": 2.5, "cycleway": 3,
    "steps": 3, "bridleway": 3, "track": 3,
}


@dataclass
class Location:
    lat: float
    lon: float
    label: str


@dataclass
class MapFeatures:
    """Raw OSM features projected to local metres (origin = address point, +y north)."""

    roads: list[tuple[LineString, float]] = field(default_factory=list)  # (line, width_m)
    paths: list[tuple[LineString, float]] = field(default_factory=list)
    areas: list[Polygon] = field(default_factory=list)  # pedestrian plazas etc.
    buildings: list[Polygon] = field(default_factory=list)
    water: list[Polygon] = field(default_factory=list)
    waterways: list[tuple[LineString, float]] = field(default_factory=list)


def _cached_request(key: str, fetch) -> dict | list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def geocode(address: str) -> Location:
    def fetch():
        r = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    results = _cached_request(f"nominatim:{address}", fetch)
    if not results:
        raise SystemExit(f"Could not geocode address: {address!r}")
    hit = results[0]
    return Location(float(hit["lat"]), float(hit["lon"]), hit.get("display_name", address))


class Projector:
    """Local equirectangular projection: degrees -> metres around an origin."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.mx = 111_320.0 * math.cos(math.radians(lat0))
        self.my = 110_540.0

    def __call__(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.mx, (lat - self.lat0) * self.my

    def bbox(self, half_m: float) -> tuple[float, float, float, float]:
        dlat, dlon = half_m / self.my, half_m / self.mx
        return (self.lat0 - dlat, self.lon0 - dlon, self.lat0 + dlat, self.lon0 + dlon)


def _overpass(query: str) -> dict:
    def fetch():
        last_err: Exception | None = None
        for url in OVERPASS_URLS:
            for attempt in range(2):
                try:
                    r = requests.post(url, data={"data": query},
                                      headers={"User-Agent": USER_AGENT}, timeout=180)
                    if r.status_code in (429, 504):
                        time.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    return r.json()
                except requests.RequestException as e:
                    last_err = e
        raise SystemExit(f"Overpass API request failed: {last_err}")

    return _cached_request(f"overpass:{query}", fetch)


def _way_coords(el: dict, proj: Projector) -> list[tuple[float, float]]:
    return [proj(p["lat"], p["lon"]) for p in el.get("geometry", []) if p]


def _relation_polygons(el: dict, proj: Projector) -> list[Polygon]:
    outers, inners = [], []
    for m in el.get("members", []):
        if m.get("type") != "way" or "geometry" not in m:
            continue
        coords = [proj(p["lat"], p["lon"]) for p in m["geometry"] if p]
        if len(coords) >= 2:
            (inners if m.get("role") == "inner" else outers).append(LineString(coords))
    if not outers:
        return []
    with warnings.catch_warnings():  # degenerate member rings are common in OSM data
        warnings.simplefilter("ignore", RuntimeWarning)
        shell = unary_union(list(polygonize(linemerge(outers))))
        holes = unary_union(list(polygonize(linemerge(inners)))) if inners else None
    if holes is not None:
        shell = shell.difference(holes)
    if isinstance(shell, Polygon):
        return [shell]
    if isinstance(shell, MultiPolygon):
        return list(shell.geoms)
    return []


def _closed_polygon(coords) -> Polygon | None:
    if len(coords) < 4:
        return None
    poly = Polygon(coords)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if not poly.is_empty and isinstance(poly, Polygon) else None


def fetch_features(loc: Location, half_m: float) -> MapFeatures:
    proj = Projector(loc.lat, loc.lon)
    s, w, n, e = proj.bbox(half_m * 1.15)
    bbox = f"{s:.7f},{w:.7f},{n:.7f},{e:.7f}"
    query = f"""
[out:json][timeout:120];
(
  way["highway"]({bbox});
  way["building"]({bbox});
  relation["building"]({bbox});
  way["natural"="water"]({bbox});
  relation["natural"="water"]({bbox});
  way["waterway"~"^(river|canal|riverbank)$"]({bbox});
);
out geom;
"""
    data = _overpass(query)
    feats = MapFeatures()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "relation":
            polys = _relation_polygons(el, proj)
            if "building" in tags:
                feats.buildings += polys
            elif tags.get("natural") == "water":
                feats.water += polys
            continue

        coords = _way_coords(el, proj)
        if len(coords) < 2:
            continue
        closed = coords[0] == coords[-1]
        hw = tags.get("highway")

        if "building" in tags and tags.get("building") != "no":
            if closed and (p := _closed_polygon(coords)):
                feats.buildings.append(p)
        elif hw:
            if tags.get("tunnel") in ("yes", "building_passage") or tags.get("layer", "0").startswith("-"):
                continue
            if closed and tags.get("area") == "yes":
                if hw in PATH_WIDTHS_M or hw in ROAD_WIDTHS_M:
                    if p := _closed_polygon(coords):
                        feats.areas.append(p)
                continue
            line = LineString(coords)
            if hw in ROAD_WIDTHS_M:
                width = _tag_width(tags) or ROAD_WIDTHS_M[hw]
                feats.roads.append((line, width))
            elif hw in PATH_WIDTHS_M:
                feats.paths.append((line, PATH_WIDTHS_M[hw]))
        elif tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            if closed and (p := _closed_polygon(coords)):
                feats.water.append(p)
        elif tags.get("waterway") in ("river", "canal"):
            feats.waterways.append((LineString(coords), _tag_width(tags) or 15.0))
    return feats


def _tag_width(tags: dict) -> float | None:
    try:
        return float(tags.get("width", "").split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None
