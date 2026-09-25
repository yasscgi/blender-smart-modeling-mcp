# Smart Blender MCP — Codex Low-Token Instructions

When the `smart_blender` MCP is available, treat Blender as a headless engineering target by default.

## Hard rules

1. DO NOT use computer/screen interaction to inspect Blender during iterative modeling.
2. DO NOT take screenshots of the Blender UI after each edit.
3. DO NOT repeatedly call `preview`.
4. DO NOT dump mesh vertices, edge IDs, face IDs, or full Blender state unless debugging a specific failure.
5. DO NOT rebuild all parts when only one part fails.
6. Prefer one semantic MCP call over many small calls.

## Preferred workflow

1. Read the user's reference image once.
2. Produce one compact Blueprint Manifest.
3. Cache it with `inspect(kind="blueprint")`.
4. Optionally inspect `kind="plan"` once.
5. Run `model([{ "do": "engineer", ... }])`.
6. Use the returned numeric checks: fit, dimensions, topology, failed_ids.
7. If several parts need comparison, use `inspect(kind="fit_batch")` in one call.
8. Patch only failed parts with `inspect(kind="blueprint_patch")`.
9. Rebuild only the patched part_ids.
10. Call `preview(mode="engineering")` ONCE at the final visual checkpoint.

## Visual budget

The compact MCP allows one preview by default. Do not work around this by opening Blender and inspecting the UI directly. If another visual checkpoint is genuinely required, explain why and intentionally call preview with `force=true`.

## Stop conditions

Stop iterating when:
- silhouette fit reaches the requested target, or improvement is negligible;
- dimension error is within tolerance;
- topology requirements pass.

Do not spend tokens pursuing invisible cosmetic differences after the engineering targets pass.

## Response discipline

Keep tool reasoning compact. After each engineer pass, focus only on:
- failed_ids
- worst fit/view
- dimension error
- topology failure
- next patch

Do not narrate every Blender operation.
