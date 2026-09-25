from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .blueprint import blueprint_digest, validate_blueprint_spec
from .planner import make_plan, materialize_strategies


def compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def benchmark_manifest(spec: dict) -> dict:
    """Static benchmark that does not require Blender.

    It measures manifest/planner payload characteristics and estimates how much
    repeated context is avoided by caching a blueprint once and addressing it by ID.
    Runtime geometry quality is measured separately by model(do='engineer').
    """
    resolved = materialize_strategies(spec)
    validation = validate_blueprint_spec(resolved)
    if not validation.get("ok"):
        return validation

    plan = make_plan(resolved)
    bid = blueprint_digest(resolved)
    manifest_bytes = compact_json_bytes(resolved)
    blueprint_ref = {"blueprint_id": bid}
    ref_bytes = compact_json_bytes(blueprint_ref)

    part_count = max(1, len(resolved.get("parts", [])))
    # Conservative comparison: a naive iterative workflow resends the whole
    # manifest once per part; cached workflow sends it once, then tiny IDs.
    naive_repeated_bytes = manifest_bytes * part_count
    cached_bytes = manifest_bytes + ref_bytes * max(0, part_count - 1)
    saved = max(0, naive_repeated_bytes - cached_bytes)
    savings_pct = (saved / naive_repeated_bytes * 100.0) if naive_repeated_bytes else 0.0

    return {
        "ok": True,
        "blueprint_id": bid,
        "parts": part_count,
        "manifest_bytes": manifest_bytes,
        "blueprint_ref_bytes": ref_bytes,
        "naive_repeated_bytes": naive_repeated_bytes,
        "cached_bytes": cached_bytes,
        "estimated_context_bytes_saved": saved,
        "estimated_context_savings_pct": round(savings_pct, 2),
        "planner": {
            "total_complexity": plan.get("total_complexity", 0),
            "strategies": {
                p["id"]: p["strategy"] for p in plan.get("parts", [])
            },
            "order": plan.get("order", []),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Smart Blender blueprint payload efficiency")
    parser.add_argument("manifest", type=Path, help="Path to a Blueprint Manifest JSON file")
    args = parser.parse_args()
    spec = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(json.dumps(benchmark_manifest(spec), indent=2))


if __name__ == "__main__":
    main()
