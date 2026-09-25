from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .blueprint import validate_blueprint_spec


def choose_strategy(part: dict) -> tuple[str, str]:
    """Choose the cheapest reliable reconstruction strategy from structured evidence."""
    requested = str(part.get("strategy", "")).strip()
    if requested and requested != "auto":
        return requested, "explicit"

    profile = part.get("profile")
    sections = part.get("sections")
    path = part.get("path")
    views = part.get("views", {}) if isinstance(part.get("views", {}), dict) else {}

    if isinstance(path, list) and len(path) >= 2 and isinstance(profile, list) and len(profile) >= 2:
        return "sweep", "path+profile"
    if isinstance(sections, list) and len(sections) >= 2:
        return "loft", "cross-sections"
    if isinstance(profile, list) and len(profile) >= 2 and part.get("rotational", False):
        return "lathe", "rotational-profile"

    canonical = sum(1 for k in ("front", "side", "top") if isinstance(views.get(k), list) and len(views[k]) >= 3)
    if canonical >= 2:
        return "orthographic_hull", f"{canonical}-view-hull"

    if isinstance(profile, list) and len(profile) >= 2:
        return "lathe", "profile-fallback"
    return "orthographic_hull", "default"


def _dependency_order(parts: list[dict], links: list[dict]) -> list[str]:
    ids = [str(p.get("id")) for p in parts]
    known = set(ids)
    incoming: dict[str, int] = {pid: 0 for pid in ids}
    graph: dict[str, list[str]] = defaultdict(list)

    for link in links or []:
        src = str(link.get("from", ""))
        dst = str(link.get("to", ""))
        if src in known and dst in known and src != dst:
            graph[src].append(dst)
            incoming[dst] += 1

    q = deque(pid for pid in ids if incoming[pid] == 0)
    ordered: list[str] = []
    while q:
        pid = q.popleft()
        ordered.append(pid)
        for nxt in graph[pid]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                q.append(nxt)

    # Cycles are legal as assembly metadata; preserve source order for unresolved nodes.
    ordered.extend(pid for pid in ids if pid not in ordered)
    return ordered


def _complexity(part: dict, strategy: str) -> dict:
    views = part.get("views", {}) if isinstance(part.get("views", {}), dict) else {}
    view_points = sum(len(v) for v in views.values() if isinstance(v, list))
    feature_count = len(part.get("features", []) or [])
    pattern_instances = 0
    for feature in part.get("features", []) or []:
        pattern = feature.get("pattern") if isinstance(feature, dict) else None
        if isinstance(pattern, dict):
            try:
                pattern_instances += max(0, int(pattern.get("count", 1)) - 1)
            except Exception:
                pass

    structural = {
        "orthographic_hull": view_points,
        "lathe": len(part.get("profile", []) or []) * 4,
        "loft": sum(len(s.get("points", [])) for s in part.get("sections", []) or [] if isinstance(s, dict)),
        "sweep": len(part.get("path", []) or []) * max(2, len(part.get("profile", []) or [])),
    }.get(strategy, view_points)

    score = structural + feature_count * 8 + pattern_instances * 2
    level = "low" if score < 40 else "medium" if score < 120 else "high"
    return {
        "level": level,
        "score": int(score),
        "features": feature_count,
        "pattern_instances": pattern_instances,
    }


def make_plan(spec: dict) -> dict:
    """Return a compact deterministic engineer plan. No mesh/vertex data is emitted."""
    report = validate_blueprint_spec(spec)
    if not report.get("ok"):
        return report

    parts = [dict(p) for p in spec.get("parts", [])]
    order = _dependency_order(parts, spec.get("assembly_links", []) or [])
    by_id = {str(p.get("id")): p for p in parts}

    plan_parts = []
    total_score = 0
    for pid in order:
        part = by_id[pid]
        strategy, reason = choose_strategy(part)
        comp = _complexity(part, strategy)
        total_score += comp["score"]
        plan_parts.append({
            "id": pid,
            "strategy": strategy,
            "why": reason,
            "complexity": comp["level"],
            "score": comp["score"],
            "fit_check": strategy == "orthographic_hull",
            "topology_check": True,
            "features": comp["features"],
            "patterns": comp["pattern_instances"],
        })

    return {
        "ok": True,
        "part_count": len(plan_parts),
        "order": order,
        "parts": plan_parts,
        "assembly_links": len(spec.get("assembly_links", []) or []),
        "total_complexity": total_score,
        "engineering_ready": report.get("engineering_ready", False),
        "blueprint_h": report.get("blueprint_h"),
    }


def materialize_strategies(spec: dict) -> dict:
    """Return a copy with any missing/'auto' strategy resolved deterministically."""
    out = {**spec}
    out["parts"] = []
    for part in spec.get("parts", []):
        item = dict(part)
        strategy, _ = choose_strategy(item)
        item["strategy"] = strategy
        out["parts"].append(item)
    return out
