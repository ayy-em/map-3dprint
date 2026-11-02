"""Top-down PNG preview of the generated parts, coloured like the 3MF."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from manifold3d import Manifold


def _top_faces(mesh: Manifold) -> tuple[np.ndarray, np.ndarray]:
    m = mesh.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3][np.asarray(m.tri_verts)]
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    up = n[:, 2] > 1e-9
    return v[up][:, :, :2], v[up][:, :, 2].mean(axis=1)


def render_preview(pieces: dict[str, dict[str, tuple[Manifold, str]]], path: Path,
                   offsets: dict[str, tuple[float, float]] | None = None) -> Path:
    """Draw every upward-facing triangle, lowest first, shaded lighter with height."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.colors import to_rgb

    offsets = offsets or {}
    polys, zs, cols = [], [], []
    for piece, parts in pieces.items():
        dx, dy = offsets.get(piece, (0.0, 0.0))
        for mesh, color in parts.values():
            if mesh.is_empty():
                continue
            p, z = _top_faces(mesh)
            polys.append(p + (dx, dy))
            zs.append(z)
            cols.append(np.tile(to_rgb(color), (len(z), 1)))
    if not polys:
        return path
    P, Z, C = np.concatenate(polys), np.concatenate(zs), np.concatenate(cols)
    order = np.argsort(Z, kind="stable")
    shade = 0.55 + 0.45 * (Z / max(Z.max(), 1e-9))
    C = np.clip(C * shade[:, None], 0, 1)

    pts = P.reshape(-1, 2)
    (x0, y0), (x1, y1) = pts.min(0), pts.max(0)
    w, h = x1 - x0, y1 - y0
    fig = plt.figure(figsize=(12, 12 * h / w), facecolor="#F4F1EA")
    ax = fig.add_axes((0.02, 0.02, 0.96, 0.96))
    ax.add_collection(PolyCollection(P[order], facecolors=C[order], edgecolors=C[order], linewidths=0.2))
    ax.set_xlim(x0 - 2, x1 + 2)
    ax.set_ylim(y0 - 2, y1 + 2)
    ax.set_aspect("equal")
    ax.axis("off")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path
