"""Build the 3D parts (map tile, frame, pin) from OSM features."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from manifold3d import Manifold
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import extrude, fit_text, polygons_of, text_outline, union
from .osm import MapFeatures


KEYHOLE_HEAD_D = 9.0     # screw-head entry hole
KEYHOLE_SHANK_W = 4.5    # slot for the screw shank
KEYHOLE_SLOT_L = 10.0    # how far the piece slides down onto the screw
KEYHOLE_LIP = 1.6        # material in front of the screw head
KEYHOLE_DEPTH = 3.4      # total cavity depth from the back face


@dataclass
class Options:
    size: float = 200.0            # map tile side, mm
    radius: float = 600.0          # real-world half side covered, metres
    base_height: float = 2.4
    road_height: float = 1.2
    path_height: float = 0.8
    building_height: float = 3.6   # flat mode height; fallback scale in real mode
    building_heights: str = "flat"  # flat | real
    height_scale: float = 1.0      # vertical exaggeration for real heights
    max_building_height: float = 12.0
    water_depth: float = 0.8
    min_road_width: float = 1.0
    min_path_width: float = 0.6
    parks: bool = True
    park_height: float = 0.6
    railways: bool = True
    rail_height: float = 1.0
    min_rail_width: float = 0.8
    shape: str = "square"          # square | circle | hexagon | heart
    frame: bool = True
    border_radius: float = 6.0
    frame_width: float = 8.0
    seat_width: float = 1.2        # width of the 45-degree bevel the tile rests on in the frame, mm
    fit_clearance: float = 0.2     # gap between tile edge and frame opening, per side, mm
    text: bool = True
    text_strip: float = 24.0
    text_height: float = 1.0       # relief height (emboss) or depth (deboss/flush)
    text_style: str = "emboss"     # emboss | deboss | flush
    coordinates: bool = False      # second text line with lat/lon
    compass: bool = False
    scale_bar: bool = False
    rotate: float = 0.0            # map rotation, degrees counter-clockwise
    mount: str = "none"            # none | keyhole | magnets | stand
    magnet_diameter: float = 10.0
    magnet_thickness: float = 2.0
    pin: bool = False
    pin_size: float = 9.0          # pin head diameter, mm
    pin_height: float = 4.5        # pin height above the base, mm
    route_width: float = 1.6       # mm

    def min_base_for_mount(self) -> float:
        """Base thickness needed to fit the chosen mount cavities, mm."""
        return {"keyhole": KEYHOLE_DEPTH + 0.6,
                "magnets": self.magnet_thickness + 0.2 + 0.6}.get(self.mount, 0.0)

    @property
    def frame_thickness(self) -> float:
        """Front and back of the frame are flush with the tile's base plate."""
        return self.base_height

    @property
    def seat(self) -> float:
        """Bevel width actually used: leave material under water recesses at the tile edge."""
        floor = self.base_height - (self.water_depth if self.water_depth > 0 else 0) - 0.4
        return max(0.0, min(self.seat_width, floor, self.base_height - 0.4))

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
    return poly.buffer(-d, join_style=join) if abs(d) > 1e-9 else poly


def round_corners(poly: BaseGeometry, r: float) -> BaseGeometry:
    return poly.buffer(-r, join_style="mitre").buffer(r, quad_segs=16) if r > 0 else poly


def seated_solid(poly: BaseGeometry, height: float, seat: float, grow: float = 0.0,
                 join: str = "mitre", step: float = 0.1) -> Manifold:
    """`poly` extruded from z = 0 to `height`, its bottom edge bevelled inward at 45 degrees
    over `seat` mm, as a stack of `step`-tall slabs. `grow` offsets every slab outward."""
    poly = poly.buffer(grow, join_style=join) if grow else poly
    n = int(round(seat / step))
    if n == 0:
        return extrude(poly, 0, height)
    slabs = [extrude(inset(poly, seat - (i + 1) * step, join), i * step, step + 0.001)
             for i in range(n)]
    slabs.append(extrude(poly, n * step, height - n * step))
    return union(slabs)


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


def pin_outline(opts: Options, icon: str = "house") -> tuple[BaseGeometry, BaseGeometry]:
    """Map-pin silhouette with its tip at (0, 0), and the icon cut-out in its head."""
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
    if icon == "dot":
        return outline, Point(0, cy).buffer(0.45 * r, quad_segs=24)
    return outline, house


