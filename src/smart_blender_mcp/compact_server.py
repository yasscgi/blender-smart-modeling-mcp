from __future__ import annotations

import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Smart Blender Compact")
HOST, PORT = "127.0.0.1", 9877


def _recv(s: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise ConnectionError("Blender connection closed")
        out.extend(chunk)
    return bytes(out)


def _call(action: str, **params: Any) -> dict:
    raw = json.dumps({"action": action, "params": params}, separators=(",", ":")).encode()
    with socket.create_connection((HOST, PORT), timeout=60) as s:
        s.sendall(len(raw).to_bytes(4, "big") + raw)
        size = int.from_bytes(_recv(s, 4), "big")
        return json.loads(_recv(s, size))


@mcp.tool()
def state(changed_since: str = "") -> dict:
    """Compact scene delta. Reuse scene_h as changed_since."""
    return _call("scene_state", detail="compact", changed_since=changed_since)


@mcp.tool()
def inspect(kind: str, name: str = "", selector: dict | None = None) -> dict:
    """Inspect topology, faces, or edges without dumping the mesh."""
    if kind == "topology":
        return _call("topology_state", name=name)
    if kind == "faces":
        return _call("mesh_query", name=name, selector=selector or {"region": "all"})
    if kind == "edges":
        return _call("edge_query", name=name, selector=selector or {})
    raise ValueError("kind must be topology, faces, or edges")


@mcp.tool()
def model(steps: list[dict], checkpoint: bool = True, return_steps: bool = False) -> dict:
    """Run a mixed modeling plan in one call.
    step do: create, object, mesh, topology, lathe, loft, sweep, curve, radial, validate."""
    return _call(
        "smart_batch",
        steps=steps,
        checkpoint=checkpoint,
        return_steps=return_steps,
    )


@mcp.tool()
def preview(width: int = 512, height: int = 512) -> dict:
    """Render one visual checkpoint."""
    return _call("viewport_snapshot", width=width, height=height)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
