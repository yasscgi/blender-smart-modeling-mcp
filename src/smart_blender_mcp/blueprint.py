from __future__ import annotations

import hashlib
import json
from typing import Any


VIEWS = ("front", "side", "top")


def _span(points: list[list[float]], axis: int) -> float:
    vals = [float(p[axis]) for p in points]
    return max(vals) - min(vals) if vals else 0.0


def _valid_polygon(points: Any) -> bool:
    return (
        isinstance(points, list)
        and len(points) >= 3
        and all(
            isinstance(p, (list, tuple))
            and len(p) == 2
            and all(isinstance(v, (int, float)) for v in p)
            for p in points
        )
    )


def blueprint_digest(spec: dict) -> str:
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.blake2s(raw.encode("utf-8"), digest_size=8).hexdigest()


def validate_blueprint_spec(spec: dict) -> dict:
    """Validate a compact engineering blueprint manifest.

    Supported manifest:
      {
        "model_id": "...",
        "units": "mm",
        "coordinate_mode": "normalized" | "mm",
        "parts": [
          {
            "id": "P01",
            "name": "Body",
            "dimensions_mm": {"width": 80, "depth": 50, "height": 120},
            "views": {
              "front": [[u,v], ...],
              "side": [[u,v], ...],
              "top": [[u,v], ...]
            },
            "position_mm": [0,0,0],
            "confidence": 0.9
          }
        ]
      }

    Normalized coordinates use 0..1 within each orthographic view.
    """
    errors: list[str] = []
    warnings: list[str] = []
    parts_out: list[dict] = []

    if not isinstance(spec, dict):
        return {"ok": False, "errors": ["spec must be an object"], "warnings": []}

    units = spec.get("units", "mm")
    if units != "mm":
        errors.append("Only millimeter engineering manifests are supported in v0.4")

    mode = spec.get("coordinate_mode", "normalized")
    if mode not in {"normalized", "mm"}:
        errors.append("coordinate_mode must be 'normalized' or 'mm'")

    parts = spec.get("parts", [])
    if not isinstance(parts, list) or not parts:
        errors.append("parts must be a non-empty list")
        parts = []

    seen: set[str] = set()
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            errors.append(f"parts[{idx}] must be an object")
            continue

        pid = str(part.get("id") or f"P{idx+1:02d}")
        if pid in seen:
            errors.append(f"duplicate part id: {pid}")
        seen.add(pid)

        dims = part.get("dimensions_mm", {})
        try:
            width = float(dims["width"])
            depth = float(dims["depth"])
            height = float(dims["height"])
            if min(width, depth, height) <= 0:
                raise ValueError
        except Exception:
            errors.append(f"{pid}: dimensions_mm must contain positive width/depth/height")
            width = depth = height = 0.0

        views = part.get("views", {})
        available = []
        missing = []
        for view in VIEWS:
            pts = views.get(view)
            if pts is None:
                missing.append(view)
                continue
            if not _valid_polygon(pts):
                errors.append(f"{pid}: {view} must contain at least 3 numeric 2D points")
                continue
            available.append(view)

            if mode == "normalized":
                outside = sum(
                    1 for p in pts
                    if float(p[0]) < -0.05 or float(p[0]) > 1.05
                    or float(p[1]) < -0.05 or float(p[1]) > 1.05
                )
                if outside:
                    warnings.append(f"{pid}: {view} has {outside} normalized points outside 0..1")

        if len(available) < 2:
            warnings.append(f"{pid}: only {len(available)} orthographic view(s); 2+ strongly recommended")
        if missing:
            warnings.append(f"{pid}: missing views: {','.join(missing)}")

        conflicts = []
        if mode == "mm" and width and depth and height:
            tol = float(part.get("dimension_tolerance", 0.08))
            checks = []
            if _valid_polygon(views.get("front")):
                checks += [
                    ("front.width", _span(views["front"], 0), width),
                    ("front.height", _span(views["front"], 1), height),
                ]
            if _valid_polygon(views.get("side")):
                checks += [
                    ("side.depth", _span(views["side"], 0), depth),
                    ("side.height", _span(views["side"], 1), height),
                ]
            if _valid_polygon(views.get("top")):
                checks += [
                    ("top.width", _span(views["top"], 0), width),
                    ("top.depth", _span(views["top"], 1), depth),
                ]
            for label, got, expected in checks:
                if expected > 0 and abs(got - expected) / expected > tol:
                    conflicts.append({
                        "field": label,
                        "span_mm": round(got, 3),
                        "dimension_mm": round(expected, 3),
                    })
            if conflicts:
                warnings.append(f"{pid}: orthographic spans conflict with stated dimensions")

        confidence = max(0.0, min(1.0, float(part.get("confidence", 1.0))))
        if confidence < 0.6:
            warnings.append(f"{pid}: low dimensional/reference confidence ({confidence:.2f})")

        features = part.get("features", [])
        if not isinstance(features, list):
            errors.append(f"{pid}: features must be a list")
            features = []
        valid_features = 0
        for fi, feature in enumerate(features):
            if not isinstance(feature, dict):
                errors.append(f"{pid}: features[{fi}] must be an object")
                continue
            ftype = feature.get("type")
            if ftype in {"hole_cylinder", "boss_cylinder"}:
                if float(feature.get("diameter_mm", 0)) <= 0 or float(feature.get("depth_mm", 0)) <= 0:
                    errors.append(f"{pid}: {ftype} requires positive diameter_mm and depth_mm")
                    continue
                if str(feature.get("axis", "Z")).upper() not in {"X", "Y", "Z"}:
                    errors.append(f"{pid}: {ftype} axis must be X/Y/Z")
                    continue
                valid_features += 1
            elif ftype in {"cut_box", "boss_box"}:
                fdims = feature.get("dimensions_mm", [])
                if (
                    not isinstance(fdims, list)
                    or len(fdims) != 3
                    or any(float(v) <= 0 for v in fdims)
                ):
                    errors.append(f"{pid}: {ftype} requires 3 positive dimensions_mm")
                    continue
                valid_features += 1
            else:
                warnings.append(f"{pid}: unsupported feature type {ftype!r}")

        view_score = {0: 0.0, 1: 0.45, 2: 0.82, 3: 1.0}[min(3, len(available))]
        conflict_penalty = min(0.35, 0.08 * len(conflicts))
        reconstructability = max(0.0, min(1.0, view_score * confidence - conflict_penalty))
        if reconstructability < 0.65:
            warnings.append(f"{pid}: reconstructability is low ({reconstructability:.2f}); add another orthographic view or dimensions")

        parts_out.append({
            "id": pid,
            "views": available,
            "missing": missing,
            "dimensions_mm": [round(width, 3), round(depth, 3), round(height, 3)],
            "conflicts": conflicts,
            "confidence": round(confidence, 3),
            "features": valid_features,
            "reconstructability": round(reconstructability, 3),
        })

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "parts": parts_out,
        "part_count": len(parts_out),
        "engineering_ready": (not errors) and all(p["reconstructability"] >= 0.65 for p in parts_out),
        "avg_reconstructability": round(
            sum(p["reconstructability"] for p in parts_out) / len(parts_out), 3
        ) if parts_out else 0.0,
        "blueprint_h": blueprint_digest(spec),
    }


def part_polygon_mm(part: dict, view: str, mode: str) -> list[list[float]]:
    """Map a part view polygon into centered millimeter coordinates.

    front -> [x,z]
    side  -> [y,z]
    top   -> [x,y]
    """
    points = part.get("views", {}).get(view)
    if not _valid_polygon(points):
        return []

    dims = part["dimensions_mm"]
    w = float(dims["width"])
    d = float(dims["depth"])
    h = float(dims["height"])

    if mode == "mm":
        return [[float(a), float(b)] for a, b in points]

    if view == "front":
        return [[(float(u) - 0.5) * w, (float(v) - 0.5) * h] for u, v in points]
    if view == "side":
        return [[(float(u) - 0.5) * d, (float(v) - 0.5) * h] for u, v in points]
    if view == "top":
        return [[(float(u) - 0.5) * w, (float(v) - 0.5) * d] for u, v in points]
    raise ValueError(f"Unsupported view: {view}")
