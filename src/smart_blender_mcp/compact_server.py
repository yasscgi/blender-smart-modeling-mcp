from __future__ import annotations

import json
import socket
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from .blueprint import validate_blueprint_spec, blueprint_digest
from .planner import make_plan, materialize_strategies

mcp = FastMCP("Smart Blender Compact")
HOST, PORT = "127.0.0.1", 9877
_BLUEPRINT_CACHE: dict[str, dict] = {}
_METRICS = {"calls": 0, "tx_bytes": 0, "rx_bytes": 0, "seconds": 0.0}


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
    started = time.perf_counter()
    with socket.create_connection((HOST, PORT), timeout=300) as s:
        s.sendall(len(raw).to_bytes(4, "big") + raw)
        size = int.from_bytes(_recv(s, 4), "big")
        payload = _recv(s, size)
    _METRICS["calls"] += 1
    _METRICS["tx_bytes"] += len(raw) + 4
    _METRICS["rx_bytes"] += len(payload) + 4
    _METRICS["seconds"] += time.perf_counter() - started
    return json.loads(payload)


def _metrics_snapshot() -> dict:
    return {
        "calls": int(_METRICS["calls"]),
        "tx_bytes": int(_METRICS["tx_bytes"]),
        "rx_bytes": int(_METRICS["rx_bytes"]),
        "seconds": round(float(_METRICS["seconds"]), 4),
    }


def _metrics_delta(before: dict) -> dict:
    now = _metrics_snapshot()
    return {
        "calls": now["calls"] - before["calls"],
        "tx_bytes": now["tx_bytes"] - before["tx_bytes"],
        "rx_bytes": now["rx_bytes"] - before["rx_bytes"],
        "seconds": round(now["seconds"] - before["seconds"], 4),
    }


def _resolve_blueprint(item: dict) -> tuple[dict, str]:
    bid = str(item.get("blueprint_id", ""))
    spec = item.get("spec")
    if not isinstance(spec, dict):
        if not bid:
            raise ValueError("engineer requires blueprint_id or spec")
        spec = _BLUEPRINT_CACHE.get(bid)
        if spec is None:
            raise ValueError("Unknown blueprint_id; cache it with inspect(kind='blueprint')")
    report = validate_blueprint_spec(spec)
    if not report.get("ok"):
        raise ValueError("Blueprint validation failed: " + "; ".join(report.get("errors", [])[:4]))
    spec = materialize_strategies(spec)
    bid = blueprint_digest(spec)
    _BLUEPRINT_CACHE[bid] = spec
    return spec, bid


