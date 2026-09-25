bl_info = {
    "name": "Smart Modeling MCP",
    "author": "Yasscgi",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Smart MCP",
    "category": "3D View",
}

import bpy
import bmesh
import hashlib
import json
import math
import queue
import socket
import threading
from mathutils import Vector, Matrix

HOST, PORT = "127.0.0.1", 9877
_requests = queue.Queue()
_running = False
_server = None


def result(ok=True, **kw):
    return {"ok": ok, **kw}


def active(name=""):
    o = bpy.data.objects.get(name) if name else bpy.context.active_object
    if not o:
        raise ValueError("Object not found")
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    return o


def _round3(v):
    return [round(float(x), 4) for x in v]


def object_digest(o):
    raw = [o.name, o.type, *[round(float(x), 5) for x in o.dimensions]]
    if o.type == "MESH":
        raw += [len(o.data.vertices), len(o.data.edges), len(o.data.polygons)]
        if o.data.vertices:
            # Tiny deterministic sample; enough for change detection without serializing geometry.
            step = max(1, len(o.data.vertices) // 16)
            for i in range(0, len(o.data.vertices), step):
                v = o.data.vertices[i]
                raw.extend(round(float(c), 4) for c in v.co)
                if i // step >= 15:
                    break
    return hashlib.blake2s(repr(raw).encode(), digest_size=6).hexdigest()


def compact_obj(o):
    d = {
        "n": o.name,
        "t": o.type,
        "l": _round3(o.location),
        "d": _round3(o.dimensions),
        "h": object_digest(o),
    }
    if o.type == "MESH":
        d["v"] = len(o.data.vertices)
        d["e"] = len(o.data.edges)
        d["f"] = len(o.data.polygons)
        if o.modifiers:
            d["m"] = [m.type for m in o.modifiers]
    return d


def scene_digest():
    raw = "|".join(object_digest(o) for o in sorted(bpy.context.scene.objects, key=lambda x: x.name))
    return hashlib.blake2s(raw.encode(), digest_size=8).hexdigest()


def op_create(p):
    kind = p["kind"].upper()
    loc = p["location"]
    q = p.get("params", {})
    fn = {
        "CUBE": bpy.ops.mesh.primitive_cube_add,
        "CYLINDER": bpy.ops.mesh.primitive_cylinder_add,
        "SPHERE": bpy.ops.mesh.primitive_uv_sphere_add,
        "CONE": bpy.ops.mesh.primitive_cone_add,
        "TORUS": bpy.ops.mesh.primitive_torus_add,
        "PLANE": bpy.ops.mesh.primitive_plane_add,
    }.get(kind)
    if not fn:
        raise ValueError("Unsupported primitive")

    args = {"location": loc}
    if kind in {"CYLINDER", "CONE"}:
        args["vertices"] = int(q.get("vertices", 32))
    if kind == "SPHERE":
        args.update(
            segments=int(q.get("segments", 32)),
            ring_count=int(q.get("rings", 16)),
        )
    if kind == "TORUS":
        args.update(
            major_segments=int(q.get("major_segments", 48)),
            minor_segments=int(q.get("minor_segments", 12)),
            major_radius=float(q.get("major_radius", 1.0)),
            minor_radius=float(q.get("minor_radius", 0.25)),
        )

    fn(**args)
    o = bpy.context.object
    if p.get("name"):
        o.name = p["name"]
    o.scale = p["scale"]
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return result(object=compact_obj(o), scene_h=scene_digest())


def add_mod(o, typ, vals):
    m = o.modifiers.new(vals.get("name", typ.title()), typ)
    for k, v in vals.items():
        if k != "name" and hasattr(m, k):
            setattr(m, k, v)
    return m


def _make_cutter(spec):
    kind = spec.get("kind", "cube").upper()
    loc = spec.get("location", [0, 0, 0])
    rot = spec.get("rotation", [0, 0, 0])
    scale = spec.get("scale", [1, 1, 1])
    if kind == "CUBE":
        bpy.ops.mesh.primitive_cube_add(location=loc, rotation=rot)
    elif kind == "SPHERE":
        bpy.ops.mesh.primitive_uv_sphere_add(
            segments=int(spec.get("segments", 32)),
            ring_count=int(spec.get("rings", 16)),
            location=loc,
            rotation=rot,
        )
    elif kind == "CYLINDER":
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=int(spec.get("vertices", 32)),
            radius=float(spec.get("radius", 1.0)),
            depth=float(spec.get("depth", 2.0)),
            location=loc,
            rotation=rot,
        )
    else:
        raise ValueError("boolean_primitive supports cube/sphere/cylinder")
    cutter = bpy.context.object
    cutter.name = "__SMART_MCP_CUTTER__"
    cutter.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return cutter


