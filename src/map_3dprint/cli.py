"""Command-line entry point: address in, printable STL files out."""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
from pathlib import Path

from .geometry import write_3mf, write_stl
from .model import Options, Overlay, build_frame, build_map, build_stand, describe_scale, merged
from shapely.geometry import LineString

from .osm import Location, OverpassError, Projector, fetch_features, geocode, locate, route

# Display colours for the 3MF parts (slicers show these; assign filaments per part).
COLORS = {
    "base": "#2B2B2B", "roads": "#F2F2F2", "paths": "#A8A8A8", "buildings": "#E0B04A",
    "parks": "#5DA35D", "rails": "#7A5C48",
    "pin": "#D7263D", "route": "#D7263D", "frame": "#2B2B2B", "text": "#F2F2F2", "stand": "#2B2B2B",
}


def format_coords(lat: float, lon: float) -> str:
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "map"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    d = Options()
    p = argparse.ArgumentParser(
        prog="map-3dprint",
        description="Generate 3D-printable city map tiles (STL) around an address, from OpenStreetMap.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("address", help="Address to centre the map on (also used as the frame text)")
    p.add_argument("--coords", metavar="LAT,LON", help="Use these coordinates instead of geocoding")
    p.add_argument("-o", "--out-dir", type=Path, default=Path("output"), help="Output directory")
    p.add_argument("--name", help="Base filename for the STLs (default: slug of the address)")

    g = p.add_argument_group("map")
    g.add_argument("--size", type=float, default=d.size, help="Map tile side length, mm")
    g.add_argument("--shape", choices=("square", "circle", "hexagon", "heart"), default=d.shape,
                   help="Outline of the map tile (the frame follows it)")
    g.add_argument("--radius", type=float, default=d.radius,
                   help="Real-world distance from centre to tile edge, metres")
    g.add_argument("--base-height", type=float, default=d.base_height, help="Base plate thickness, mm")
    g.add_argument("--road-height", type=float, default=d.road_height, help="Road relief above base, mm")
    g.add_argument("--path-height", type=float, default=d.path_height, help="Footpath relief above base, mm")
    g.add_argument("--building-height", type=float, default=d.building_height,
                   help="Building relief above base, mm (flat mode)")
    g.add_argument("--building-heights", choices=("flat", "real"), default=d.building_heights,
                   help="flat = all buildings the same height; real = from OSM height/levels tags "
                        "(at map scale, untagged buildings use the local median)")
    g.add_argument("--height-scale", type=float, default=d.height_scale,
                   help="Vertical exaggeration for --building-heights real")
    g.add_argument("--max-building-height", type=float, default=d.max_building_height,
                   help="Cap for real building heights, mm")
    g.add_argument("--water-depth", type=float, default=d.water_depth,
                   help="Depth water is recessed into the base, mm (0 = off)")
    g.add_argument("--parks", action=argparse.BooleanOptionalAction, default=d.parks,
                   help="Raise parks, gardens, woods and grass as a thin layer")
    g.add_argument("--park-height", type=float, default=d.park_height, help="Park relief above base, mm")
    g.add_argument("--railways", action=argparse.BooleanOptionalAction, default=d.railways,
                   help="Include above-ground railway lines")
    g.add_argument("--rail-height", type=float, default=d.rail_height, help="Railway relief above base, mm")
    g.add_argument("--min-road-width", type=float, default=d.min_road_width, help="Minimum road width, mm")
    g.add_argument("--min-path-width", type=float, default=d.min_path_width, help="Minimum path width, mm")
    g.add_argument("--pin", action=argparse.BooleanOptionalAction, default=d.pin,
                   help="Add a raised map pin with a house icon at the address")
    g.add_argument("--pin-size", type=float, default=d.pin_size, help="Pin head diameter, mm")
    g.add_argument("--add-pin", action="append", default=[], metavar="ADDRESS",
                   help="Extra pin (address or 'lat,lon'); repeat for several. Must fall on the tile.")
    g.add_argument("--route", choices=("none", "straight", "walk", "bike", "drive"), default="none",
                   help="Raised route from the main address through the --add-pin stops, in order")
    g.add_argument("--route-width", type=float, default=d.route_width, help="Route line width, mm")

    g.add_argument("--mount", choices=("none", "keyhole", "magnets", "stand"), default=d.mount,
                   help="Hanging/standing hardware: keyhole slots or magnet pockets in the back of the tile "
                        "(the base is thickened to fit), or a separate desk stand piece")
    g.add_argument("--magnet-diameter", type=float, default=d.magnet_diameter, help="Magnet diameter, mm")
    g.add_argument("--magnet-thickness", type=float, default=d.magnet_thickness, help="Magnet thickness, mm")

    g = p.add_argument_group("frame")
    g.add_argument("--frame", action=argparse.BooleanOptionalAction, default=d.frame,
                   help="Generate the frame/bezel STL")
    g.add_argument("--border-radius", type=float, default=d.border_radius,
                   help="Corner radius of the frame (or of the tile when --no-frame), mm")
    g.add_argument("--frame-width", type=float, default=d.frame_width, help="Frame side width, mm")
    g.add_argument("--frame-overlap", type=float, default=d.frame_overlap,
                   help="How far the frame covers the tile edge, mm")
    g.add_argument("--frame-thickness", type=float, default=d.frame_thickness, help="Frame thickness, mm")
    g.add_argument("--text", action=argparse.BooleanOptionalAction, default=d.text,
                   help="Put the address text on the frame")
    g.add_argument("--label", help="Custom frame text (default: the address as given)")
    g.add_argument("--text-strip", type=float, default=d.text_strip,
                   help="Height of the frame's bottom text strip, mm")
    g.add_argument("--text-style", choices=("emboss", "deboss", "flush"), default=d.text_style,
                   help="emboss = raised letters, deboss = carved in, flush = separate inlay part level "
                        "with the surface (for a second colour; use 3MF or --split)")
    g.add_argument("--coordinates", action=argparse.BooleanOptionalAction, default=d.coordinates,
                   help="Add a second text line with the latitude/longitude")
    g.add_argument("--compass", action=argparse.BooleanOptionalAction, default=d.compass,
                   help="Compass rose on the left of the text strip")
    g.add_argument("--scale-bar", action=argparse.BooleanOptionalAction, default=d.scale_bar,
                   help="Scale bar on the right of the text strip")
    g.add_argument("--text-height", type=float, default=d.text_height,
                   help="Text relief height (emboss) or depth (deboss/flush), mm")

    p.add_argument("--format", choices=("both", "3mf", "stl"), default="both",
                   help="3MF keeps each layer as a coloured part of one object, ready for multi-colour "
                        "printing; STL is single-part meshes")
    p.add_argument("--split", action="store_true",
                   help="Also write each layer (base, roads, paths, buildings, pin, text) as its own STL")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    a = parse_args(argv)
    opts = Options(**{k: getattr(a, k) for k in Options.__dataclass_fields__ if hasattr(a, k)})
    if not opts.frame:
        opts.frame_overlap = 0.0
    if opts.base_height < opts.min_base_for_mount():
        opts.base_height = round(opts.min_base_for_mount(), 2)
        print(f"Note: base thickened to {opts.base_height} mm to fit the {opts.mount} mount")

    t0 = time.perf_counter()
    if a.coords:
        lat, lon = (float(x) for x in a.coords.split(","))
        loc = Location(lat, lon, a.address)
    else:
        print(f"Geocoding {a.address!r} ...")
        loc = geocode(a.address)
    print(f"  -> {loc.label}  ({loc.lat:.6f}, {loc.lon:.6f})")
    print(f"Downloading OpenStreetMap data, {describe_scale(opts)} ...")
    try:
        feats = fetch_features(loc, opts.radius, extras=opts.parks or opts.railways)
    except OverpassError as e:
        raise SystemExit(f"Could not download map data: {e}. Try again in a few minutes.")
    print(f"  {len(feats.roads)} roads, {len(feats.paths)} paths, {len(feats.buildings)} buildings, "
          f"{len(feats.water)} water areas, {len(feats.parks)} parks, {len(feats.rails)} railway lines")

    overlay = Overlay()
    stops = [locate(p) for p in a.add_pin]
    proj = Projector(loc.lat, loc.lon)
    overlay.pins = [proj(p.lat, p.lon) for p in stops]
    for p in stops:
        print(f"  pin: {p.label}")
    if a.route != "none":
        if not stops:
            raise SystemExit("--route needs at least one --add-pin stop")
        line = route([loc, *stops], a.route)
        overlay.route = LineString([proj(lat, lon) for lat, lon in line])
        print(f"  route ({a.route}): {overlay.route.length / 1000:.2f} km")

    name = a.name or slugify(a.address)
    out: Path = a.out_dir
    want_stl, want_3mf = a.format in ("both", "stl"), a.format in ("both", "3mf")
    written = []

    print("Building map tile ...")
    pieces = {"map": build_map(feats, opts, overlay)}
    if opts.frame:
        print("Building frame ...")
        pieces["frame"] = build_frame(opts, a.label or a.address, format_coords(loc.lat, loc.lon))
    if opts.mount == "stand":
        pieces["stand"] = {"stand": build_stand(opts)}

    if want_stl:
        for piece, parts in pieces.items():
            written.append(_save(merged(parts), out / f"{name}-{piece}.stl"))
            # A flush inlay is invisible once merged, so always split it out for STL.
            split = a.split or (piece == "frame" and opts.text_style == "flush" and "text" in parts)
            if split and len(parts) > 1:
                written += [_save(m, out / f"{name}-{piece}-{k}.stl") for k, m in parts.items()]
    if want_3mf:
        coloured = {piece: {k: (m, COLORS.get(k, "#808080")) for k, m in parts.items()}
                    for piece, parts in pieces.items()}
        # Place the frame next to the tile so both fit on the plate preview without overlapping.
        offsets = {"frame": (opts.size + 2 * opts.frame_width + 10, 0.0),
                   "stand": (0.0, -opts.size / 2 - opts.text_strip - 40)}
        path = out / f"{name}.3mf"
        written.append((path, write_3mf(coloured, path, offsets)))

    print(f"Done in {time.perf_counter() - t0:.1f}s:")
    for path, ntri in written:
        print(f"  {path}  ({ntri:,} triangles)")


def _save(mesh, path: Path) -> tuple[Path, int]:
    return path, write_stl(mesh, path, name=path.stem)
