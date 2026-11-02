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
    buildings: list[tuple[Polygon, float | None]] = field(default_factory=list)  # (footprint, height_m)
    water: list[Polygon] = field(default_factory=list)
    waterways: list[tuple[LineString, float]] = field(default_factory=list)
    parks: list[Polygon] = field(default_factory=list)
    rails: list[tuple[LineString, float]] = field(default_factory=list)


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


ROUTING_URL = "https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/{coords}"
ROUTE_PROFILES = {"walk": "foot", "bike": "bike", "drive": "car"}


def route(points: list[Location], mode: str) -> list[tuple[float, float]]:
    """(lat, lon) polyline through `points` in order; straight segments for mode 'straight'."""
    if mode == "straight":
        return [(p.lat, p.lon) for p in points]
    coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in points)
    url = ROUTING_URL.format(profile=ROUTE_PROFILES[mode], coords=coords)

    def fetch():
        r = requests.get(url, params={"overview": "full", "geometries": "geojson"},
                         headers={"User-Agent": USER_AGENT}, timeout=(15, 60))
        r.raise_for_status()
        return r.json()

    data = _cached_request(f"route:{url}", fetch)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise SystemExit(f"Routing failed: {data.get('message', data.get('code'))}")
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


def locate(text: str) -> Location:
    """Geocode an address, or parse a literal 'lat,lon'."""
    try:
        lat, lon = (float(x) for x in text.split(","))
        return Location(lat, lon, text)
    except ValueError:
        return geocode(text)


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


class OverpassError(RuntimeError):
    pass


def _overpass(query: str) -> dict:
    def fetch():
        errors = []
        for url in OVERPASS_URLS:
            for attempt in range(2):
                try:
                    r = requests.post(url, data={"data": query},
                                      headers={"User-Agent": USER_AGENT}, timeout=(15, 180))
                    if r.status_code in (429, 504):
                        errors.append(f"{url}: HTTP {r.status_code}")
                        time.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    return r.json()
                except requests.RequestException as e:
                    errors.append(f"{url}: {type(e).__name__}")
                    break  # unreachable or timed out: try the next mirror
        raise OverpassError("all Overpass servers failed (" + "; ".join(errors) + ")")

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


def fetch_features(loc: Location, half_m: float, extras: bool = True,
                   pad: float = 1.15) -> MapFeatures:
    """Download map data. `extras` (parks, railways) is a separate, optional query."""
    proj = Projector(loc.lat, loc.lon)
    s, w, n, e = proj.bbox(half_m * pad)
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
    extra_query = f"""
[out:json][timeout:120];
(
  way["leisure"~"^(park|garden)$"]({bbox});
  relation["leisure"~"^(park|garden)$"]({bbox});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|cemetery)$"]({bbox});
  relation["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|cemetery)$"]({bbox});
  way["natural"~"^(wood|scrub)$"]({bbox});
  way["railway"~"^(rail|light_rail|narrow_gauge)$"]({bbox});
);
out geom;
"""
    elements = _overpass(query).get("elements", [])
    if extras:
        try:
            elements += _overpass(extra_query).get("elements", [])
        except OverpassError as e:
            print(f"  warning: skipping parks/railways, {e}")
    feats = MapFeatures()
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "relation":
            polys = _relation_polygons(el, proj)
            if "building" in tags:
                h = building_height_m(tags)
                feats.buildings += [(p, h) for p in polys]
            elif tags.get("natural") == "water":
                feats.water += polys
            elif _is_park(tags):
                feats.parks += polys
            continue

        coords = _way_coords(el, proj)
        if len(coords) < 2:
            continue
        closed = coords[0] == coords[-1]
        hw = tags.get("highway")

        if "building" in tags and tags.get("building") != "no":
            if closed and (p := _closed_polygon(coords)):
                feats.buildings.append((p, building_height_m(tags)))
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
        elif tags.get("railway") in RAIL_TYPES:
            if tags.get("tunnel") != "yes" and not tags.get("layer", "0").startswith("-"):
                feats.rails.append((LineString(coords), 4.0))
        elif tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            if closed and (p := _closed_polygon(coords)):
                feats.water.append(p)
        elif tags.get("waterway") in ("river", "canal"):
            feats.waterways.append((LineString(coords), _tag_width(tags) or 15.0))
        elif _is_park(tags):
            if closed and (p := _closed_polygon(coords)):
                feats.parks.append(p)
    return feats


RAIL_TYPES = ("rail", "light_rail", "narrow_gauge")
PARK_TAGS = {
    "leisure": ("park", "garden"),
    "landuse": ("grass", "forest", "meadow", "recreation_ground", "village_green", "cemetery"),
    "natural": ("wood", "scrub"),
}


def _is_park(tags: dict) -> bool:
    return any(tags.get(k) in v for k, v in PARK_TAGS.items())


def _tag_number(tags: dict, key: str) -> float | None:
    try:
        return float(tags.get(key, "").split()[0].replace(",", ".").rstrip("m"))
    except (ValueError, IndexError):
        return None


def _tag_width(tags: dict) -> float | None:
    return _tag_number(tags, "width")


METRES_PER_LEVEL = 3.2


def building_height_m(tags: dict) -> float | None:
    """Real-world building height from OSM tags, or None when unknown."""
    if (h := _tag_number(tags, "height")) and h > 0:
        return h
    if (levels := _tag_number(tags, "building:levels")) and levels > 0:
        roof = _tag_number(tags, "roof:levels") or 0
        return (levels + roof) * METRES_PER_LEVEL
    return None