def do_batch(p):
    if p.get("checkpoint", True):
        bpy.ops.ed.undo_push(message="Smart MCP object batch")
    changed = []

    for x in p["ops"]:
        typ = x["op"]
        o = active(x.get("name", ""))

        if typ == "transform":
            if "location" in x:
                o.location = x["location"]
            if "rotation" in x:
                o.rotation_euler = x["rotation"]
            if "scale" in x:
                o.scale = x["scale"]

        elif typ == "bevel":
            add_mod(o, "BEVEL", {"width": x.get("width", 0.01), "segments": x.get("segments", 3)})

        elif typ == "solidify":
            add_mod(o, "SOLIDIFY", {"thickness": x.get("thickness", 0.01)})

        elif typ == "subdivide":
            add_mod(o, "SUBSURF", {
                "levels": x.get("levels", 2),
                "render_levels": x.get("levels", 2),
            })

        elif typ == "mirror":
            m = add_mod(o, "MIRROR", {})
            m.use_axis[0] = x.get("x", True)
            m.use_axis[1] = x.get("y", False)
            m.use_axis[2] = x.get("z", False)

        elif typ == "boolean":
            target = bpy.data.objects.get(x["target"])
            if not target:
                raise ValueError("Boolean target not found: " + x["target"])
            m = add_mod(o, "BOOLEAN", {})
            m.operation = x.get("operation", "DIFFERENCE")
            m.solver = "EXACT"
            m.object = target
            if x.get("apply", False):
                bpy.context.view_layer.objects.active = o
                bpy.ops.object.modifier_apply(modifier=m.name)

        elif typ == "boolean_primitive":
            target = o
            cutter = _make_cutter(x)
            target.select_set(True)
            bpy.context.view_layer.objects.active = target
            m = target.modifiers.new("SmartBoolean", "BOOLEAN")
            m.operation = x.get("operation", "DIFFERENCE")
            m.solver = "EXACT"
            m.object = cutter
            bpy.ops.object.modifier_apply(modifier=m.name)
            bpy.data.objects.remove(cutter, do_unlink=True)
            active(target.name)

        elif typ == "shade_smooth":
            if o.type == "MESH":
                for f in o.data.polygons:
                    f.use_smooth = True

        elif typ == "apply_modifier":
            bpy.context.view_layer.objects.active = o
            bpy.ops.object.modifier_apply(modifier=x["modifier"])

        elif typ == "duplicate":
            n = o.copy()
            n.data = o.data.copy() if o.data else None
            bpy.context.collection.objects.link(n)
            n.name = x.get("new_name", o.name + "_copy")

        elif typ == "delete":
            name = o.name
            bpy.data.objects.remove(o, do_unlink=True)
            changed.append(name)
            continue

        else:
            raise ValueError("Unsupported batch op: " + typ)

        changed.append(o.name)

    return result(changed=changed, count=len(changed), scene_h=scene_digest())


def _bm_bounds(bm):
    if not bm.verts:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    lo = Vector((
        min(v.co.x for v in bm.verts),
        min(v.co.y for v in bm.verts),
        min(v.co.z for v in bm.verts),
    ))
    hi = Vector((
        max(v.co.x for v in bm.verts),
        max(v.co.y for v in bm.verts),
        max(v.co.z for v in bm.verts),
    ))
    return lo, hi


def _norm01(x, lo, hi):
    span = hi - lo
    if abs(span) < 1e-12:
        return 0.5
    return (x - lo) / span


def _face_matches(face, selector, lo, hi):
    if not selector:
        return True

    if "all" in selector:
        return all(_face_matches(face, s, lo, hi) for s in selector["all"])
    if "any" in selector:
        return any(_face_matches(face, s, lo, hi) for s in selector["any"])

    if "face_ids" in selector and face.index not in set(selector["face_ids"]):
        return False

    region = selector.get("region")
    if region and region != "all":
        c = face.calc_center_median()
        nx = _norm01(c.x, lo.x, hi.x)
        ny = _norm01(c.y, lo.y, hi.y)
        nz = _norm01(c.z, lo.z, hi.z)
        band = max(0.001, min(0.5, float(selector.get("band", 0.15))))
        region_ok = {
            "top": nz >= 1.0 - band,
            "bottom": nz <= band,
            "right": nx >= 1.0 - band,
            "left": nx <= band,
            "front": ny <= band,
            "back": ny >= 1.0 - band,
        }.get(region)
        if region_ok is None:
            raise ValueError("Unknown region: " + str(region))
        if not region_ok:
            return False

    box = selector.get("bbox")
    if box:
        c = face.calc_center_median()
        vals = {
            "x": _norm01(c.x, lo.x, hi.x),
            "y": _norm01(c.y, lo.y, hi.y),
            "z": _norm01(c.z, lo.z, hi.z),
        }
        for axis, rng in box.items():
            if not (float(rng[0]) <= vals[axis] <= float(rng[1])):
                return False

    normal = selector.get("normal")
    if normal:
        axis_name = normal.get("axis", "Z").upper()
        sign = -1.0 if axis_name.startswith("-") else 1.0
        axis_name = axis_name.replace("-", "").replace("+", "")
        axis = {
            "X": Vector((1, 0, 0)),
            "Y": Vector((0, 1, 0)),
            "Z": Vector((0, 0, 1)),
        }[axis_name] * sign
        if face.normal.normalized().dot(axis) < float(normal.get("min_dot", 0.7)):
            return False

    return True