def _engineer_run(item: dict) -> dict:
    before = _metrics_snapshot()
    spec, bid = _resolve_blueprint(item)
    plan = make_plan(spec)
    if not plan.get("ok"):
        return plan

    selected = [str(x) for x in item.get("part_ids", [])]
    build = _call(
        "reconstruct_blueprint",
        spec=spec,
        part_ids=selected,
        replace_existing=bool(item.get("replace_existing", True)),
        bevel_mm=float(item.get("bevel_mm", 0.0)),
        cleanup=bool(item.get("cleanup", True)),
        collection_name=str(item.get("collection_name", "Blueprint_Reconstruction")),
        checkpoint=True,
    )
    if not build.get("ok"):
        build["blueprint_id"] = bid
        build["metrics"] = _metrics_delta(before)
        return build

    part_specs = {str(p.get("id")): p for p in spec.get("parts", [])}
    built = {str(p.get("id")): p for p in build.get("parts", [])}
    target_fit = max(0.5, min(0.999, float(item.get("target_fit", 0.90))))
    max_dim_error = max(0.0, float(item.get("max_dimension_error_pct", 3.0)))
    auto_repair = bool(item.get("auto_repair", True))
    fit_resolution = max(24, min(int(item.get("fit_resolution", 64)), 160))

    checks = []
    passed = 0
    for pp in plan.get("parts", []):
        pid = pp["id"]
        if selected and pid not in selected:
            continue
        built_part = built.get(pid)
        spec_part = part_specs.get(pid)
        if not built_part or not spec_part:
            checks.append({"id": pid, "ok": False, "reason": "not-built"})
            continue

        name = str(built_part.get("name", pid))
        topo = _call("topology_state", name=name)
        repair = None
        if auto_repair and topo.get("ok") and (
            int(topo.get("nonmanifold", 0)) > 0 or int(topo.get("boundary_e", 0)) > 0
        ):
            repair = _call("mesh_validate", name=name, repair=True)
            topo = _call("topology_state", name=name)

        fit_value = None
        if pp.get("fit_check"):
            fit = _call(
                "silhouette_fit",
                part=spec_part,
                coordinate_mode=spec.get("coordinate_mode", "normalized"),
                part_id=pid,
                resolution=fit_resolution,
            )
            if fit.get("ok"):
                fit_value = float(fit.get("mean_iou", 0.0))

        quality = built_part.get("quality", {})
        dim_error = float(quality.get("max_dimension_error_pct", 999.0))
        boundary = int(topo.get("boundary_e", 999999)) if topo.get("ok") else 999999
        nonman = int(topo.get("nonmanifold", 999999)) if topo.get("ok") else 999999
        watertight = bool(spec_part.get("watertight", True))
        hard_nonmanifold = max(0, nonman - boundary) if not watertight else nonman

        fit_ok = fit_value is None or fit_value >= target_fit
        topo_ok = (boundary == 0 if watertight else True) and hard_nonmanifold == 0
        dim_ok = dim_error <= max_dim_error
        ok = fit_ok and topo_ok and dim_ok
        if ok:
            passed += 1

        entry = {
            "id": pid,
            "ok": ok,
            "strategy": pp.get("strategy"),
            "dim_err_pct": round(dim_error, 3),
            "boundary": boundary,
            "nonmanifold": nonman,
            "watertight": watertight,
        }
        if fit_value is not None:
            entry["fit"] = round(fit_value, 4)
        if repair is not None:
            entry["repair_attempted"] = True
        if not ok:
            reasons = []
            if not fit_ok:
                reasons.append("fit")
            if not dim_ok:
                reasons.append("dimensions")
            if not topo_ok:
                reasons.append("topology")
            entry["fails"] = reasons
        checks.append(entry)

    total = len(checks)
    fit_values = [x["fit"] for x in checks if "fit" in x]
    avg_fit = round(sum(fit_values) / len(fit_values), 4) if fit_values else None
    avg_dim = round(sum(x["dim_err_pct"] for x in checks) / total, 3) if total else None

    response = {
        "ok": passed == total and total > 0,
        "blueprint_id": bid,
        "parts": total,
        "passed": passed,
        "failed": total - passed,
        "avg_fit": avg_fit,
        "avg_dim_err_pct": avg_dim,
        "checks": checks,
        "scene_h": build.get("scene_h"),
        "metrics": _metrics_delta(before),
    }
    if bool(item.get("include_plan", False)):
        response["plan"] = plan
    return response


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

    if kind == "plan":
        if not isinstance(selector, dict):
            raise ValueError("plan selector requires blueprint_id or spec")
        bid = str(selector.get("blueprint_id", ""))
        spec = selector.get("spec")
        if not isinstance(spec, dict):
            spec = _BLUEPRINT_CACHE.get(bid)
        if not isinstance(spec, dict):
            raise ValueError("Unknown blueprint_id or missing spec")
        return make_plan(materialize_strategies(spec))

    if kind == "metrics":
        return {"ok": True, **_metrics_snapshot()}

    raise ValueError("kind must be topology, faces, edges, fit, blueprint, blueprint_patch, plan, or metrics")


@mcp.tool()
def model(
    steps: list[dict],
    checkpoint: bool = True,
    rollback_on_error: bool = True,
    return_steps: bool = False,
) -> dict:
    """Run a mixed modeling plan in one call.
    do supports engineer (automatic plan/build/check/repair), plus
    create/object/mesh/topology/lathe/loft/sweep/curve/radial/orthographic/cameras/validate.
    For orthographic use blueprint_id after inspect(kind='blueprint') to avoid resending the manifest."""
    engineer_steps = [s for s in steps if isinstance(s, dict) and s.get("do") == "engineer"]
    if engineer_steps:
        if len(steps) != 1 or len(engineer_steps) != 1:
            raise ValueError("do='engineer' must be the only step in model()")
        return _engineer_run(engineer_steps[0])

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
