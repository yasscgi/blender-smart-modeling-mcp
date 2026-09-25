# Blender Smart Modeling MCP

A token-efficient MCP focused on **real Blender modeling**, not giant generated Python scripts.

## Why it is different

The AI does not receive the whole mesh. It works with compact semantic instructions such as:

- "top 15% of the object"
- "faces pointing mostly +Z"
- "middle 30% of the height"
- "extrude this region by 8 mm"
- "inset then scale this region"
- "bisect the mesh at this plane"

That means complex modeling can be driven with tens of parameters instead of thousands of vertex coordinates.

## Architecture

AI client → MCP server (stdio) → localhost socket → Blender add-on → bpy/bmesh

The socket listener runs outside Blender's UI thread, while Blender API work is executed safely on the main thread.

## V0.2 modeling tools

### Scene / state
- `scene_state` — compact object summaries + geometry hashes
- `checkpoint` — undo checkpoint
- `viewport_snapshot` — visual verification only when needed

### Creation
- `create_primitive`
- `lathe_profile` — detailed rotational forms from a sparse profile\n- `loft_sections` — complex non-rotational bodies from sparse cross-sections
- `curve_tube` — cables, handles, ornaments and paths

### Object-level modeling
- `model_batch`
  - transform
  - bevel
  - solidify
  - subdivide
  - mirror
  - boolean
  - boolean_primitive
  - shade_smooth
  - apply_modifier
  - duplicate
  - delete

### Direct mesh modeling
- `mesh_query` — inspect a semantic region without dumping geometry
- `mesh_edit_batch`
  - extrude
  - inset
  - translate region
  - scale region
  - delete faces
  - subdivide region
  - bisect / knife-like planar cut
  - merge by distance
  - recalculate normals

### Validation
- `mesh_validate` — manifold, boundary and loose geometry checks

## Semantic selectors

Selectors are resolved inside Blender.

```json
{"region":"top","band":0.15}
```

```json
{"bbox":{"x":[0.2,0.8],"y":[0.0,1.0],"z":[0.65,1.0]}}
```

```json
{"normal":{"axis":"Z","min_dot":0.8}}
```

Selectors can be combined:

```json
{
  "all":[
    {"region":"top","band":0.25},
    {"normal":{"axis":"Z","min_dot":0.65}}
  ]
}
```

## Sparse loft example

A non-circular product body can be represented by a few cross-sections instead of thousands of vertices:

```json
{
  "name":"Housing",
  "sections":[
    {"z":0.0,"points":[[-1,-0.7],[1,-0.7],[1,0.7],[-1,0.7]]},
    {"z":1.2,"points":[[-1.1,-0.8],[1.1,-0.8],[1.1,0.8],[-1.1,0.8]]},
    {"z":2.4,"points":[[-0.75,-0.55],[0.75,-0.55],[0.75,0.55],[-0.75,0.55]]}
  ],
  "cap":true
}
```

Blender generates the connecting topology locally.

## Low-token modeling example

Create a product body, then shape the upper region in one topology batch:

```json
{
  "name":"Body",
  "ops":[
    {
      "op":"inset",
      "selector":{"region":"top","band":0.12},
      "thickness":0.06
    },
    {
      "op":"extrude",
      "selector":{"region":"top","band":0.12},
      "distance":0.25
    },
    {
      "op":"scale",
      "selector":{"region":"top","band":0.18},
      "factor":[0.72,0.72,1.0]
    },
    {
      "op":"recalc_normals"
    }
  ]
}
```

The client sends the semantic recipe. Blender resolves the faces locally.

## Change hashes

Every object has a compact `h` digest and the scene has `scene_h`.
Pass a previous digest to `scene_state(changed_since=...)`. If nothing changed, the server returns only:

```json
{"ok":true,"changed":false,"scene_h":"..."}
```

This avoids repeatedly paying tokens for identical scene descriptions.

## V0.3 — Compact Advanced Modeling

For the lowest token overhead, use the **compact server**:

```bash
uvx --from . blender-smart-mcp-compact
```

It exposes only four MCP tools:

- `state` — compact scene delta/hash
- `inspect` — topology, face-region or edge-region intelligence
- `model` — mixed modeling plan in one transactional call
- `preview` — visual checkpoint only when needed

The full server remains available as `blender-smart-mcp` for debugging or direct access to individual tools.

### One-call mixed modeling

`model()` accepts a list of compact steps. Supported `do` values:

- `create`
- `object`
- `mesh`
- `topology`
- `lathe`
- `loft`
- `sweep`
- `curve`
- `radial`
- `validate`

Example:

```json
{
  "steps":[
    {
      "do":"create",
      "kind":"cube",
      "name":"Body",
      "scale":[1.0,0.75,1.5]
    },
    {
      "do":"mesh",
      "name":"Body",
      "ops":[
        {
          "op":"inset",
          "selector":{"region":"top","band":0.12},
          "thickness":0.08
        },
        {
          "op":"extrude",
          "selector":{"region":"top","band":0.12},
          "distance":0.35
        }
      ]
    },
    {
      "do":"topology",
      "name":"Body",
      "ops":[
        {
          "op":"bevel_edges",
          "selector":{
            "orientation":{"axis":"Z","min_dot":0.9}
          },
          "width":0.04,
          "segments":3
        }
      ]
    },
    {
      "do":"validate",
      "name":"Body"
    }
  ],
  "checkpoint":true,
  "rollback_on_error":true
}
```

By default only the final compact result is returned. Set `return_steps=true` only when debugging.

### Edge loops and rings without ID dumps

Edge selectors can choose a tiny seed and expand it locally:

```json
{
  "nearest":[0.0,0.75,1.2],
  "orientation":{"axis":"X","min_dot":0.6},
  "expand":"ring"
}
```

The same selector can be used by `edge_query` or topology operations. This avoids sending long edge-ID arrays.

### Advanced topology

V0.3 adds:

- compact topology state: tris/quads/ngons, manifold state, poles and boundary loops
- local edge bevel/support edges
- edge subdivision/dissolve/collapse
- semantic planar loop cuts
- boundary-loop bridging
- seed-to-ring and seed-to-loop expansion
- profile sweep for rails, trims, frames and handles
- radial arrays for repeated details

### Transaction safety

The compact `model` tool can place one Blender undo checkpoint for the entire plan. With `rollback_on_error=true`, a failed step attempts to restore the scene to the state before the batch.

## V0.4 — Engineering 2D Plan → 3D

V0.4 adds a multi-part orthographic reconstruction pipeline intended for AI-generated engineering reference sheets.

**Pipeline**

`master reference sheet → Blueprint Manifest → validation → Front/Side/Top visual hull → engineering features → quality report → topology refinement`

The reference image can contain many parts (P01, P02, ...). The vision model reads it once and emits one compact manifest for all parts.

### Compact usage

Validate the whole plan without opening more MCP tools:

```python
inspect(kind="blueprint", selector=manifest)
```

Then reconstruct all parts in one modeling transaction:

```json
{
  "steps": [
    {
      "do": "orthographic",
      "spec": "<Blueprint Manifest>",
      "bevel_mm": 0.6,
      "cleanup": true,
      "collection_name": "My_Model"
    }
  ]
}
```

V0.4 reconstructs each part by intersecting silhouette prisms from the canonical Front, Side and Top plans. It then applies explicit details such as cylindrical holes, cylindrical bosses, box cuts and box bosses.

Each reconstructed part returns:

- views actually used,
- engineering features applied,
- final dimensions,
- dimension error percentage,
- boundary-edge count,
- non-manifold-edge count,
- compact geometry hash.

Use `do:"cameras"` to create six orthographic engineering cameras for Front/Back/Left/Right/Top/Bottom review.

See `docs/REFERENCE_BLUEPRINT.md` and `examples/engineering_blueprint.json`.

## V0.4.1 — Engineer loop with almost no repeated context

The compact server keeps the Blueprint Manifest in memory.

1. Send the master manifest once:

```text
inspect(kind="blueprint", selector=<manifest>)
→ blueprint_id
```

2. Reconstruct all parts without resending the plan:

```json
{
  "steps": [
    {
      "do": "orthographic",
      "blueprint_id": "<id>",
      "collection_name": "Product"
    }
  ]
}
```

3. Score one part directly against its cached Front/Side/Top plans:

```text
inspect(
  kind="fit",
  name="P07",
  selector={"blueprint_id":"<id>","resolution":64}
)
```

The fit result is a compact IoU score per view instead of another image.

4. Patch only the failing part:

```json
{
  "blueprint_id": "<id>",
  "part_id": "P07",
  "changes": {
    "dimensions_mm": {"depth": 42},
    "views": {"side": [[0.1,0.0],[0.9,0.0],[0.8,1.0],[0.2,1.0]]}
  }
}
```

Call this through `inspect(kind="blueprint_patch", selector=...)`. It returns a new `blueprint_id`.

5. Rebuild only P07:

```json
{
  "steps": [
    {
      "do": "orthographic",
      "blueprint_id": "<new-id>",
      "part_ids": ["P07"],
      "replace_existing": true
    }
  ]
}
```

### Per-part engineering strategies

A single blueprint may mix:

- `orthographic_hull` — Front/Side/Top silhouette intersection
- `lathe` — rotational product parts from radius/Z profiles
- `loft` — shape through multiple cross-sections
- `sweep` — handles, rails, trims and tubing from path + profile

The AI should choose the simplest strategy that preserves the design intent.

### Repeated detail without repeated tokens

Engineering features support local patterns. Example: one hole definition can create a full bolt circle:

```json
{
  "type":"hole_cylinder",
  "axis":"Z",
  "center_mm":[32,0,0],
  "diameter_mm":4,
  "depth_mm":10,
  "pattern":{
    "type":"radial",
    "count":12,
    "axis":"Z",
    "center_mm":[0,0,0],
    "angle_degrees":360
  }
}
```

Linear patterns use `step_mm`.

### One-image visual verification

`preview(mode="engineering")` renders Front/Back/Left/Right/Top/Bottom into a single contact sheet. Use it only after numeric dimension/topology/fit checks need visual confirmation.

## Install

Requires Blender 4.2+ and Python 3.10+.

1. Install the Python package:

```bash
pip install -e .
```

or with uv:

```bash
uvx --from . blender-smart-mcp
```

2. Blender → Preferences → Add-ons → Install → select `addon.py`.
3. Enable **Smart Modeling MCP**.
4. In 3D View press `N` → **Smart MCP** → **Start / Stop**.
5. Configure your MCP client to run:

```json
{
  "mcpServers": {
    "smart-blender": {
      "command": "blender-smart-mcp-compact"
    }
  }
}
```

## Token strategy

1. Query `scene_state` once.
2. Use semantic selectors rather than IDs.
3. Send related edits in one `mesh_edit_batch`.
4. Use geometry hashes to avoid repeated state.
5. Request face IDs only for an exceptional local correction.
6. Render a snapshot only at meaningful visual checkpoints.
7. Validate topology after major stages, not every operation.

## Next layer

The next engineering layer will focus on automated silhouette comparison, local error heatmaps, constraint-driven refinement, sockets/pegs from assembly links, and reference-driven detail passes without expanding the compact MCP surface.
