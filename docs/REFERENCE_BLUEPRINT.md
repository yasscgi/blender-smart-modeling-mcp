# Reference Blueprint Contract — V0.4

This contract is designed for an AI vision model that receives one master reference image containing many parts and then calls the Blender MCP with a compact engineering manifest.

## Recommended single-sheet layout

Use one clean sheet with:

1. **Assembled 3/4 view**
2. **Exploded assembly view**
3. **Part grid**, one box per part: P01, P02, ...
4. For each part: **Front, Back, Right, Left, Top, Bottom**
5. At least one known overall dimension or scale reference
6. Dimension callouts for important holes, slots, pegs, wall thicknesses and offsets
7. Clear white/neutral background and no perspective in orthographic views

The MCP currently reconstructs geometry from the canonical **Front + Side + Top** silhouettes. Opposite views are useful to the vision model for consistency checking and hidden-detail inference.

## Coordinate convention

- Units: millimeters.
- Blender engineering mode uses numeric Blender coordinates as millimeter design units and configures the scene unit display to millimeters.
- +X = right
- +Y = back
- +Z = up
- Front silhouette points represent X/Z.
- Side silhouette points represent Y/Z.
- Top silhouette points represent X/Y.

For `coordinate_mode="normalized"`, every polygon point is in 0..1 inside its stated part dimensions. This is preferred because it keeps manifests compact and scale-independent.

## Part manifest

Each part should contain:

- `id`: stable ID such as P01
- `name`
- `dimensions_mm`: width/depth/height
- `views.front`, `views.side`, `views.top`: ordered closed silhouettes (do not repeat first point)
- `position_mm`: assembly position
- `confidence`: 0..1
- optional `features`

## Engineering features

Supported V0.4 features:

### Hole
```json
{"type":"hole_cylinder","axis":"Y","center_mm":[0,0,20],"diameter_mm":12,"depth_mm":70}
```

### Cylindrical boss
```json
{"type":"boss_cylinder","axis":"Z","center_mm":[0,0,58],"diameter_mm":24,"depth_mm":8}
```

### Box cut
```json
{"type":"cut_box","center_mm":[0,0,0],"dimensions_mm":[20,8,12],"rotation_deg":[0,0,0]}
```

### Box boss
```json
{"type":"boss_box","center_mm":[0,0,0],"dimensions_mm":[20,8,12],"rotation_deg":[0,0,0]}
```

## Reconstruction method

For each part Blender:

1. builds an extrusion/prism from the Front silhouette,
2. builds one from the Side silhouette,
3. builds one from the Top silhouette,
4. computes exact Boolean intersections to form an orthographic **visual hull**,
5. applies explicit engineering features,
6. reports final dimensions and topology health.

This produces a strong base for product/hard-surface modeling. Fine surface features that do not change an orthographic silhouette must be encoded as explicit features or refined in later modeling passes.

## Low-token workflow

1. Vision model reads the master sheet once.
2. It emits one Blueprint Manifest for all parts.
3. Call compact `inspect(kind="blueprint", selector=manifest)`.
4. Fix only reported low-confidence/conflicting parts.
5. Call compact `model()` with one `orthographic` step.
6. Use returned per-part quality metrics.
7. Request `preview()` only at visual milestones.
8. Refine only failing parts through semantic mesh/topology tools.

This keeps repeated image descriptions, vertex dumps and per-part tool chatter out of the model context.
