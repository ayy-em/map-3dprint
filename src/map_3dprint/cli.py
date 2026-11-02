"""Command-line entry point: address in, printable STL files out."""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
from pathlib import Path

from .geometry import write_stl
from .model import Options, build_frame, build_map, describe_scale, merged
from .osm import Location, fetch_features, geocode


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
    g.add_argument("--radius", type=float, default=d.radius,
                   help="Real-world distance from centre to tile edge, metres")
    g.add_argument("--base-height", type=float, default=d.base_height, help="Base plate thickness, mm")
    g.add_argument("--road-height", type=float, default=d.road_height, help="Road relief above base, mm")
    g.add_argument("--path-height", type=float, default=d.path_height, help="Footpath relief above base, mm")
    g.add_argument("--building-height", type=float, default=d.building_height,
                   help="Building relief above base, mm")
    g.add_argument("--water-depth", type=float, default=d.water_depth,
                   help="Depth water is recessed into the base, mm (0 = off)")
    g.add_argument("--min-road-width", type=float, default=d.min_road_width, help="Minimum road width, mm")
    g.add_argument("--min-path-width", type=float, default=d.min_path_width, help="Minimum path width, mm")
    g.add_argument("--pin", action=argparse.BooleanOptionalAction, default=d.pin,
                   help="Add a raised map pin with a house icon at the address")
    g.add_argument("--pin-size", type=float, default=d.pin_size, help="Pin head diameter, mm")

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
                   help="Emboss the address text on the frame")
    g.add_argument("--label", help="Custom frame text (default: the address as given)")
    g.add_argument("--text-strip", type=float, default=d.text_strip,
                   help="Height of the frame's bottom text strip, mm")
    g.add_argument("--text-height", type=float, default=d.text_height, help="Text relief height, mm")

    p.add_argument("--split", action="store_true",
                   help="Also write each layer (base, roads, paths, buildings, pin, text) as its own STL, "
                        "for multi-colour printing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    a = parse_args(argv)
    opts = Options(**{k: getattr(a, k) for k in Options.__dataclass_fields__})
    if not opts.frame:
        opts.frame_overlap = 0.0

    t0 = time.perf_counter()
    if a.coords:
        lat, lon = (float(x) for x in a.coords.split(","))
        loc = Location(lat, lon, a.address)
    else:
        print(f"Geocoding {a.address!r} ...")
        loc = geocode(a.address)
    print(f"  -> {loc.label}  ({loc.lat:.6f}, {loc.lon:.6f})")
    print(f"Downloading OpenStreetMap data, {describe_scale(opts)} ...")
    feats = fetch_features(loc, opts.radius)
    print(f"  {len(feats.roads)} roads, {len(feats.paths)} paths, {len(feats.buildings)} buildings, "
          f"{len(feats.water)} water areas")

    name = a.name or slugify(a.address)
    out: Path = a.out_dir
    written = []

    print("Building map tile ...")
    map_parts = build_map(feats, opts)
    written.append(_save(merged(map_parts), out / f"{name}-map.stl"))
    if a.split:
        written += [_save(m, out / f"{name}-map-{k}.stl") for k, m in map_parts.items()]

    if opts.frame:
        print("Building frame ...")
        frame_parts = build_frame(opts, a.label or a.address)
        written.append(_save(merged(frame_parts), out / f"{name}-frame.stl"))
        if a.split and "text" in frame_parts:
            written += [_save(m, out / f"{name}-frame-{k}.stl") for k, m in frame_parts.items()]

    print(f"Done in {time.perf_counter() - t0:.1f}s:")
    for path, ntri in written:
        print(f"  {path}  ({ntri:,} triangles)")


def _save(mesh, path: Path) -> tuple[Path, int]:
    return path, write_stl(mesh, path, name=path.stem)
