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
- `lathe_profile` — detailed rotational forms from a sparse profile
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
      "command": "blender-smart-mcp"
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

The next versions can add edge-loop recognition, bridge loops, profile sweep, radial details, support-loop insertion, reference-camera calibration, silhouette fitting and local topology patches while keeping the same compact protocol.