@dataclass
class Overlay:
    """Extra markers, in local metres (same frame as MapFeatures)."""

    pins: list[tuple[float, float]] = field(default_factory=list)  # additional stops
    route: LineString | None = None


def build_map(feats: MapFeatures, opts: Options, overlay: Overlay | None = None) -> dict[str, Manifold]:
    s = opts.mm_per_m
    outline = tile_outline(opts)
    inner = inset(outline, 0.6, _join(opts))

    roads = _scaled_lines(feats.roads, s, opts.min_road_width)
    paths = unary_union([_scaled_lines(feats.paths, s, opts.min_path_width),
                         _scaled_polys(feats.areas, s)]).difference(roads)
    building_groups = _building_groups(feats.buildings, opts)
    water = unary_union([_scaled_polys(feats.water, s),
                         _scaled_lines(feats.waterways, s, opts.min_road_width)])

    parks = _scaled_polys(feats.parks, s) if opts.parks else Polygon()
    rails = _scaled_lines(feats.rails, s, opts.min_rail_width) if opts.railways else Polygon()

    overlay = overlay or Overlay()
    pins = [((0.0, 0.0), "house")] if opts.pin else []
    for x, y in overlay.pins:
        a = math.radians(opts.rotate)
        pt = ((x * math.cos(a) - y * math.sin(a)) * s, (x * math.sin(a) + y * math.cos(a)) * s)
        if inner.contains(Point(pt)):
            pins.append((pt, "dot"))
        else:
            print(f"  warning: extra pin at {x:.0f} m, {y:.0f} m is outside the tile, skipped")
    pin_shapes = [tuple(affinity.translate(g, *pt) for g in pin_outline(opts, icon))
                  for pt, icon in pins]
    keep_out = unary_union([o.buffer(0.8) for o, _ in pin_shapes]) if pin_shapes else Polygon()
    route = Polygon()
    if overlay.route is not None:
        route = _scaled_lines([(overlay.route, 0)], s, opts.route_width)

    def finish(g):
        if opts.rotate:
            g = affinity.rotate(g, opts.rotate, origin=(0, 0))
        return _clean(g.intersection(inner).difference(keep_out))

    route = finish(route)
    roads, paths, water, parks, rails = (finish(g).difference(route)
                                         for g in (roads, paths, water, parks, rails))
    building_groups = {h: g for h, g in ((h, finish(g).difference(route))
                                         for h, g in building_groups.items()) if not g.is_empty}
    buildings = unary_union(list(building_groups.values()))
    roads = roads.difference(buildings)  # buildings win where data overlaps
    paths = paths.difference(buildings).difference(rails)
    rails = rails.difference(buildings)
    parks = _clean(parks.difference(unary_union([roads, paths, buildings, water, rails])))

    plate = outline if opts.frame else round_corners(outline, opts.border_radius)
    if opts.frame:  # bevelled underside edge: drops into the frame's matching seat
        base = seated_solid(plate, opts.base_height, opts.seat, join=_join(opts))
    else:
        base = extrude(plate, 0, opts.base_height)
    if not water.is_empty and opts.water_depth > 0:
        depth = min(opts.water_depth, opts.base_height - 0.4)
        base = base - extrude(water, opts.base_height - depth, depth + 1)

    clear = opts.seat + 1 if opts.frame else 0.6  # keep hardware clear of the bevel
    base = base - mount_cavities(inset(outline, clear, _join(opts)), opts)

    z = opts.base_height
    parts = {
        "base": base,
        "roads": extrude(roads, z, opts.road_height),
        "paths": extrude(paths, z, opts.path_height),
        "parks": extrude(parks, z, opts.park_height),
        "rails": extrude(rails, z, opts.rail_height),
        "buildings": union([extrude(g, z, h) for h, g in building_groups.items()]),
    }
    parts["route"] = extrude(route, z, max(opts.road_height, opts.path_height) + 0.6)
    if pin_shapes:
        h = opts.pin_height
        cut = min(0.6, h - 0.6)
        parts["pin"] = union([extrude(o, z, h) - extrude(icon, z + h - cut, 2.0)
                              for o, icon in pin_shapes])
    return {k: v for k, v in parts.items() if not v.is_empty()}


