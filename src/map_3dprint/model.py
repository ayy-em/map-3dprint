"""Build the 3D parts (map tile, frame, pin) from OSM features."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from manifold3d import Manifold
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import extrude, fit_text, polygons_of, union
from .osm import MapFeatures


@dataclass
class Options:
    size: float = 200.0            # map tile side, mm
    radius: float = 600.0          # real-world half side covered, metres
    base_height: float = 1.2
    road_height: float = 1.0
    path_height: float = 0.6
    building_height: float = 2.4   # flat mode height; fallback scale in real mode
    building_heights: str = "flat"  # flat | real
    height_scale: float = 1.0      # vertical exaggeration for real heights
    max_building_height: float = 8.0
    water_depth: float = 0.6
    min_road_width: float = 1.0
    min_path_width: float = 0.6
    parks: bool = True
    park_height: float = 0.4
    railways: bool = True
    rail_height: float = 0.8
    min_rail_width: float = 0.8
    shape: str = "square"          # square | circle | hexagon | heart
    frame: bool = True
    border_radius: float = 6.0
    frame_width: float = 8.0
    frame_overlap: float = 3.0
    frame_thickness: float = 2.0
    text: bool = True
    text_strip: float = 24.0
    text_height: float = 0.8       # relief height (emboss) or depth (deboss/flush)
    text_style: str = "emboss"     # emboss | deboss | flush
    pin: bool = False
    pin_size: float = 16.0         # pin head diameter, mm

    @property
    def mm_per_m(self) -> float:
        return (self.size / 2) / self.radius


# --------------------------------------------------------------------------- map

def _scaled_lines(items, scale: float, min_w: float) -> BaseGeometry:
    bufs = [
        affinity.scale(line, scale, scale, origin=(0, 0)).buffer(
            max(width * scale, min_w) / 2, quad_segs=4, cap_style="round", join_style="round")
        for line, width in items
    ]
    return unary_union(bufs) if bufs else Polygon()


def _scaled_polys(polys, scale: float) -> BaseGeometry:
    out = [affinity.scale(p, scale, scale, origin=(0, 0)) for p in polys]
    return unary_union(out) if out else Polygon()


def _clean(geom: BaseGeometry, min_area: float = 0.2, simplify: float = 0.05) -> BaseGeometry:
    geom = geom.simplify(simplify, preserve_topology=True).buffer(0)
    return unary_union([p for p in polygons_of(geom) if p.area >= min_area])


def tile_outline(opts: Options) -> Polygon:
    """Outline of the map tile, centred on the origin, fitting a `size` x `size` box."""
    h = opts.size / 2
    match opts.shape:
        case "square":
            return box(-h, -h, h, h)
        case "circle":
            return Point(0, 0).buffer(h, quad_segs=64)
        case "hexagon":  # flat top/bottom, corner-to-corner = size
            return Polygon([(h * math.cos(math.radians(a)), h * math.sin(math.radians(a)))
                            for a in range(0, 360, 60)])
        case "heart":
            t = np.linspace(0, 2 * math.pi, 241)[:-1]
            x = 16 * np.sin(t) ** 3
            y = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t)
            poly = Polygon(np.column_stack([x, y]))
            minx, miny, maxx, maxy = poly.bounds
            k = opts.size / max(maxx - minx, maxy - miny)
            return affinity.translate(affinity.scale(poly, k, k, origin=(0, 0)),
                                      -(minx + maxx) / 2 * k, -(miny + maxy) / 2 * k)
    raise ValueError(f"unknown shape {opts.shape!r}")


def _join(opts: Options) -> str:
    """Sharp joins keep polygon corners crisp; curved shapes need round joins (no cusp spikes)."""
    return "mitre" if opts.shape in ("square", "hexagon") else "round"


def inset(poly: BaseGeometry, d: float, join: str = "mitre") -> BaseGeometry:
    return poly.buffer(-d, join_style=join) if d else poly


def round_corners(poly: BaseGeometry, r: float) -> BaseGeometry:
    return poly.buffer(-r, join_style="mitre").buffer(r, quad_segs=16) if r > 0 else poly


def _building_groups(buildings, opts: Options) -> dict[float, BaseGeometry]:
    """Group building footprints (scaled to mm) by printed height, quantised to 0.2 mm."""
    s = opts.mm_per_m
    if opts.building_heights == "flat":
        return {opts.building_height: _scaled_polys([p for p, _ in buildings], s)}
    known = sorted(h for _, h in buildings if h)
    fallback = known[len(known) // 2] if known else 3 * 3.2  # median, else 3 storeys
    lo = max(opts.path_height, opts.road_height) + 0.4
    groups: dict[float, list] = {}
    for poly, h in buildings:
        mm = (h or fallback) * s * opts.height_scale
        mm = round(min(max(mm, lo), opts.max_building_height) / 0.2) * 0.2
        groups.setdefault(round(mm, 1), []).append(poly)
    return {h: _scaled_polys(polys, s) for h, polys in groups.items()}


def pin_outline(opts: Options) -> tuple[BaseGeometry, BaseGeometry]:
    """Map-pin silhouette with its tip at (0, 0), and the house cut-out in its head."""
    r = opts.pin_size / 2
    cy = 2.3 * r
    head = Point(0, cy).buffer(r, quad_segs=32)
    outline = unary_union([head, Point(0, 0).buffer(0.01)]).convex_hull
    # Little house: square body + triangular roof, centred in the head.
    w, body_h, roof_h = 1.05 * r, 0.55 * r, 0.5 * r
    y0 = cy - (body_h + roof_h) / 2
    body = box(-w / 2, y0, w / 2, y0 + body_h)
    roof = Polygon([(-w / 2 - 0.15 * r, y0 + body_h - 0.01), (w / 2 + 0.15 * r, y0 + body_h - 0.01),
                    (0, y0 + body_h + roof_h)])
    door = box(-0.13 * r, y0, 0.13 * r, y0 + 0.32 * r)
    house = unary_union([body, roof]).difference(door)
    return outline, house


def build_map(feats: MapFeatures, opts: Options) -> dict[str, Manifold]:
    s = opts.mm_per_m
    outline = tile_outline(opts)
    margin = opts.frame_overlap if opts.frame else 0.6
    inner = inset(outline, margin, _join(opts))

    roads = _scaled_lines(feats.roads, s, opts.min_road_width)
    paths = unary_union([_scaled_lines(feats.paths, s, opts.min_path_width),
                         _scaled_polys(feats.areas, s)]).difference(roads)
    building_groups = _building_groups(feats.buildings, opts)
    water = unary_union([_scaled_polys(feats.water, s),
                         _scaled_lines(feats.waterways, s, opts.min_road_width)])

    parks = _scaled_polys(feats.parks, s) if opts.parks else Polygon()
    rails = _scaled_lines(feats.rails, s, opts.min_rail_width) if opts.railways else Polygon()

    keep_out = Polygon()
    if opts.pin:
        keep_out = pin_outline(opts)[0].buffer(0.8)

    def finish(g):
        return _clean(g.intersection(inner).difference(keep_out))

    roads, paths, water, parks, rails = map(finish, (roads, paths, water, parks, rails))
    building_groups = {h: g for h, g in ((h, finish(g)) for h, g in building_groups.items())
                       if not g.is_empty}
    buildings = unary_union(list(building_groups.values()))
    roads = roads.difference(buildings)  # buildings win where data overlaps
    paths = paths.difference(buildings).difference(rails)
    rails = rails.difference(buildings)
    parks = _clean(parks.difference(unary_union([roads, paths, buildings, water, rails])))

    plate = outline if opts.frame else round_corners(outline, opts.border_radius)
    base = extrude(plate, 0, opts.base_height)
    if not water.is_empty and opts.water_depth > 0:
        depth = min(opts.water_depth, opts.base_height - 0.4)
        base = base - extrude(water, opts.base_height - depth, depth + 1)

    z = opts.base_height
    parts = {
        "base": base,
        "roads": extrude(roads, z, opts.road_height),
        "paths": extrude(paths, z, opts.path_height),
        "parks": extrude(parks, z, opts.park_height),
        "rails": extrude(rails, z, opts.rail_height),
        "buildings": union([extrude(g, z, h) for h, g in building_groups.items()]),
    }
    if opts.pin:
        outline, house = pin_outline(opts)
        tallest = max(building_groups, default=opts.building_height)
        top = z + max(tallest, opts.road_height) + 1.6
        pin = extrude(outline, z, top - z) - extrude(house, top - 0.8, 2.0)
        parts["pin"] = pin
    return {k: v for k, v in parts.items() if not v.is_empty()}


# ------------------------------------------------------------------------- frame

def build_frame(opts: Options, label: str | None) -> dict[str, Manifold]:
    """A bezel that lies on top of the map tile, covering `frame_overlap` mm of its edge."""
    fw = opts.frame_width
    opening = inset(tile_outline(opts), opts.frame_overlap, _join(opts))
    minx, miny, maxx, _ = opening.bounds
    has_text = opts.text and bool(label)
    outer = opening.buffer(fw, join_style=_join(opts))
    if not has_text:
        text_box = None
    elif opts.shape == "square":  # widen the bottom edge into a text strip
        outer = unary_union([outer, box(minx - fw, miny - opts.text_strip, maxx + fw, miny)])
        text_box = (maxx - minx - 4, opts.text_strip * 0.62, miny - opts.text_strip / 2)
    else:  # hang a plaque below the shape
        pw, ph = 0.75 * opts.size, opts.text_strip * 0.75
        top = miny - fw / 2
        outer = unary_union([outer, box(-pw / 2, top - fw / 2 - ph, pw / 2, top)])
        text_box = (pw - 6, ph * 0.62, top - fw / 2 - ph / 2)
    outer = round_corners(outer, opts.border_radius)
    t = opts.frame_thickness
    body = extrude(outer.difference(opening), 0, t)
    if text_box is None:
        return {"frame": body}

    max_w, max_h, cy = text_box
    txt = affinity.translate(fit_text(label, max_w, max_h), 0, cy)
    depth = min(opts.text_height, t - 0.6)  # keep >= 0.6 mm of floor under a recess
    match opts.text_style:
        case "emboss":  # raised letters on top of the frame
            return {"frame": body, "text": extrude(txt, t, opts.text_height)}
        case "deboss":  # letters carved into the frame
            return {"frame": body - extrude(txt, t - depth, depth + 1)}
        case "flush":  # letters inlaid level with the surface: a separate part filling a pocket
            return {"frame": body - extrude(txt, t - depth, depth + 1),
                    "text": extrude(txt, t - depth, depth)}
    raise ValueError(f"unknown text style {opts.text_style!r}")


def merged(parts: dict[str, Manifold]) -> Manifold:
    return union(list(parts.values()))


def describe_scale(opts: Options) -> str:
    return (f"{opts.size:.0f} mm tile covers {2 * opts.radius:.0f} m "
            f"(1:{round(1000 / opts.mm_per_m):,})")
