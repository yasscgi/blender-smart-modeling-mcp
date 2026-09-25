from __future__ import annotations

import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP
from .blueprint import validate_blueprint_spec, blueprint_digest

mcp = FastMCP("Smart Blender Compact")
HOST, PORT = "127.0.0.1", 9877
_BLUEPRINT_CACHE: dict[str, dict] = {}


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
    with socket.create_connection((HOST, PORT), timeout=300) as s:
        s.sendall(len(raw).to_bytes(4, "big") + raw)
        size = int.from_bytes(_recv(s, 4), "big")
        return json.loads(_recv(s, size))


@mcp.tool()
def state(changed_since: str = "") -> dict:
    """Compact scene delta. Reuse scene_h as changed_since."""
    return _call("scene_state", detail="compact", changed_since=changed_since)


@mcp.tool()
def inspect(kind: str, name: str = "", selector: dict | None = None) -> dict:
    """Compact inspection: topology/faces/edges, cache or patch a blueprint, or score silhouette fit."""
    if kind == "topology":
        return _call("topology_state", name=name)
    if kind == "faces":
        return _call("mesh_query", name=name, selector=selector or {"region": "all"})
    if kind == "edges":
        return _call("edge_query", name=name, selector=selector or {})
    if kind == "blueprint":
        if not isinstance(selector, dict):
            raise ValueError("selector must contain the blueprint manifest")
        report = validate_blueprint_spec(selector)
        if report.get("ok"):
            bid = blueprint_digest(selector)
            _BLUEPRINT_CACHE[bid] = selector
            report["blueprint_id"] = bid
            report["cached"] = True
        return report

    if kind == "fit":
        if not isinstance(selector, dict):
            raise ValueError("fit selector requires blueprint_id")
        bid = str(selector.get("blueprint_id", ""))
        spec = _BLUEPRINT_CACHE.get(bid)
        if spec is None:
            raise ValueError("Unknown blueprint_id")
        part_id = str(selector.get("part_id") or name)
        part = next((p for p in spec.get("parts", []) if str(p.get("id")) == part_id), None)
        if part is None:
            raise ValueError("Part not found in cached blueprint: " + part_id)
        return _call(
            "silhouette_fit",
            part=part,
            coordinate_mode=spec.get("coordinate_mode", "normalized"),
            part_id=part_id,
            resolution=max(24, min(int(selector.get("resolution", 64)), 160)),
        )

    if kind == "blueprint_patch":
        if not isinstance(selector, dict):
            raise ValueError("selector must contain blueprint_id, part_id and changes")
        bid = str(selector.get("blueprint_id", ""))
        base = _BLUEPRINT_CACHE.get(bid)
        if base is None:
            raise ValueError("Unknown blueprint_id")
        part_id = str(selector.get("part_id", ""))
        changes = selector.get("changes", {})
        if not part_id or not isinstance(changes, dict):
            raise ValueError("blueprint_patch requires part_id and object changes")

        patched = json.loads(json.dumps(base))
        target = next((p for p in patched.get("parts", []) if str(p.get("id")) == part_id), None)
        if target is None:
            raise ValueError("Part not found in cached blueprint: " + part_id)

        for key, value in changes.items():
            if key in {"dimensions_mm", "views"} and isinstance(value, dict):
                current = target.setdefault(key, {})
                current.update(value)
            else:
                target[key] = value

        report = validate_blueprint_spec(patched)
        if report.get("ok"):
            new_id = blueprint_digest(patched)
            _BLUEPRINT_CACHE[new_id] = patched
            report["blueprint_id"] = new_id
            report["previous_blueprint_id"] = bid
            report["patched_part"] = part_id
            report["cached"] = True
        return report

    raise ValueError("kind must be topology, faces, edges, fit, blueprint, or blueprint_patch")


@mcp.tool()
def model(
    steps: list[dict],
    checkpoint: bool = True,
    rollback_on_error: bool = True,
    return_steps: bool = False,
) -> dict:
    """Run a mixed modeling plan in one call.
    do supports create/object/mesh/topology/lathe/loft/sweep/curve/radial,
    orthographic (cached multi-part engineering blueprint), cameras, and validate.
    For orthographic use blueprint_id after inspect(kind='blueprint') to avoid resending the manifest."""
    resolved = []
    for step in steps:
        item = dict(step)
        if item.get("do") == "orthographic":
            bid = item.pop("blueprint_id", "")
            if "spec" not in item and bid:
                spec = _BLUEPRINT_CACHE.get(bid)
                if spec is None:
                    raise ValueError("Unknown blueprint_id; call inspect(kind='blueprint') first")
                item["spec"] = spec
            elif isinstance(item.get("spec"), dict):
                spec = item["spec"]
                report = validate_blueprint_spec(spec)
                if not report.get("ok"):
                    return report
                _BLUEPRINT_CACHE[blueprint_digest(spec)] = spec
        resolved.append(item)

    return _call(
        "smart_batch",
        steps=resolved,
        checkpoint=checkpoint,
        rollback_on_error=rollback_on_error,
        return_steps=return_steps,
    )


@mcp.tool()
def preview(
    width: int = 512,
    height: int = 512,
    mode: str = "single",
    part_ids: list[str] | None = None,
) -> dict:
    """Render a visual checkpoint.
    mode='engineering' packs Front/Back/Left/Right/Top/Bottom into one image."""
    if mode == "engineering":
        return _call(
            "engineering_contact_sheet",
            size=max(128, min(width, height, 1024)),
            part_ids=part_ids or [],
        )
    return _call("viewport_snapshot", width=width, height=height)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
