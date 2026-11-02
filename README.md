# map-3dprint

Turn an address into 3D-printable city map tiles (STL and 3MF), built from
[OpenStreetMap](https://www.openstreetmap.org/) data.

You give it an address and get:

| File | What it is |
|---|---|
| `<name>-map.stl` | Map tile: base plate with raised roads, footpaths, parks, railways and buildings, and water recessed into the base. Optional pins, route and hanging cavities. |
| `<name>-frame.stl` | Bezel that lies on top of the tile's edge. The bottom strip carries the address and, optionally, coordinates, a compass and a scale bar. |
| `<name>-stand.stl` | With `--mount stand`: a desk stand that holds the frame's bottom edge. |
| `<name>.3mf` | Every piece in one file. Each layer is a separate coloured part of its object, so you can assign a filament per part in Bambu Studio, OrcaSlicer or PrusaSlicer. |
| `<name>-preview.png` | Coloured top-down render of the assembled result, so you can check it before slicing. |
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
uv run map-3dprint "Calle de Toledo 138, Madrid" --pin --text-style flush --building-heights real
uv run map-3dprint "10 Downing St, London" --radius 400 --shape circle --compass --scale-bar
uv run map-3dprint "Home address" --pin --add-pin "Where we met" --add-pin "First flat" --route walk
uv run map-3dprint "Somewhere" --coords 40.4043,-3.7132 --rotate 30 --mount keyhole
```

Files are written to `./output/` unless you pass `-o`. To list every option, run `uv run map-3dprint --help`.

### Map

| Option | Default | Meaning |
|---|---|---|
| `--size` | 200 | Tile size (width of the bounding box), mm |
| `--radius` | 600 | Real-world distance from the centre to the tile edge, metres (sets the map scale) |
| `--shape` | square | `square`, `circle`, `hexagon` or `heart`. The frame follows the shape; non-square frames get a text plaque underneath. |
| `--rotate` | 0 | Turn the map counter-clockwise by this many degrees. The compass follows. |
| `--building-heights` | flat | `flat` makes every building `--building-height` tall. `real` uses OSM `height` or `building:levels` at map scale, and untagged buildings get the local median. |
| `--height-scale`, `--max-building-height` | 1.0 / 8 | Vertical exaggeration and height cap for `real` |
| `--parks / --no-parks` | on | Thin raised layer for parks, gardens, woods and grass |
| `--railways / --no-railways` | on | Above-ground railway lines |
| `--base-height`, `--road-height`, `--path-height`, `--building-height`, `--park-height`, `--rail-height` | 1.2 / 1.0 / 0.6 / 2.4 / 0.4 / 0.8 | Layer heights, mm |

### Pins and routes

| Option | Default | Meaning |
|---|---|---|
| `--pin / --no-pin` | off | Raised pin with a house cut-out, tip on the address |
| `--add-pin ADDRESS` | | Extra pin with a dot cut-out, given as an address or `lat,lon`. Repeat it for more stops. Stops outside the tile are skipped with a warning. |
| `--route` | none | `straight`, `walk`, `bike` or `drive`. Draws a raised line from the main address through the stops in order, using routing from [routing.openstreetmap.de](https://routing.openstreetmap.de). |

### Frame

| Option | Default | Meaning |
|---|---|---|
| `--frame / --no-frame` | on | Generate the frame |
| `--border-radius` | 6 | Corner radius of the frame. With `--no-frame` it rounds the tile instead. |
| `--frame-overlap` | 3 | How far the frame covers the tile edge, mm. The tile is left flat under it. |
| `--text / --no-text` | on | Put the address on the frame |
| `--label` | the address | Custom frame text. Long text wraps onto two lines at a comma. |
| `--text-style` | emboss | `emboss` gives raised letters, `deboss` carves them in, and `flush` makes an inlay part level with the surface (meant for a second colour) |
| `--text-height` | 0.8 | Relief height (emboss) or depth (deboss/flush), mm |
| `--coordinates` | off | Second line with the latitude and longitude |
| `--compass`, `--scale-bar` | off | Compass rose on the left of the strip, and a scale bar with a round distance on the right |

### Mounting

| Option | Meaning |
|---|---|
| `--mount keyhole` | Two keyhole slots in the back of the tile for hanging on screws. The base is thickened to 4 mm. |
| `--mount magnets` | Up to four pockets in the back of the tile for `--magnet-diameter` × `--magnet-thickness` magnets (default 10 × 2 mm). The base is thickened to fit them. |
| `--mount stand` | A separate desk stand with a slot that holds the frame's bottom edge at a 15° lean |

### Output

| Option | Default | Meaning |
|---|---|---|
| `--format` | both | `3mf`, `stl` or `both` |
| `--preview / --no-preview` | on | PNG preview |
| `--split` | off | Also export the layers as separate STLs |

## How it works

1. **Nominatim** geocodes the address.
2. **Overpass API** downloads roads, footpaths, buildings and water inside the bounding box. A second, optional query fetches parks and railways, and the tool still runs if that query fails.
3. **shapely** projects the features to millimetres, buffers roads to their real widths (with a printable minimum), merges, rotates and clips them to the tile shape.
4. **manifold3d** extrudes and merges the layers into watertight meshes. The STL and 3MF files are written straight from numpy, with no extra dependencies.

API responses are cached in `~/.cache/map-3dprint/`, so a second run for the same address and radius doesn't need the network. Please respect the
[Nominatim](https://operations.osmfoundation.org/policies/nominatim/), Overpass and routing service usage policies.

Map data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