# ------------------------------------------------------------------------ mounts

def _symmetric_spots(area: BaseGeometry, candidates, clearance: float, want: int):
    """Pick mirrored (x, y) pairs from `candidates` whose clearance circle fits inside `area`."""
    spots = []
    for x, y in candidates:
        pair = [(x, y), (-x, y)]
        if all(area.contains(Point(p).buffer(clearance)) for p in pair):
            spots += pair
        if len(spots) >= want:
            break
    return spots[:want]


def mount_cavities(area: BaseGeometry, opts: Options) -> Manifold:
    """Cavities cut into the back (z = 0) face of the tile for hanging hardware."""
    S = opts.size
    if opts.mount == "magnets":
        r = opts.magnet_diameter / 2 + 0.1
        cands = [(0.35 * S, 0.35 * S), (0.35 * S, -0.35 * S), (0.25 * S, 0.3 * S),
                 (0.2 * S, -0.15 * S), (0.15 * S, -0.3 * S), (0.15 * S, 0.0)]
        spots = _symmetric_spots(area, cands, r + 3, 4)
        return union([extrude(Point(p).buffer(r, quad_segs=32), -1, opts.magnet_thickness + 0.2 + 1)
                      for p in spots])
    if opts.mount == "keyhole":
        cands = [(0.25 * S, 0.3 * S), (0.2 * S, 0.2 * S), (0.15 * S, 0.1 * S)]
        cuts = []
        for x, y in _symmetric_spots(area, cands, KEYHOLE_HEAD_D + KEYHOLE_SLOT_L, 2):
            head = Point(x, y).buffer(KEYHOLE_HEAD_D / 2, quad_segs=24)
            shank = box(x - KEYHOLE_SHANK_W / 2, y, x + KEYHOLE_SHANK_W / 2, y + KEYHOLE_SLOT_L)
            under = box(x - KEYHOLE_HEAD_D / 2, y, x + KEYHOLE_HEAD_D / 2, y + KEYHOLE_SLOT_L)
            shank = shank.union(Point(x, y + KEYHOLE_SLOT_L).buffer(KEYHOLE_SHANK_W / 2))
            under = under.union(Point(x, y + KEYHOLE_SLOT_L).buffer(KEYHOLE_HEAD_D / 2))
            cuts += [extrude(head.union(shank), -1, KEYHOLE_LIP + 1),           # open at the back
                     extrude(head.union(under), KEYHOLE_LIP - 0.01, KEYHOLE_DEPTH - KEYHOLE_LIP)]
        return union(cuts)
    return Manifold()


def build_stand(opts: Options) -> Manifold:
    """Desk stand: a bar with a slot, leaning 15 degrees back, that holds the bottom edge."""
    held = opts.frame_thickness if opts.frame else opts.base_height  # tile sits inside the frame
    slot_w, slot_d, lean = held + 0.4, 9.0, math.radians(15)
    depth, height = 44.0, 18.0
    length = min(0.6 * opts.size, 140.0)
    profile = Polygon([(0, 0), (depth, 0), (depth, 9), (depth - 12, height),
                       (12, height), (0, 7)])
    cx, floor = depth / 2, height - slot_d
    slot = box(-slot_w / 2, 0, slot_w / 2, 40)  # rotated about its bottom centre
    slot = affinity.rotate(slot, -math.degrees(lean), origin=(0, 0))
    slot = affinity.translate(slot, cx, floor)
    m = extrude(profile.difference(slot), 0, length)
    # profile was drawn in XY and extruded along Z; lay it down so the profile stands upright
    m = m.transform(np.array([[0, 0, 1, -length / 2], [1, 0, 0, -cx], [0, 1, 0, 0]], dtype=float))
    return m


# ------------------------------------------------------------------------- frame

