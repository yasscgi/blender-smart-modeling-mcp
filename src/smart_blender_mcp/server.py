from __future__ import annotations
import json, socket
from typing import Any
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Smart Blender Modeling")
HOST, PORT = "127.0.0.1", 9877

def call(action: str, **params: Any) -> dict:
    payload = json.dumps({"action": action, "params": params}, separators=(",", ":")).encode()
    with socket.create_connection((HOST, PORT), timeout=30) as s:
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        n = int.from_bytes(_recv(s, 4), "big")
        return json.loads(_recv(s, n))

def _recv(s: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = s.recv(n-len(out))
        if not chunk: raise ConnectionError("Blender connection closed")
        out.extend(chunk)
    return bytes(out)

@mcp.tool()
def scene_state(detail: str = "compact") -> dict:
    """Compact scene state. Use detail='full' only when necessary."""
    return call("scene_state", detail=detail)

@mcp.tool()
def create_primitive(kind: str, name: str = "", location: list[float] | None = None,
                     scale: list[float] | None = None, params: dict | None = None) -> dict:
    """Create cube/cylinder/sphere/cone/torus/plane with compact parameters."""
    return call("create_primitive", kind=kind, name=name, location=location or [0,0,0],
                scale=scale or [1,1,1], params=params or {})

@mcp.tool()
def model_batch(ops: list[dict], checkpoint: bool = True) -> dict:
    """Execute many modeling operations in one round-trip. Preferred for token efficiency.
    Supported ops: transform, bevel, solidify, subdivide, mirror, boolean, shade_smooth,
    apply_modifier, duplicate, delete."""
    return call("model_batch", ops=ops, checkpoint=checkpoint)

@mcp.tool()
def lathe_profile(name: str, points: list[list[float]], segments: int = 64,
                  axis: str = "Z", cap: bool = True) -> dict:
    """Create detailed rotational geometry from 2D radius/height profile points."""
    return call("lathe_profile", name=name, points=points, segments=segments, axis=axis, cap=cap)

@mcp.tool()
def curve_tube(name: str, points: list[list[float]], radius: float = 0.02,
               resolution: int = 4, cyclic: bool = False) -> dict:
    """Create a smooth tube/cable/ornamental path from sparse control points."""
    return call("curve_tube", name=name, points=points, radius=radius,
                resolution=resolution, cyclic=cyclic)

@mcp.tool()
def mesh_validate(name: str = "", repair: bool = False) -> dict:
    """Return compact print/modeling health metrics; optionally repair normals/loose geometry."""
    return call("mesh_validate", name=name, repair=repair)

@mcp.tool()
def checkpoint(label: str = "") -> dict:
    """Push an undo checkpoint before risky modeling changes."""
    return call("checkpoint", label=label)

@mcp.tool()
def viewport_snapshot(width: int = 512, height: int = 512) -> dict:
    """Return path/metadata for a viewport preview. Use sparingly to reduce visual-token cost."""
    return call("viewport_snapshot", width=width, height=height)

def main() -> None:
    mcp.run()

if __name__ == "__main__":
    main()