def _select_faces(bm, selector):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    lo, hi = _bm_bounds(bm)
    return [f for f in bm.faces if _face_matches(f, selector or {"region": "all"}, lo, hi)]


def _region_verts(faces):
    return list({v for f in faces for v in f.verts})


def _region_center(verts):
    if not verts:
        return Vector((0, 0, 0))
    c = Vector((0, 0, 0))
    for v in verts:
        c += v.co
    return c / len(verts)


def _average_normal(faces):
    if not faces:
        return Vector((0, 0, 1))
    n = Vector((0, 0, 0))
    for f in faces:
        n += f.normal * max(f.calc_area(), 1e-8)
    return n.normalized() if n.length > 1e-10 else Vector((0, 0, 1))


def mesh_query(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.normal_update()
    faces = _select_faces(bm, p.get("selector", {"region": "all"}))
    verts = _region_verts(faces)
    center = _region_center(verts)
    avg_n = _average_normal(faces)

    out = result(
        name=o.name,
        faces=len(faces),
        verts=len(verts),
        center=_round3(center),
        normal=_round3(avg_n),
        object_h=object_digest(o),
    )

    if p.get("include_ids"):
        cap = max(1, min(512, int(p.get("max_ids", 64))))
        ids = [f.index for f in faces]
        out["face_ids"] = ids[:cap]
        out["truncated"] = len(ids) > cap

    bm.free()
    return out


def _extrude(bm, faces, op):
    if not faces:
        raise ValueError("Selector matched no faces")
    n = _average_normal(faces)
    res = bmesh.ops.extrude_face_region(bm, geom=faces)
    new_verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
    if "vector" in op:
        vec = Vector(op["vector"])
    else:
        vec = n * float(op.get("distance", 0.1))
    bmesh.ops.translate(bm, verts=new_verts, vec=vec)
    return len(new_verts)


def _inset(bm, faces, op):
    if not faces:
        raise ValueError("Selector matched no faces")
    res = bmesh.ops.inset_region(
        bm,
        faces=faces,
        use_boundary=True,
        use_even_offset=True,
        thickness=float(op.get("thickness", 0.05)),
        depth=float(op.get("depth", 0.0)),
    )
    return len(res.get("faces", []))


def _edge_matches(edge, selector, lo, hi):
    if not selector:
        return True

    if "all" in selector:
        return all(_edge_matches(edge, s, lo, hi) for s in selector["all"])
    if "any" in selector:
        return any(_edge_matches(edge, s, lo, hi) for s in selector["any"])

    if "edge_ids" in selector and edge.index not in set(selector["edge_ids"]):
        return False

    if "boundary" in selector and bool(selector["boundary"]) != bool(edge.is_boundary):
        return False

    if "manifold" in selector and bool(selector["manifold"]) != bool(edge.is_manifold):
        return False

    mid = (edge.verts[0].co + edge.verts[1].co) * 0.5

    box = selector.get("bbox")
    if box:
        vals = {
            "x": _norm01(mid.x, lo.x, hi.x),
            "y": _norm01(mid.y, lo.y, hi.y),
            "z": _norm01(mid.z, lo.z, hi.z),
        }
        for axis, rng in box.items():
            if not (float(rng[0]) <= vals[axis] <= float(rng[1])):
                return False

    region = selector.get("region")
    if region and region != "all":
        nx = _norm01(mid.x, lo.x, hi.x)
        ny = _norm01(mid.y, lo.y, hi.y)
        nz = _norm01(mid.z, lo.z, hi.z)
        band = max(0.001, min(0.5, float(selector.get("band", 0.15))))
        ok = {
            "top": nz >= 1.0 - band,
            "bottom": nz <= band,
            "right": nx >= 1.0 - band,
            "left": nx <= band,
            "front": ny <= band,
            "back": ny >= 1.0 - band,
        }.get(region)
        if ok is None:
            raise ValueError("Unknown edge region: " + str(region))
        if not ok:
            return False

    orient = selector.get("orientation")
    if orient:
        axis_name = orient.get("axis", "Z").upper()
        sign = -1.0 if axis_name.startswith("-") else 1.0
        axis_name = axis_name.replace("-", "").replace("+", "")
        axis = {
            "X": Vector((1, 0, 0)),
            "Y": Vector((0, 1, 0)),
            "Z": Vector((0, 0, 1)),
        }[axis_name] * sign
        d = edge.verts[1].co - edge.verts[0].co
        if d.length < 1e-12:
            return False
        # abs allows either edge winding direction.
        if abs(d.normalized().dot(axis)) < float(orient.get("min_dot", 0.8)):
            return False

    min_len = selector.get("min_length")
    max_len = selector.get("max_length")
    length = edge.calc_length()
    if min_len is not None and length < float(min_len):
        return False
    if max_len is not None and length > float(max_len):
        return False

    return True


def _edge_mid(e):
    return (e.verts[0].co + e.verts[1].co) * 0.5


def _edge_ring(seed):
    found = {seed}
    stack = [seed]
    while stack:
        edge = stack.pop()
        for face in edge.link_faces:
            if len(face.edges) != 4:
                continue
            opp = None
            ev = set(edge.verts)
            for candidate in face.edges:
                if candidate is edge:
                    continue
                if not ev.intersection(candidate.verts):
                    opp = candidate
                    break
            if opp is not None and opp not in found:
                found.add(opp)
                stack.append(opp)
    return list(found)


def _edge_loop(seed):
    found = {seed}

    def walk(start_vertex, first_edge):
        current_v = start_vertex
        current_e = first_edge
        direction = (current_e.other_vert(current_v).co - current_v.co)
        if direction.length < 1e-12:
            return
        direction.normalize()

        while True:
            candidates = [e for e in current_v.link_edges if e is not current_e and e not in found]
            if not candidates:
                return
            scored = []
            for e in candidates:
                d = e.other_vert(current_v).co - current_v.co
                if d.length < 1e-12:
                    continue
                d.normalize()
                scored.append((abs(direction.dot(d)), e, d))
            if not scored:
                return
            score, nxt, nxt_dir = max(scored, key=lambda item: item[0])
            if score < 0.55:
                return
            found.add(nxt)
            current_v = nxt.other_vert(current_v)
            current_e = nxt
            direction = nxt_dir
            if current_v in seed.verts:
                return

    walk(seed.verts[0], seed)
    walk(seed.verts[1], seed)
    return list(found)


def _select_edges(bm, selector):
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    lo, hi = _bm_bounds(bm)

    selector = dict(selector or {})
    nearest = selector.pop("nearest", None)
    expand = selector.pop("expand", None)
    edges = [e for e in bm.edges if _edge_matches(e, selector, lo, hi)]

    if nearest is not None and edges:
        p = Vector(nearest)
        edges = [min(edges, key=lambda e: (_edge_mid(e) - p).length_squared)]

    if expand and edges:
        seed = edges[0]
        if expand == "ring":
            edges = _edge_ring(seed)
        elif expand == "loop":
            edges = _edge_loop(seed)
        else:
            raise ValueError("Unknown edge expansion: " + str(expand))

    return edges


def _edge_center(edges):
    verts = list({v for e in edges for v in e.verts})
    return _region_center(verts)


def _boundary_components(bm):
    remaining = {e for e in bm.edges if e.is_boundary}
    components = []
    while remaining:
        seed = remaining.pop()
        comp = [seed]
        stack = [seed]
        while stack:
            e = stack.pop()
            for v in e.verts:
                for n in v.link_edges:
                    if n in remaining and n.is_boundary:
                        remaining.remove(n)
                        comp.append(n)
                        stack.append(n)
        components.append(comp)
    return components


def _boundary_loop_from_spec(bm, spec):
    comps = _boundary_components(bm)
    if not comps:
        raise ValueError("Mesh has no boundary loops")

    component = (spec or {}).get("component")
    if component:
        axis_map = {
            "topmost": (2, 1),
            "bottommost": (2, -1),
            "rightmost": (0, 1),
            "leftmost": (0, -1),
            "backmost": (1, 1),
            "frontmost": (1, -1),
        }
        if component not in axis_map:
            raise ValueError("Unknown boundary component selector: " + str(component))
        axis, sign = axis_map[component]
        return max(comps, key=lambda c: sign * _edge_center(c)[axis])

    if "nearest" in (spec or {}):
        p = Vector(spec["nearest"])
        return min(comps, key=lambda c: (_edge_center(c) - p).length_squared)

    wanted = set(_select_edges(bm, {**(spec or {}), "boundary": True}))
    if wanted:
        return max(comps, key=lambda c: len(wanted.intersection(c)))

    if len(comps) == 1:
        return comps[0]
    raise ValueError("Boundary selector is ambiguous; use component or nearest")


def edge_query(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.normal_update()
    edges = _select_edges(bm, p.get("selector", {}))
    center = _edge_center(edges) if edges else Vector((0, 0, 0))
    total_len = sum(e.calc_length() for e in edges)

    out = result(
        name=o.name,
        edges=len(edges),
        center=_round3(center),
        length=round(float(total_len), 5),
        boundary=sum(1 for e in edges if e.is_boundary),
        object_h=object_digest(o),
    )
    if p.get("include_ids"):
        cap = max(1, min(512, int(p.get("max_ids", 64))))
        ids = [e.index for e in edges]
        out["edge_ids"] = ids[:cap]
        out["truncated"] = len(ids) > cap

    bm.free()
    return out


def topology_state(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.normal_update()

    tris = sum(1 for f in bm.faces if len(f.verts) == 3)
    quads = sum(1 for f in bm.faces if len(f.verts) == 4)
    ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
    nonman = sum(1 for e in bm.edges if not e.is_manifold)
    boundary_edges = sum(1 for e in bm.edges if e.is_boundary)
    poles3 = sum(1 for v in bm.verts if len(v.link_edges) == 3)
    poles5 = sum(1 for v in bm.verts if len(v.link_edges) >= 5)

    loops = []
    for comp in _boundary_components(bm):
        loops.append({
            "e": len(comp),
            "c": _round3(_edge_center(comp)),
            "len": round(sum(e.calc_length() for e in comp), 5),
        })
    loops.sort(key=lambda x: (x["c"][2], x["c"][1], x["c"][0]))

    out = result(
        name=o.name,
        v=len(bm.verts),
        e=len(bm.edges),
        f=len(bm.faces),
        tri=tris,
        quad=quads,
        ngon=ngons,
        boundary_e=boundary_edges,
        boundary_loops=loops[:16],
        boundary_loop_count=len(loops),
        nonmanifold=nonman,
        valence3=poles3,
        valence5plus=poles5,
        object_h=object_digest(o),
    )
    bm.free()
    return out


def topology_batch(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")
    if p.get("checkpoint", True):
        bpy.ops.ed.undo_push(message="Smart MCP topology batch")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.normal_update()
    stats = []

    try:
        for op in p.get("ops", []):
            typ = op["op"]

            if typ == "loop_cut":
                axis_name = op.get("axis", "Z").upper()
                axis_i = {"X": 0, "Y": 1, "Z": 2}[axis_name]
                lo, hi = _bm_bounds(bm)
                t = max(0.0, min(1.0, float(op.get("position", 0.5))))
                coord = lo[axis_i] + (hi[axis_i] - lo[axis_i]) * t
                plane_co = Vector((0, 0, 0))
                plane_no = Vector((0, 0, 0))
                plane_co[axis_i] = coord
                plane_no[axis_i] = 1.0
                cut = bmesh.ops.bisect_plane(
                    bm,
                    geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                    dist=float(op.get("epsilon", 1e-6)),
                    plane_co=plane_co,
                    plane_no=plane_no,
                    clear_inner=False,
                    clear_outer=False,
                )
                stats.append({"op": typ, "axis": axis_name, "position": t, "cut": len(cut.get("geom_cut", []))})
                bm.normal_update()
                continue

            if typ == "bridge_boundaries":
                first = _boundary_loop_from_spec(bm, op.get("first", {"component": "bottommost"}))
                second = _boundary_loop_from_spec(bm, op.get("second", {"component": "topmost"}))
                if set(first) == set(second):
                    raise ValueError("bridge_boundaries selected the same loop twice")
                kwargs = {
                    "edges": list(dict.fromkeys(first + second)),
                    "use_pairs": False,
                }
                if "twist" in op:
                    kwargs["twist_offset"] = int(op["twist"])
                bridged = bmesh.ops.bridge_loops(bm, **kwargs)
                stats.append({
                    "op": typ,
                    "first": len(first),
                    "second": len(second),
                    "new": len(bridged.get("faces", [])),
                })
                bm.normal_update()
                continue

            edges = _select_edges(bm, op.get("selector", {}))
            if not edges:
                raise ValueError(typ + " selector matched no edges")

            if typ in {"bevel_edges", "support_edges"}:
                width = float(op.get("width", 0.01))
                segments = 1 if typ == "support_edges" else max(1, int(op.get("segments", 2)))
                bevel = bmesh.ops.bevel(
                    bm,
                    geom=edges,
                    offset=width,
                    offset_type="OFFSET",
                    segments=segments,
                    profile=float(op.get("profile", 0.5)),
                    affect="EDGES",
                )
                stats.append({"op": typ, "edges": len(edges), "new": len(bevel.get("faces", []))})

            elif typ == "subdivide_edges":
                subdiv = bmesh.ops.subdivide_edges(
                    bm,
                    edges=edges,
                    cuts=max(1, int(op.get("cuts", 1))),
                    use_grid_fill=bool(op.get("grid_fill", False)),
                    smooth=float(op.get("smooth", 0.0)),
                )
                stats.append({"op": typ, "edges": len(edges), "new": len(subdiv.get("geom_inner", []))})

            elif typ == "collapse_edges":
                before = len(bm.verts)
                bmesh.ops.collapse(bm, edges=edges, uvs=True)
                stats.append({"op": typ, "edges": len(edges), "removed_v": before - len(bm.verts)})

            elif typ == "dissolve_edges":
                bmesh.ops.dissolve_edges(
                    bm,
                    edges=edges,
                    use_verts=bool(op.get("use_verts", False)),
                    use_face_split=bool(op.get("use_face_split", False)),
                )
                stats.append({"op": typ, "edges": len(edges)})

            else:
                raise ValueError("Unsupported topology op: " + typ)

            bm.normal_update()

        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(o.data)
        o.data.update()
    finally:
        bm.free()

    return result(
        name=o.name,
        ops=stats,
        object=compact_obj(o),
        scene_h=scene_digest(),
    )


def op_sweep(p):
    path = [Vector(v) for v in p.get("path", [])]
    profile = p.get("profile", [])
    closed_path = bool(p.get("closed_path", False))
    closed_profile = bool(p.get("closed_profile", True))
    if len(path) < 2:
        raise ValueError("sweep_profile needs at least 2 path points")
    if len(profile) < 2:
        raise ValueError("sweep_profile needs at least 2 profile points")

    up_hint = Vector(p.get("up", [0, 0, 1]))
    if up_hint.length < 1e-9:
        up_hint = Vector((0, 0, 1))
    up_hint.normalize()

    verts = []
    plen = len(path)
    for i, point in enumerate(path):
        if closed_path:
            tangent = path[(i + 1) % plen] - path[(i - 1) % plen]
        elif i == 0:
            tangent = path[1] - path[0]
        elif i == plen - 1:
            tangent = path[-1] - path[-2]
        else:
            tangent = path[i + 1] - path[i - 1]

        if tangent.length < 1e-9:
            raise ValueError("Sweep path contains duplicate/degenerate points")
        tangent.normalize()

        side = tangent.cross(up_hint)
        if side.length < 1e-6:
            fallback = Vector((1, 0, 0)) if abs(tangent.x) < 0.9 else Vector((0, 1, 0))
            side = tangent.cross(fallback)
        side.normalize()
        up = side.cross(tangent).normalized()

        for x, y in profile:
            verts.append(tuple(point + side * float(x) + up * float(y)))

    ring = len(profile)
    faces = []
    span = plen if closed_path else plen - 1
    edge_span = ring if closed_profile else ring - 1
    for r in range(span):
        nr = (r + 1) % plen
        for j in range(edge_span):
            nj = (j + 1) % ring
            a0 = r * ring + j
            a1 = r * ring + nj
            b1 = nr * ring + nj
            b0 = nr * ring + j
            faces.append((a0, a1, b1, b0))

    if p.get("cap", True) and not closed_path and closed_profile:
        faces.append(tuple(reversed(range(0, ring))))
        end0 = (plen - 1) * ring
        faces.append(tuple(end0 + i for i in range(ring)))

    me = bpy.data.meshes.new(p["name"] + "Mesh")
    me.from_pydata(verts, [], faces)
    me.update()
    o = bpy.data.objects.new(p["name"], me)
    bpy.context.collection.objects.link(o)

    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.to_mesh(me)
    bm.free()
    me.update()

    return result(object=compact_obj(o), scene_h=scene_digest())


def op_radial_array(p):
    o = active(p.get("name", ""))
    count = max(1, int(p.get("count", 1)))
    if count == 1:
        return result(created=0, names=[], scene_h=scene_digest())

    axis_name = p.get("axis", "Z").upper()
    axis = {
        "X": Vector((1, 0, 0)),
        "Y": Vector((0, 1, 0)),
        "Z": Vector((0, 0, 1)),
    }[axis_name]
    center = Vector(p.get("center", [0, 0, 0]))
    total = math.radians(float(p.get("angle_degrees", 360.0)))
    linked = bool(p.get("linked", True))
    created = []

    original_matrix = o.matrix_world.copy()
    for i in range(1, count):
        angle = total * i / count
        n = o.copy()
        if o.data and not linked:
            n.data = o.data.copy()
        bpy.context.collection.objects.link(n)
        n.name = f"{o.name}_Radial_{i:02d}"
        rot = Matrix.Rotation(angle, 4, axis)
        n.matrix_world = Matrix.Translation(center) @ rot @ Matrix.Translation(-center) @ original_matrix
        created.append(n.name)

    return result(created=len(created), names=created[:64], scene_h=scene_digest())


def mesh_edit_batch(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")

    if p.get("checkpoint", True):
        bpy.ops.ed.undo_push(message="Smart MCP mesh batch")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    bm.normal_update()
    stats = []

    try:
        for op in p.get("ops", []):
            typ = op["op"]

            if typ == "bisect":
                geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
                cut = bmesh.ops.bisect_plane(
                    bm,
                    geom=geom,
                    dist=float(op.get("epsilon", 1e-6)),
                    plane_co=Vector(op.get("plane_co", [0, 0, 0])),
                    plane_no=Vector(op.get("plane_no", [0, 0, 1])),
                    clear_inner=bool(op.get("clear_inner", False)),
                    clear_outer=bool(op.get("clear_outer", False)),
                )
                stats.append({"op": typ, "cut": len(cut.get("geom_cut", []))})
                bm.normal_update()
                continue

            if typ == "merge_by_distance":
                before = len(bm.verts)
                bmesh.ops.remove_doubles(
                    bm,
                    verts=list(bm.verts),
                    dist=float(op.get("distance", 0.0001)),
                )
                stats.append({"op": typ, "merged": before - len(bm.verts)})
                bm.normal_update()
                continue

            if typ == "recalc_normals":
                bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
                stats.append({"op": typ})
                bm.normal_update()
                continue

            faces = _select_faces(bm, op.get("selector", {"region": "all"}))
            verts = _region_verts(faces)

            if typ == "extrude":
                count = _extrude(bm, faces, op)
                stats.append({"op": typ, "faces": len(faces), "new_v": count})

            elif typ == "inset":
                count = _inset(bm, faces, op)
                stats.append({"op": typ, "faces": len(faces), "new_f": count})

            elif typ == "translate":
                vec = Vector(op.get("vector", [0, 0, 0]))
                bmesh.ops.translate(bm, verts=verts, vec=vec)
                stats.append({"op": typ, "v": len(verts)})

            elif typ == "scale":
                factor = op.get("factor", 1.0)
                if isinstance(factor, (int, float)):
                    factor = [factor, factor, factor]
                pivot = Vector(op.get("pivot", _region_center(verts)))
                sx, sy, sz = [float(v) for v in factor]
                for v in verts:
                    d = v.co - pivot
                    v.co = pivot + Vector((d.x * sx, d.y * sy, d.z * sz))
                stats.append({"op": typ, "v": len(verts)})

            elif typ == "delete_faces":
                bmesh.ops.delete(bm, geom=faces, context="FACES_ONLY")
                stats.append({"op": typ, "f": len(faces)})

            elif typ == "subdivide":
                edges = list({e for f in faces for e in f.edges})
                res = bmesh.ops.subdivide_edges(
                    bm,
                    edges=edges,
                    cuts=max(1, int(op.get("cuts", 1))),
                    use_grid_fill=bool(op.get("grid_fill", True)),
                    smooth=float(op.get("smooth", 0.0)),
                )
                stats.append({"op": typ, "edges": len(edges), "new": len(res.get("geom_inner", []))})

            else:
                raise ValueError("Unsupported mesh op: " + typ)

            bm.normal_update()

        bm.to_mesh(o.data)
        o.data.update()

    finally:
        bm.free()

    return result(
        name=o.name,
        ops=stats,
        object=compact_obj(o),
        scene_h=scene_digest(),
    )


def op_lathe(p):
    pts = p["points"]
    seg = max(3, int(p.get("segments", 64)))
    verts, faces = [], []
    for i in range(seg):
        a = 2 * math.pi * i / seg
        c, s = math.cos(a), math.sin(a)
        verts += [(r * c, r * s, z) for r, z in pts]
    n = len(pts)
    for i in range(seg):
        j = (i + 1) % seg
        for k in range(n - 1):
            faces.append((i * n + k, j * n + k, j * n + k + 1, i * n + k + 1))

    me = bpy.data.meshes.new(p["name"] + "Mesh")
    me.from_pydata(verts, [], faces)
    me.update()
    o = bpy.data.objects.new(p["name"], me)
    bpy.context.collection.objects.link(o)
    return result(object=compact_obj(o), scene_h=scene_digest())


def op_loft(p):
    sections = p.get("sections", [])
    if len(sections) < 2:
        raise ValueError("loft_sections needs at least 2 sections")
    point_count = len(sections[0].get("points", []))
    if point_count < 3:
        raise ValueError("Each loft section needs at least 3 points")
    if any(len(s.get("points", [])) != point_count for s in sections):
        raise ValueError("All loft sections must have the same point count")

    verts = []
    faces = []
    for section in sections:
        z = float(section["z"])
        verts.extend((float(x), float(y), z) for x, y in section["points"])

    rings = len(sections)
    for r in range(rings - 1):
        a0 = r * point_count
        b0 = (r + 1) * point_count
        for i in range(point_count):
            j = (i + 1) % point_count
            faces.append((a0 + i, a0 + j, b0 + j, b0 + i))

    if p.get("cap", True):
        faces.append(tuple(reversed(range(0, point_count))))
        top0 = (rings - 1) * point_count
        faces.append(tuple(top0 + i for i in range(point_count)))

    me = bpy.data.meshes.new(p["name"] + "Mesh")
    me.from_pydata(verts, [], faces)
    me.update()
    o = bpy.data.objects.new(p["name"], me)
    bpy.context.collection.objects.link(o)

    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.to_mesh(me)
    bm.free()
    me.update()

    return result(object=compact_obj(o), scene_h=scene_digest())


def op_curve(p):
    cu = bpy.data.curves.new(p["name"] + "Curve", "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = p["radius"]
    cu.bevel_resolution = p["resolution"]
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(len(p["points"]) - 1)
    for b, co in zip(sp.bezier_points, p["points"]):
        b.co = co
        b.handle_left_type = "AUTO"
        b.handle_right_type = "AUTO"
    sp.use_cyclic_u = p["cyclic"]
    o = bpy.data.objects.new(p["name"], cu)
    bpy.context.collection.objects.link(o)
    return result(object=compact_obj(o), scene_h=scene_digest())


def validate(p):
    o = active(p.get("name", ""))
    if o.type != "MESH":
        return result(False, error="Not a mesh")

    bm = bmesh.new()
    bm.from_mesh(o.data)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    nonman = sum(1 for e in bm.edges if not e.is_manifold)
    loose = sum(1 for v in bm.verts if not v.link_edges)

    if p.get("repair"):
        loose_verts = [v for v in bm.verts if not v.link_edges]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(o.data)
        o.data.update()

    out = result(
        name=o.name,
        v=len(bm.verts),
        e=len(bm.edges),
        f=len(bm.faces),
        boundary=boundary,
        nonmanifold=nonman,
        loose=loose,
        object_h=object_digest(o),
    )
    bm.free()
    return out


def dispatch(a, p):
    if a == "scene_state":
        h = scene_digest()
        if p.get("changed_since") and p["changed_since"] == h:
            return result(changed=False, scene_h=h)
        obs = [compact_obj(o) for o in bpy.context.scene.objects]
        return result(
            changed=True,
            objects=obs,
            active=bpy.context.active_object.name if bpy.context.active_object else None,
            count=len(obs),
            scene_h=h,
        )
    if a == "create_primitive":
        return op_create(p)
    if a == "model_batch":
        return do_batch(p)
    if a == "mesh_query":
        return mesh_query(p)
    if a == "mesh_edit_batch":
        return mesh_edit_batch(p)
    if a == "edge_query":
        return edge_query(p)
    if a == "topology_state":
        return topology_state(p)
    if a == "topology_batch":
        return topology_batch(p)
    if a == "sweep_profile":
        return op_sweep(p)
    if a == "radial_array":
        return op_radial_array(p)
    if a == "lathe_profile":
        return op_lathe(p)
    if a == "loft_sections":
        return op_loft(p)
    if a == "curve_tube":
        return op_curve(p)
    if a == "mesh_validate":
        return validate(p)
    if a == "checkpoint":
        bpy.ops.ed.undo_push(message=p.get("label") or "Smart MCP")
        return result(checkpoint=True, scene_h=scene_digest())
    if a == "viewport_snapshot":
        path = bpy.path.abspath("//smart_mcp_preview.png")
        bpy.context.scene.render.filepath = path
        bpy.context.scene.render.resolution_x = p["width"]
        bpy.context.scene.render.resolution_y = p["height"]
        bpy.ops.render.render(write_still=True)
        return result(path=path, w=p["width"], h=p["height"], scene_h=scene_digest())
    raise ValueError("Unknown action")


def pump():
    try:
        while True:
            req, evt, box = _requests.get_nowait()
            try:
                box["v"] = dispatch(req["action"], req.get("params", {}))
            except Exception as e:
                box["v"] = result(False, error=str(e))
            evt.set()
    except queue.Empty:
        pass
    return 0.02 if _running else None


def recv_exact(c, n):
    b = bytearray()
    while len(b) < n:
        q = c.recv(n - len(b))
        if not q:
            raise ConnectionError()
        b.extend(q)
    return bytes(b)


def client(c):
    try:
        n = int.from_bytes(recv_exact(c, 4), "big")
        req = json.loads(recv_exact(c, n))
        evt = threading.Event()
        box = {}
        _requests.put((req, evt, box))
        evt.wait(60)
        raw = json.dumps(
            box.get("v", result(False, error="timeout")),
            separators=(",", ":"),
        ).encode()
        c.sendall(len(raw).to_bytes(4, "big") + raw)
    finally:
        c.close()


def loop():
    global _server
    s = socket.socket()
    _server = s
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(8)
    s.settimeout(1)
    while _running:
        try:
            c, _ = s.accept()
            threading.Thread(target=client, args=(c,), daemon=True).start()
        except socket.timeout:
            pass
        except OSError:
            break


def start():
    global _running
    if _running:
        return
    _running = True
    threading.Thread(target=loop, daemon=True).start()
    if not bpy.app.timers.is_registered(pump):
        bpy.app.timers.register(pump)


def stop():
    global _running
    _running = False
    if _server:
        try:
            _server.close()
        except Exception:
            pass


class SMARTMCP_OT_toggle(bpy.types.Operator):
    bl_idname = "smartmcp.toggle"
    bl_label = "Start / Stop"

    def execute(self, ctx):
        stop() if _running else start()
        return {"FINISHED"}


class SMARTMCP_PT_panel(bpy.types.Panel):
    bl_label = "Smart Modeling MCP"
    bl_idname = "SMARTMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Smart MCP"

    def draw(self, ctx):
        self.layout.label(text=("Running :9877" if _running else "Stopped"))
        self.layout.label(text="Advanced Topology V0.3")
        self.layout.operator("smartmcp.toggle")


classes = (SMARTMCP_OT_toggle, SMARTMCP_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    stop()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