def compass_rose(d: float, angle: float = 0.0) -> BaseGeometry:
    """8-point compass star with an 'N', fitting a d x d box centred on the origin."""
    r_long, r_short, r_mid = 0.34 * d, 0.13 * d, 0.2 * d
    pts = []
    for i in range(16):  # alternate long/short points with narrow waists between them
        a = math.radians(90 - i * 22.5)
        r = (r_long if i % 4 == 0 else r_mid) if i % 2 == 0 else 0.06 * d
        pts.append((r * math.cos(a), r * math.sin(a)))
    star = Polygon(pts).union(Point(0, 0).buffer(r_short * 0.45)).difference(Point(0, 0).buffer(r_short * 0.2))
    star = affinity.translate(star, 0, -0.1 * d)
    n = text_outline("N")
    nx0, ny0, nx1, ny1 = n.bounds
    k = 0.2 * d / (ny1 - ny0)
    n = affinity.translate(affinity.scale(n, k, k, origin=(0, 0)), -(nx0 + nx1) / 2 * k,
                           0.5 * d - ny1 * k)
    # keep the N above the north tip when rotated
    tip = affinity.rotate(Point(0, -0.1 * d + r_long), angle, origin=(0, -0.1 * d))
    n = affinity.translate(n, tip.x, tip.y - (-0.1 * d + r_long))
    return unary_union([affinity.rotate(star, angle, origin=(0, -0.1 * d)), n])


def scale_bar(max_w: float, h: float, mm_per_m: float) -> BaseGeometry:
    """A scale bar with a distance label, at most max_w wide and h tall, centred on the origin."""
    nice = [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000]
    fitting = [d for d in nice if d * mm_per_m <= max_w * 0.9]
    dist = fitting[-1] if fitting else nice[0]
    w = dist * mm_per_m
    thick, tick = max(0.12 * h, 1.0), 0.3 * h
    y = 0.05 * h
    bar = unary_union([
        box(-w / 2, y, w / 2, y + thick),
        box(-w / 2, y, -w / 2 + thick, y + tick), box(w / 2 - thick, y, w / 2, y + tick),
        box(-thick / 2, y, thick / 2, y + tick * 0.6),
    ])
    label = f"{dist // 1000} km" if dist >= 1000 and dist % 1000 == 0 else f"{dist} m"
    txt = fit_text(label, w, 0.38 * h)
    _, ty0, _, ty1 = txt.bounds
    txt = affinity.translate(txt, 0, y - 0.12 * h - ty1)
    return unary_union([bar, txt])


def build_frame(opts: Options, label: str | None, coords: str | None = None) -> dict[str, Manifold]:
    """A frame the tile drops into from the front and sits flush in ("stomp").

    Frame and tile base are equally thick, so both faces are flush. The opening is the tile
    outline plus `fit_clearance`, narrowing at the back along a 45-degree seat that matches
    the bevel under the tile's edge and stops it falling through.
    """
    fw = opts.frame_width
    tile = tile_outline(opts)
    opening = tile.buffer(opts.fit_clearance, join_style=_join(opts))
    minx, miny, maxx, _ = opening.bounds
    label = label if opts.text else None
    coords = coords if opts.coordinates else None
    has_text = bool(label or coords or opts.compass or opts.scale_bar)
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
    if opts.seat:
        cutter = seated_solid(tile, t + 1, opts.seat, opts.fit_clearance, _join(opts))
        body = extrude(outer, 0, t) - cutter.translate((0, 0, -0.01))
    else:
        body = extrude(outer.difference(opening), 0, t)
    if text_box is None:
        return {"frame": body}

    max_w, max_h, cy = text_box
    deco = []
    side = 0.0
    if opts.compass or opts.scale_bar:
        side = min(max_h * 2.2, max_w * 0.22)
        if opts.compass:
            deco.append(affinity.translate(compass_rose(max_h * 1.45, opts.rotate), -max_w / 2 + side / 2, cy))
        if opts.scale_bar:
            deco.append(affinity.translate(scale_bar(side, max_h, opts.mm_per_m), max_w / 2 - side / 2, cy))
    text_w = max_w - 2 * (side + 3) if side else max_w
    if label and coords:
        deco += [affinity.translate(fit_text(label, text_w, max_h * 0.62), 0, cy + max_h * 0.2),
                 affinity.translate(fit_text(coords, text_w, max_h * 0.3), 0, cy - max_h * 0.36)]
    elif label or coords:
        deco.append(affinity.translate(fit_text(label or coords, text_w, max_h), 0, cy))
    txt = unary_union(deco)
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
