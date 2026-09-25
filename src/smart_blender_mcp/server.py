from __future__ import annotations

import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP
from .blueprint import validate_blueprint_spec

mcp = FastMCP("Smart Blender Modeling")
HOST, PORT = "127.0.0.1", 9877


def _recv(s: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise ConnectionError("Blender connection closed")
        out.extend(chunk)
    return bytes(out)


def call(action: str, **params: Any) -> dict:
    payload = json.dumps({"action": action, "params": params}, separators=(",", ":")).encode()
    with socket.create_connection((HOST, PORT), timeout=300) as s:
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        n = int.from_bytes(_recv(s, 4), "big")
        return json.loads(_recv(s, n))


@mcp.tool()
def scene_state(detail: str = "compact", changed_since: str = "") -> dict:
    """Return a token-small scene summary and geometry digests.
    detail='compact' is preferred. changed_since may be a previous scene digest."""
    return call("scene_state", detail=detail, changed_since=changed_since)


@mcp.tool()
def create_primitive(
    kind: str,
    name: str = "",
    location: list[float] | None = None,
    scale: list[float] | None = None,
    params: dict | None = None,
) -> dict:
    """Create cube/cylinder/sphere/cone/torus/plane with compact parameters."""
    return call(
        "create_primitive",
        kind=kind,
        name=name,
        location=location or [0, 0, 0],
        scale=scale or [1, 1, 1],
        params=params or {},
    )


@mcp.tool()
def model_batch(ops: list[dict], checkpoint: bool = True) -> dict:
    """Execute object-level operations in one round-trip.
    Ops: transform, bevel, solidify, subdivide, mirror, boolean,
    boolean_primitive, shade_smooth, apply_modifier, duplicate, delete.
    Prefer one batch to many calls."""
    return call("model_batch", ops=ops, checkpoint=checkpoint)


@mcp.tool()
def mesh_query(
    name: str = "",
    selector: dict | None = None,
    include_ids: bool = False,
    max_ids: int = 64,
) -> dict:
    """Inspect only a semantic mesh region, not the whole mesh.

    Selectors are composable and normalized to the object's local bounding box:
      {"region":"top|bottom|left|right|front|back|all","band":0.15}
      {"bbox":{"x":[0.2,0.8],"y":[0,1],"z":[0.7,1]}}
      {"normal":{"axis":"Z","min_dot":0.75}}
      {"face_ids":[1,2,3]}
      {"all":[selectorA, selectorB]}  # intersection
      {"any":[selectorA, selectorB]}  # union

    IDs are omitted by default to save tokens. Ask for them only when necessary."""
    return call(
        "mesh_query",
        name=name,
        selector=selector or {"region": "all"},
        include_ids=include_ids,
        max_ids=max_ids,
    )


@mcp.tool()
def mesh_edit_batch(name: str, ops: list[dict], checkpoint: bool = True) -> dict:
    """Perform direct topology/modeling edits using semantic selectors.

    Supported ops:
      extrude {selector,distance|vector}
      inset {selector,thickness,depth}
      translate {selector,vector}
      scale {selector,factor,pivot?}
      delete_faces {selector}
      subdivide {selector,cuts,smooth}
      bisect {plane_co,plane_no,clear_inner?,clear_outer?}
      recalc_normals
      merge_by_distance {distance}

    A selector is resolved inside Blender at execution time, so face/vertex arrays
    never need to be sent through MCP."""
    return call("mesh_edit_batch", name=name, ops=ops, checkpoint=checkpoint)


@mcp.tool()
def edge_query(
    name: str = "",
    selector: dict | None = None,
    include_ids: bool = False,
    max_ids: int = 64,
) -> dict:
    """Inspect a compact semantic edge region.

    Edge selectors can use:
      {"boundary":true}
      {"bbox":{"x":[0,1],"y":[0,1],"z":[0.8,1]}}
      {"orientation":{"axis":"Z","min_dot":0.8}}
      {"edge_ids":[1,2,3]}
      {"nearest":[x,y,z]} to collapse a broad selector to one seed edge
      {"expand":"ring|loop"} to grow that seed locally inside Blender
      {"all":[...]} or {"any":[...]}

    IDs are omitted by default to keep responses small."""
    return call(
        "edge_query",
        name=name,
        selector=selector or {},
        include_ids=include_ids,
        max_ids=max_ids,
    )


@mcp.tool()
def topology_state(name: str = "") -> dict:
    """Return compact topology intelligence without vertex dumps:
    tris/quads/ngons, boundary-loop summaries, pole counts and manifold counts."""
    return call("topology_state", name=name)


@mcp.tool()
def topology_batch(name: str, ops: list[dict], checkpoint: bool = True) -> dict:
    """Advanced direct-topology edits with local semantic edge selection.

    Supported ops:
      bevel_edges {selector,width,segments,profile?}
      subdivide_edges {selector,cuts,smooth?}
      loop_cut {axis:"X|Y|Z",position:0..1}
      bridge_boundaries {first:{...},second:{...},twist?}
      collapse_edges {selector}
      dissolve_edges {selector,use_verts?}

    Prefer batching related topology changes into one call."""
    return call("topology_batch", name=name, ops=ops, checkpoint=checkpoint)


@mcp.tool()
def sweep_profile(
    name: str,
    path: list[list[float]],
    profile: list[list[float]],
    closed_path: bool = False,
    closed_profile: bool = True,
    cap: bool = True,
    up: list[float] | None = None,
) -> dict:
    """Sweep a sparse 2D profile along a 3D path.
    Useful for trims, rails, frames, handles and complex hard-surface details."""
    return call(
        "sweep_profile",
        name=name,
        path=path,
        profile=profile,
        closed_path=closed_path,
        closed_profile=closed_profile,
        cap=cap,
        up=up or [0, 0, 1],
    )


@mcp.tool()
def radial_array(
    name: str,
    count: int,
    axis: str = "Z",
    angle_degrees: float = 360.0,
    center: list[float] | None = None,
    linked: bool = True,
) -> dict:
    """Create repeated radial detail from one object in a single compact call."""
    return call(
        "radial_array",
        name=name,
        count=count,
        axis=axis,
        angle_degrees=angle_degrees,
        center=center or [0, 0, 0],
        linked=linked,
    )


@mcp.tool()
def lathe_profile(
    name: str,
    points: list[list[float]],
    segments: int = 64,
    axis: str = "Z",
    cap: bool = True,
) -> dict:
    """Create detailed rotational geometry from a sparse 2D [radius,height] profile."""
    return call("lathe_profile", name=name, points=points, segments=segments, axis=axis, cap=cap)


@mcp.tool()
def loft_sections(name: str, sections: list[dict], cap: bool = True) -> dict:
    """Build a complex non-rotational body from sparse 2D cross-sections.
    Each section: {"z": number, "points": [[x,y], ...]}.
    All sections must use the same point count/order."""
    return call("loft_sections", name=name, sections=sections, cap=cap)


@mcp.tool()
def curve_tube(
    name: str,
    points: list[list[float]],
    radius: float = 0.02,
    resolution: int = 4,
    cyclic: bool = False,
) -> dict:
    """Create a smooth tube/cable/ornament from sparse control points."""
    return call(
        "curve_tube",
        name=name,
        points=points,
        radius=radius,
        resolution=resolution,
        cyclic=cyclic,
    )


@mcp.tool()
def blueprint_validate(spec: dict) -> dict:
    """Validate a multi-part orthographic engineering manifest before modeling."""
    return validate_blueprint_spec(spec)


@mcp.tool()
def reconstruct_blueprint(
    spec: dict,
    bevel_mm: float = 0.0,
    cleanup: bool = True,
    collection_name: str = "Blueprint_Reconstruction",
) -> dict:
    """Reconstruct all parts from front/side/top 2D silhouette plans.
    Blender creates orthographic silhouette prisms and intersects them into a 3D visual hull."""
    report = validate_blueprint_spec(spec)
    if not report["ok"]:
        return report
    return call(
        "reconstruct_blueprint",
        spec=spec,
        bevel_mm=bevel_mm,
        cleanup=cleanup,
        collection_name=collection_name,
    )


@mcp.tool()
def setup_engineering_cameras(
    dimensions_mm: dict,
    center_mm: list[float] | None = None,
    margin: float = 1.15,
) -> dict:
    """Create orthographic Front/Back/Left/Right/Top/Bottom cameras for engineering review."""
    return call(
        "setup_engineering_cameras",
        dimensions_mm=dimensions_mm,
        center_mm=center_mm or [0, 0, 0],
        margin=margin,
    )


@mcp.tool()
def silhouette_fit(
    part: dict,
    coordinate_mode: str = "normalized",
    part_id: str = "",
    resolution: int = 64,
) -> dict:
    """Compare the actual mesh projection with Front/Side/Top plan silhouettes using low-token IoU scores."""
    return call(
        "silhouette_fit",
        part=part,
        coordinate_mode=coordinate_mode,
        part_id=part_id or str(part.get("id", "")),
        resolution=max(24, min(resolution, 160)),
    )


@mcp.tool()
def mesh_validate(name: str = "", repair: bool = False) -> dict:
    """Return compact manifold/boundary/loose-geometry metrics; optionally repair safe issues."""
    return call("mesh_validate", name=name, repair=repair)


@mcp.tool()
def checkpoint(label: str = "") -> dict:
    """Push an undo checkpoint before a risky modeling stage."""
    return call("checkpoint", label=label)


@mcp.tool()
def engineering_contact_sheet(
    size: int = 384,
    part_ids: list[str] | None = None,
) -> dict:
    """Render six orthographic engineering views into one contact-sheet image."""
    return call(
        "engineering_contact_sheet",
        size=max(128, min(size, 1024)),
        part_ids=part_ids or [],
    )


@mcp.tool()
def viewport_snapshot(width: int = 512, height: int = 512) -> dict:
    """Render a preview only when visual verification is needed."""
    return call("viewport_snapshot", width=width, height=height)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
