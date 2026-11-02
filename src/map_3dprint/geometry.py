"""2D (shapely) -> 3D (manifold3d) helpers, text outlines and STL export."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from manifold3d import CrossSection, FillRule, Manifold, OpType
from shapely import affinity
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def polygons_of(geom: BaseGeometry) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        return [p for g in geom.geoms for p in polygons_of(g)]
    return []


def extrude(geom: BaseGeometry, z0: float, height: float) -> Manifold:
    """Extrude a (multi)polygon from z0 up by `height`."""
    contours = []
    for poly in polygons_of(geom):
        if poly.area < 1e-4:
            continue
        contours.append(np.asarray(poly.exterior.coords)[:-1])
        contours.extend(np.asarray(r.coords)[:-1] for r in poly.interiors)
    if not contours or height <= 0:
        return Manifold()
    cs = CrossSection(contours, FillRule.EvenOdd)
    return Manifold.extrude(cs, height).translate((0.0, 0.0, z0))


def union(parts: list[Manifold]) -> Manifold:
    parts = [p for p in parts if not p.is_empty()]
    if not parts:
        return Manifold()
    return Manifold.batch_boolean(parts, OpType.Add)


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> Polygon:
    r = max(0.0, min(r, (x1 - x0) / 2 - 1e-3, (y1 - y0) / 2 - 1e-3))
    if r == 0:
        return box(x0, y0, x1, y1)
    return box(x0 + r, y0 + r, x1 - r, y1 - r).buffer(r, quad_segs=16)


def text_outline(text: str, bold: bool = True) -> BaseGeometry:
    """Return the glyph outlines of `text` at font size 1, as shapely geometry."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    prop = FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal")
    path = TextPath((0, 0), text, size=1.0, prop=prop)
    geom: BaseGeometry = Polygon()
    for ring in path.to_polygons(closed_only=True):
        if len(ring) < 4:
            continue
        p = Polygon(ring)
        if not p.is_valid:
            p = p.buffer(0)
        geom = geom.symmetric_difference(p)  # even-odd fill handles glyph holes
    return geom.buffer(0)


def fit_text(text: str, max_w: float, max_h: float, line_gap: float = 0.35) -> BaseGeometry:
    """Lay out text (1-2 lines, split at commas if it helps) centred on (0, 0)."""
    candidates = [[text.strip()]]
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        best = min(range(1, len(parts)),
                   key=lambda i: abs(len(", ".join(parts[:i])) - len(", ".join(parts[i:]))))
        candidates.append([", ".join(parts[:best]), ", ".join(parts[best:])])

    best_geom, best_scale = None, -1.0
    for lines in candidates:
        outlines = [text_outline(line) for line in lines]
        # nominal cap height ~0.73 at size 1; stack lines with a gap
        line_h = 1.0
        total_h = len(lines) * line_h + (len(lines) - 1) * line_gap
        widths = [g.bounds[2] - g.bounds[0] for g in outlines]
        scale = min(max_w / max(widths), max_h / total_h)
        if scale > best_scale:
            placed = []
            for i, g in enumerate(outlines):
                minx, _, maxx, _ = g.bounds
                y = (total_h / 2) - (i + 1) * line_h - i * line_gap + 0.22
                placed.append(affinity.translate(g, -(minx + maxx) / 2, y))
            best_geom, best_scale = unary_union(placed), scale
    return affinity.scale(best_geom, best_scale, best_scale, origin=(0, 0))


def write_stl(mesh: Manifold, path: Path, name: str = "map-3dprint") -> int:
    """Write a binary STL. Returns triangle count."""
    m = mesh.to_mesh()
    verts = np.asarray(m.vert_properties, dtype=np.float32)[:, :3]
    tris = np.asarray(m.tri_verts, dtype=np.int64)
    v = verts[tris]  # (n, 3, 3)
    normals = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lens, out=np.zeros_like(normals), where=lens > 0)

    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"], rec["v"] = normals, v
    header = name.encode("ascii", "replace")[:80].ljust(80, b" ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        f.write(np.uint32(len(tris)).tobytes())
        f.write(rec.tobytes())
    return len(tris)


# ----------------------------------------------------------------------------- 3MF

_3MF_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


def _mesh_xml(mesh: Manifold) -> str:
    m = mesh.to_mesh()
    verts = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(m.tri_verts, dtype=np.int64)
    vx = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f"<mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh>"


def write_3mf(pieces: dict[str, dict[str, tuple[Manifold, str]]], path: Path,
              offsets: dict[str, tuple[float, float]] | None = None) -> int:
    """Write a 3MF where every piece (e.g. map, frame) is one multi-part object.

    `pieces` maps piece name -> {part name: (mesh, "#RRGGBB")}. Each part becomes its own mesh
    object with a display colour; a component object groups the parts of a piece so slicers
    keep them aligned and let you assign a filament per part. Returns total triangle count.
    """
    import zipfile
    from xml.sax.saxutils import quoteattr

    offsets = offsets or {}
    colors: list[str] = []
    res, build, next_id, ntri = [], [], 2, 0
    for piece, parts in pieces.items():
        comp_ids = []
        for part, (mesh, color) in parts.items():
            if mesh.is_empty():
                continue
            if color not in colors:
                colors.append(color)
            res.append(f'<object id="{next_id}" type="model" name={quoteattr(f"{piece}-{part}")} '
                       f'pid="1" pindex="{colors.index(color)}">{_mesh_xml(mesh)}</object>')
            comp_ids.append(next_id)
            ntri += mesh.num_tri()
            next_id += 1
        if not comp_ids:
            continue
        comps = "".join(f'<component objectid="{i}"/>' for i in comp_ids)
        res.append(f'<object id="{next_id}" type="model" name={quoteattr(piece)}>'
                   f'<components>{comps}</components></object>')
        dx, dy = offsets.get(piece, (0.0, 0.0))
        build.append(f'<item objectid="{next_id}" transform="1 0 0 0 1 0 0 0 1 {dx:.3f} {dy:.3f} 0"/>')
        next_id += 1

    mats = "".join(f'<base name="{c}" displaycolor="{c}FF"/>' for c in colors)
    model = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_NS}">'
             f'<metadata name="Application">map-3dprint</metadata>'
             f'<resources><basematerials id="1">{mats}</basematerials>{"".join(res)}</resources>'
             f'<build>{"".join(build)}</build></model>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
    return ntri
