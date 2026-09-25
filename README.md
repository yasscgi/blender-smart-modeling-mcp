# Blender Smart Modeling MCP

A token-efficient MCP focused on **actual Blender modeling**, not giant Python prompts.

## Design goals

- **Compact state**: names, dimensions, counts and modifier types by default; never dump vertices unless explicitly needed.
- **Semantic geometry**: profiles, curves and high-level modeling operations encode complex shapes with tens of numbers instead of thousands of vertices.
- **Batching**: many Blender edits happen in one MCP call.
- **Checkpoints**: risky stages can be undone.
- **Validation**: compact manifold/boundary/loose-geometry feedback.
- **Visual feedback on demand**: previews are opt-in rather than generated after every edit.

## Architecture

AI client → MCP server (stdio) → length-prefixed localhost socket → Blender add-on → bpy/bmesh.

The Blender API is executed on Blender's main thread. The socket listener stays off the UI thread.

## Install

Requires Blender 4.2+ and Python 3.10+.

1. Install this repository:

    pip install -e .

   Or with uv:

    uvx --from . blender-smart-mcp

2. In Blender: Preferences → Add-ons → Install, select `addon.py`, enable **Smart Modeling MCP**.
3. In the 3D View press N → **Smart MCP** → Start.
4. Configure an MCP client:

    {
      "mcpServers": {
        "smart-blender": {
          "command": "blender-smart-mcp"
        }
      }
    }

## V1 tools

- `scene_state`
- `create_primitive`
- `model_batch`
- `lathe_profile`
- `curve_tube`
- `mesh_validate`
- `checkpoint`
- `viewport_snapshot`

### Token-efficient example

Instead of transferring a 50k-vertex bottle, send a 12-point silhouette to `lathe_profile`, then one `model_batch` call for bevel/solidify/smoothing. Query `scene_state` only for compact deltas.

## Next modeling layer

V1 establishes the low-token transport and geometry kernel. Planned tools: semantic face regions, loop/ring editing, controlled extrude/inset, parametric booleans, symmetry/radial patterns, reference-camera calibration, image silhouette fitting, local topology patches, and mesh-delta hashes.
