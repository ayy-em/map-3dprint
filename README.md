# map-3dprint

Turn an address into 3D-printable city map tiles (STL), built from
[OpenStreetMap](https://www.openstreetmap.org/) data.

You give it an address and get:

| File | What it is |
|---|---|
| `<name>-map.stl` | Square map tile: base plate, raised roads, footpaths and buildings, with water recessed into the base. Optional map pin with a house icon at the address. |
| `<name>-frame.stl` | Bezel with rounded corners that lies on top of the tile's edge. The address is embossed on the wider bottom strip. |
| `<name>.3mf` | Both pieces in one file. Each layer (base, roads, paths, buildings, pin, frame, text) is a separate coloured part of its object, so you can assign a filament per part in Bambu Studio, OrcaSlicer or PrusaSlicer. |
| `<name>-map-*.stl`, `<name>-frame-*.stl` | With `--split`: each layer as its own STL. They share a coordinate system, so load them as parts of one object. |

STL is just a bag of triangles. It has no colours, parts or modifiers, so a flush (inlaid) text only shows up as a second colour in 3MF or as split STLs.

See `example/` for reference output.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync
```

## Usage

```sh
uv run map-3dprint "Calle de Toledo 138, Madrid"
uv run map-3dprint "Calle de Toledo 138, Madrid" --pin --text-style flush
uv run map-3dprint "10 Downing St, London" --radius 400 --border-radius 3 --label "Home"
uv run map-3dprint "Somewhere" --coords 40.4043,-3.7132 --no-text
```

Files are written to `./output/` unless you pass `-o`. To list every option, run `uv run map-3dprint --help`.

Main options:

| Option | Default | Meaning |
|---|---|---|
| `--size` | 200 | Tile side length, mm |
| `--radius` | 600 | Real-world distance from the centre to the tile edge, metres (sets the map scale) |
| `--pin / --no-pin` | off | Raised map pin with a house cut-out, tip on the address |
| `--text / --no-text` | on | Put the address on the frame |
| `--text-style` | emboss | `emboss` gives raised letters, `deboss` carves them in, and `flush` makes an inlay part level with the surface (meant for a second colour) |
| `--text-height` | 0.8 | Relief height (emboss) or depth (deboss/flush), mm |
| `--label` | the address | Custom frame text. Long text wraps onto two lines at a comma. |
| `--border-radius` | 6 | Corner radius of the frame. With `--no-frame` it rounds the tile instead. |
| `--frame / --no-frame` | on | Generate the frame |
| `--frame-overlap` | 3 | How far the frame covers the tile edge, mm. The tile is left flat under it. |
| `--base-height`, `--road-height`, `--path-height`, `--building-height` | 1.2 / 1.0 / 0.6 / 2.4 | Layer heights, mm |
| `--format` | both | `3mf`, `stl` or `both` |
| `--split` | off | Also export the layers as separate STLs |

## How it works

1. **Nominatim** geocodes the address.
2. **Overpass API** downloads roads, footpaths, buildings and water inside the bounding box.
3. **shapely** projects the features to millimetres, buffers roads to their real widths (with a printable minimum), merges and clips them.
4. **manifold3d** extrudes and merges the layers into watertight meshes. The STL and 3MF files are written straight from numpy, with no extra dependencies.

API responses are cached in `~/.cache/map-3dprint/`, so a second run for the same address and radius doesn't need the network. Please respect the
[Nominatim](https://operations.osmfoundation.org/policies/nominatim/) and Overpass usage policies.

Map data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
