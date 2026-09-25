bl_info = {
    "name": "Smart Modeling MCP",
    "author": "Yasscgi",
    "version": (0, 5, 1),
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
_last_error = ""
_self_test_status = "Not run"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def result(ok=True, **kw):
    return {"ok": ok, **kw}


def active(name=""):
    o = bpy.data.objects.get(name) if name else bpy.context.active_object
    if not o:
        raise ValueError("Object not found")
    current = bpy.context.object
    if current is not None and getattr(current, "mode", "OBJECT") != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
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
    if len(pts) < 2:
        raise ValueError("lathe_profile needs at least 2 profile points")
    verts, faces = [], []
    for i in range(seg):
        angle = 2 * math.pi * i / seg
        c, s = math.cos(angle), math.sin(angle)
        verts += [(float(r) * c, float(r) * s, float(z)) for r, z in pts]

    n = len(pts)
    for i in range(seg):
        j = (i + 1) % seg
        for k in range(n - 1):
            faces.append((i * n + k, j * n + k, j * n + k + 1, i * n + k + 1))

    if p.get("cap", True):
        if abs(float(pts[0][0])) > 1e-9:
            faces.append(tuple(reversed([i * n for i in range(seg)])))
        if abs(float(pts[-1][0])) > 1e-9:
            faces.append(tuple(i * n + (n - 1) for i in range(seg)))

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


def _view_points_mm(part, view, mode):
    pts = part.get("views", {}).get(view, [])
    if not pts:
        return []
    dims = part["dimensions_mm"]
    w = float(dims["width"])
    d = float(dims["depth"])
    h = float(dims["height"])
    if mode == "mm":
        return [[float(x), float(y)] for x, y in pts]
    if view == "front":
        return [[(float(u)-0.5)*w, (float(v)-0.5)*h] for u, v in pts]
    if view == "side":
        return [[(float(u)-0.5)*d, (float(v)-0.5)*h] for u, v in pts]
    if view == "top":
        return [[(float(u)-0.5)*w, (float(v)-0.5)*d] for u, v in pts]
    return []


def _make_prism(name, view, points, extent, collection):
    if len(points) < 3:
        raise ValueError(view + " silhouette requires at least 3 points")
    half = float(extent) * 0.55
    verts = []
    for layer in (-half, half):
        for a, b in points:
            if view == "front":
                verts.append((a, layer, b))
            elif view == "side":
                verts.append((layer, a, b))
            elif view == "top":
                verts.append((a, b, layer))
            else:
                raise ValueError("Unsupported orthographic view: " + view)

    n = len(points)
    faces = [tuple(reversed(range(n))), tuple(range(n, 2*n))]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n+j, n+i))

    me = bpy.data.meshes.new(name + "Mesh")
    me.from_pydata(verts, [], faces)
    me.update()
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)

    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.to_mesh(me)
    bm.free()
    me.update()
    return obj


def _boolean_intersect(base, cutter):
    active(base.name)
    mod = base.modifiers.new("PlanIntersect", "BOOLEAN")
    mod.operation = "INTERSECT"
    mod.solver = "EXACT"
    mod.object = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)


def _engineering_feature_object(feature, collection, index):
    ftype = feature.get("type", "")
    center = [float(v) for v in feature.get("center_mm", [0, 0, 0])]
    obj = None

    if ftype in {"hole_cylinder", "boss_cylinder"}:
        diameter = float(feature.get("diameter_mm", 1.0))
        depth = float(feature.get("depth_mm", 1.0))
        axis = str(feature.get("axis", "Z")).upper()
        rotation = (0.0, 0.0, 0.0)
        if axis == "X":
            rotation = (0.0, math.pi / 2.0, 0.0)
        elif axis == "Y":
            rotation = (math.pi / 2.0, 0.0, 0.0)
        elif axis != "Z":
            raise ValueError("Cylinder feature axis must be X, Y, or Z")
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=max(12, int(feature.get("segments", 48))),
            radius=diameter / 2.0,
            depth=depth,
            location=center,
            rotation=rotation,
        )
        obj = bpy.context.object

    elif ftype in {"cut_box", "boss_box"}:
        dims = feature.get("dimensions_mm", [1, 1, 1])
        if len(dims) != 3:
            raise ValueError("Box feature dimensions_mm must have 3 values")
        rot_deg = feature.get("rotation_deg", [0, 0, 0])
        rotation = tuple(math.radians(float(v)) for v in rot_deg)
        bpy.ops.mesh.primitive_cube_add(location=center, rotation=rotation)
        obj = bpy.context.object
        obj.dimensions = [float(v) for v in dims]
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    else:
        raise ValueError("Unsupported engineering feature: " + str(ftype))

    obj.name = "__ENG_FEATURE_%03d" % index
    # Move from the active scene collection to the reconstruction collection.
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    collection.objects.link(obj)
    return obj


def _expand_engineering_features(features):
    expanded = []
    for feature in features or []:
        pattern = feature.get("pattern")
        if not isinstance(pattern, dict):
            item = dict(feature)
            item.pop("pattern", None)
            expanded.append(item)
            continue

        ptype = str(pattern.get("type", ""))
        count = max(1, min(int(pattern.get("count", 1)), 256))
        base = dict(feature)
        base.pop("pattern", None)
        base_center = Vector(base.get("center_mm", [0, 0, 0]))

        if ptype == "linear":
            step = Vector(pattern.get("step_mm", [0, 0, 0]))
            for i in range(count):
                item = dict(base)
                item["center_mm"] = list(base_center + step * i)
                expanded.append(item)

        elif ptype == "radial":
            axis_name = str(pattern.get("axis", "Z")).upper()
            axis = {
                "X": Vector((1, 0, 0)),
                "Y": Vector((0, 1, 0)),
                "Z": Vector((0, 0, 1)),
            }.get(axis_name)
            if axis is None:
                raise ValueError("Radial pattern axis must be X/Y/Z")
            origin = Vector(pattern.get("center_mm", [0, 0, 0]))
            total = math.radians(float(pattern.get("angle_degrees", 360.0)))
            for i in range(count):
                angle = total * i / count
                rot = Matrix.Rotation(angle, 4, axis)
                item = dict(base)
                item["center_mm"] = list(origin + rot @ (base_center - origin))
                if "rotation_deg" in item:
                    rr = list(item.get("rotation_deg", [0, 0, 0]))
                    axis_i = {"X": 0, "Y": 1, "Z": 2}[axis_name]
                    rr[axis_i] = float(rr[axis_i]) + math.degrees(angle)
                    item["rotation_deg"] = rr
                expanded.append(item)

        else:
            raise ValueError("Unsupported feature pattern: " + ptype)

    return expanded


def _apply_engineering_features(base, features, collection, cleanup=True):
    applied = []
    for i, feature in enumerate(_expand_engineering_features(features)):
        ftype = feature.get("type", "")
        tool = _engineering_feature_object(feature, collection, i)
        operation = "DIFFERENCE" if ftype in {"hole_cylinder", "cut_box"} else "UNION"
        try:
            active(base.name)
            mod = base.modifiers.new("EngineeringFeature", "BOOLEAN")
            mod.operation = operation
            mod.solver = "EXACT"
            mod.object = tool
            bpy.ops.object.modifier_apply(modifier=mod.name)
            applied.append(ftype)
        finally:
            if cleanup and tool.name in bpy.data.objects:
                bpy.data.objects.remove(tool, do_unlink=True)
    return applied


def _engineering_quality(obj, target_dims):
    if obj.type == "MESH" and len(obj.data.vertices):
        xs = [float(v.co.x) for v in obj.data.vertices]
        ys = [float(v.co.y) for v in obj.data.vertices]
        zs = [float(v.co.z) for v in obj.data.vertices]
        actual = [max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)]
    else:
        actual = [float(v) for v in obj.dimensions]
    target = [float(v) for v in target_dims]
    errors = [
        round(abs(a - t) / t * 100.0, 3) if t > 1e-9 else 0.0
        for a, t in zip(actual, target)
    ]

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    nonmanifold = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()

    return {
        "actual_mm": [round(v, 3) for v in actual],
        "target_mm": [round(v, 3) for v in target],
        "dimension_error_pct": errors,
        "max_dimension_error_pct": round(max(errors), 3),
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
    }


def _apply_boolean_feature_to_object(base, feature, collection, operation, cleanup=True, index=0):
    tool = _engineering_feature_object(feature, collection, index)
    try:
        active(base.name)
        mod = base.modifiers.new("AssemblyFeature", "BOOLEAN")
        mod.operation = operation
        mod.solver = "EXACT"
        mod.object = tool
        bpy.ops.object.modifier_apply(modifier=mod.name)
    finally:
        if cleanup and tool.name in bpy.data.objects:
            bpy.data.objects.remove(tool, do_unlink=True)


def _apply_assembly_links(spec, objects_by_id, collection, cleanup=True):
    reports = []
    for i, link in enumerate(spec.get("assembly_links", []) or []):
        if link.get("type") != "peg_socket":
            reports.append({"i": i, "ok": False, "error": "unsupported link type"})
            continue

        from_id = str(link.get("from", ""))
        to_id = str(link.get("to", ""))
        peg_obj = objects_by_id.get(from_id)
        socket_obj = objects_by_id.get(to_id)
        if not peg_obj or not socket_obj:
            reports.append({"i": i, "ok": False, "error": "missing from/to object"})
            continue

        axis = str(link.get("axis", "Z")).upper()
        diameter = float(link.get("diameter_mm", 4.0))
        depth = float(link.get("depth_mm", 6.0))
        clearance = max(0.0, float(link.get("clearance_mm", 0.25)))
        from_local = Vector(link.get("from_center_mm", [0, 0, 0]))
        to_local = Vector(link.get("to_center_mm", [0, 0, 0]))

        peg_feature = {
            "type": "boss_cylinder",
            "axis": axis,
            "center_mm": list(peg_obj.location + from_local),
            "diameter_mm": diameter,
            "depth_mm": depth,
            "segments": int(link.get("segments", 48)),
        }
        socket_feature = {
            "type": "hole_cylinder",
            "axis": axis,
            "center_mm": list(socket_obj.location + to_local),
            "diameter_mm": diameter + 2.0 * clearance,
            "depth_mm": depth + max(0.5, clearance * 2.0),
            "segments": int(link.get("segments", 48)),
        }

        try:
            _apply_boolean_feature_to_object(
                peg_obj, peg_feature, collection, "UNION", cleanup=cleanup, index=i*2
            )
            _apply_boolean_feature_to_object(
                socket_obj, socket_feature, collection, "DIFFERENCE", cleanup=cleanup, index=i*2+1
            )
            reports.append({
                "i": i,
                "ok": True,
                "type": "peg_socket",
                "from": from_id,
                "to": to_id,
                "diameter_mm": diameter,
                "clearance_mm": clearance,
            })
        except Exception as exc:
            reports.append({"i": i, "ok": False, "error": str(exc)})

    return reports


def _move_to_collection(obj, collection):
    if collection not in obj.users_collection:
        collection.objects.link(obj)
    for col in list(obj.users_collection):
        if col != collection:
            col.objects.unlink(obj)
    return obj


def reconstruct_blueprint(p):
    spec = p.get("spec", {})
    all_parts = spec.get("parts", [])
    selected_ids = {str(v) for v in p.get("part_ids", [])}
    parts = [
        part for part in all_parts
        if not selected_ids or str(part.get("id", "")) in selected_ids
    ]
    if not parts:
        return result(False, error="Blueprint contains no selected parts")

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 0.001
    scene.unit_settings.length_unit = "MILLIMETERS"

    cname = p.get("collection_name", "Blueprint_Reconstruction")
    collection = bpy.data.collections.get(cname)
    if collection is None:
        collection = bpy.data.collections.new(cname)
        scene.collection.children.link(collection)

    mode = spec.get("coordinate_mode", "normalized")
    bevel = max(0.0, float(p.get("bevel_mm", 0.0)))
    cleanup = bool(p.get("cleanup", True))
    built = []
    warnings = []
    objects_by_id = {}

    if p.get("checkpoint", True):
        bpy.ops.ed.undo_push(message="Blueprint 2D to 3D reconstruction")

    for idx, part in enumerate(parts):
        pid = str(part.get("id") or f"P{idx+1:02d}")
        name = str(part.get("name") or pid)

        # Replace only objects previously generated for this stable part id.
        if bool(p.get("replace_existing", True)):
            stale = [
                obj for obj in list(collection.objects)
                if str(obj.get("smart_mcp_part_id", "")) == pid
            ]
            for obj in stale:
                bpy.data.objects.remove(obj, do_unlink=True)

        dims = part.get("dimensions_mm", {})
        try:
            w = float(dims["width"]); d = float(dims["depth"]); h = float(dims["height"])
        except Exception:
            warnings.append(pid + ": invalid dimensions")
            continue

        strategy = str(part.get("strategy", "orthographic_hull"))
        views_used = []

        if strategy == "orthographic_hull":
            view_defs = [
                ("front", d, _view_points_mm(part, "front", mode)),
                ("side", w, _view_points_mm(part, "side", mode)),
                ("top", h, _view_points_mm(part, "top", mode)),
            ]
            usable = [(view, ext, pts) for view, ext, pts in view_defs if len(pts) >= 3]
            if not usable:
                warnings.append(pid + ": no usable orthographic silhouettes")
                continue

            view, ext, pts = usable[0]
            base = _make_prism(pid + "_" + view, view, pts, ext, collection)
            views_used = [v[0] for v in usable]

            for view, ext, pts in usable[1:]:
                cutter = _make_prism(pid + "__" + view, view, pts, ext, collection)
                try:
                    _boolean_intersect(base, cutter)
                finally:
                    if cleanup and cutter.name in bpy.data.objects:
                        bpy.data.objects.remove(cutter, do_unlink=True)

        elif strategy == "lathe":
            temp_name = "__" + pid + "_LATHE"
            op_lathe({
                "name": temp_name,
                "points": part.get("profile", []),
                "segments": int(part.get("segments", 64)),
                "cap": bool(part.get("cap", True)),
            })
            base = bpy.data.objects.get(temp_name)
            if not base:
                raise ValueError(pid + ": lathe generation failed")
            _move_to_collection(base, collection)

        elif strategy == "loft":
            temp_name = "__" + pid + "_LOFT"
            op_loft({
                "name": temp_name,
                "sections": part.get("sections", []),
                "cap": bool(part.get("cap", True)),
            })
            base = bpy.data.objects.get(temp_name)
            if not base:
                raise ValueError(pid + ": loft generation failed")
            _move_to_collection(base, collection)

        elif strategy == "sweep":
            temp_name = "__" + pid + "_SWEEP"
            op_sweep({
                "name": temp_name,
                "path": part.get("path", []),
                "profile": part.get("profile", []),
                "closed_path": bool(part.get("closed_path", False)),
                "closed_profile": bool(part.get("closed_profile", True)),
                "cap": bool(part.get("cap", True)),
                "up": part.get("up", [0, 0, 1]),
            })
            base = bpy.data.objects.get(temp_name)
            if not base:
                raise ValueError(pid + ": sweep generation failed")
            _move_to_collection(base, collection)

        else:
            warnings.append(pid + ": unsupported strategy " + strategy)
            continue

        base.name = name
        base["smart_mcp_part_id"] = pid
        base["smart_mcp_model_id"] = str(spec.get("model_id", "model"))

        feature_names = []
        try:
            feature_names = _apply_engineering_features(
                base,
                part.get("features", []),
                collection,
                cleanup=cleanup,
            )
        except Exception as exc:
            warnings.append(pid + ": feature error: " + str(exc))

        pos = part.get("position_mm", [0, 0, 0])
        if len(pos) == 3:
            base.location = [float(v) for v in pos]
        rot_deg = part.get("rotation_deg", [0, 0, 0])
        if isinstance(rot_deg, list) and len(rot_deg) == 3:
            base.rotation_euler = [math.radians(float(v)) for v in rot_deg]

        objects_by_id[pid] = base

        if bevel > 0:
            active(base.name)
            mod = base.modifiers.new("EngineeringBevel", "BEVEL")
            mod.width = bevel
            mod.segments = max(1, int(part.get("bevel_segments", 2)))

        if bool(part.get("smooth", False)):
            for poly in base.data.polygons:
                poly.use_smooth = True

        quality = _engineering_quality(base, [w, d, h])
        built.append({
            "id": pid,
            "name": base.name,
            "strategy": strategy,
            "views": views_used,
            "features": feature_names,
            "quality": quality,
            "object": compact_obj(base),
        })

    if not built:
        return result(False, error="No parts could be reconstructed", warnings=warnings)

    link_spec = dict(spec)
    link_spec["assembly_links"] = [
        link for link in spec.get("assembly_links", []) or []
        if str(link.get("from", "")) in objects_by_id
        and str(link.get("to", "")) in objects_by_id
    ]
    assembly = _apply_assembly_links(
        link_spec,
        objects_by_id,
        collection,
        cleanup=cleanup,
    )

    # Recompute quality after assembly booleans.
    for item in built:
        obj = objects_by_id.get(item["id"])
        if obj:
            dims = next(
                (p0.get("dimensions_mm", {}) for p0 in parts if str(p0.get("id")) == item["id"]),
                {},
            )
            if all(k in dims for k in ("width", "depth", "height")):
                item["quality"] = _engineering_quality(
                    obj,
                    [dims["width"], dims["depth"], dims["height"]],
                )
            item["object"] = compact_obj(obj)

    return result(
        model_id=spec.get("model_id", "model"),
        built=len(built),
        parts=built[:64],
        assembly=assembly[:64],
        warnings=warnings,
        collection=cname,
        scene_h=scene_digest(),
    )


def setup_engineering_cameras(p):
    dims = p.get("dimensions_mm", {})
    w = float(dims.get("width", 100))
    d = float(dims.get("depth", 100))
    h = float(dims.get("height", 100))
    center = Vector(p.get("center_mm", [0, 0, 0]))
    margin = max(1.0, float(p.get("margin", 1.15)))
    dist = max(w, d, h) * 2.5 + 1.0

    specs = {
        "Front": (Vector((0, -dist, 0)), max(w, h)),
        "Back": (Vector((0, dist, 0)), max(w, h)),
        "Left": (Vector((-dist, 0, 0)), max(d, h)),
        "Right": (Vector((dist, 0, 0)), max(d, h)),
        "Top": (Vector((0, 0, dist)), max(w, d)),
        "Bottom": (Vector((0, 0, -dist)), max(w, d)),
    }

    names = []
    for label, (offset, scale) in specs.items():
        cam_name = "ENG_" + label
        cam_obj = bpy.data.objects.get(cam_name)
        if cam_obj is None or cam_obj.type != "CAMERA":
            cam_data = bpy.data.cameras.new(cam_name + "_Camera")
            cam_obj = bpy.data.objects.new(cam_name, cam_data)
            bpy.context.scene.collection.objects.link(cam_obj)
        cam_obj.location = center + offset
        direction = center - cam_obj.location
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        cam_obj.data.type = "ORTHO"
        cam_obj.data.ortho_scale = scale * margin
        names.append(cam_name)

    return result(cameras=names, center=_round3(center), scene_h=scene_digest())


def _engineering_targets(part_ids):
    wanted = {str(v) for v in (part_ids or [])}
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not wanted:
        generated = [o for o in meshes if o.get("smart_mcp_part_id")]
        return generated or meshes
    return [o for o in meshes if str(o.get("smart_mcp_part_id", "")) in wanted]


def _world_bounds(objects):
    points = []
    for obj in objects:
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ Vector(corner))
    if not points:
        raise ValueError("No mesh objects available for engineering preview")
    lo = Vector((
        min(p.x for p in points),
        min(p.y for p in points),
        min(p.z for p in points),
    ))
    hi = Vector((
        max(p.x for p in points),
        max(p.y for p in points),
        max(p.z for p in points),
    ))
    return lo, hi


def engineering_contact_sheet(p):
    size = max(128, min(int(p.get("size", 384)), 1024))
    targets = _engineering_targets(p.get("part_ids", []))
    lo, hi = _world_bounds(targets)
    center = (lo + hi) * 0.5
    dims = hi - lo

    setup_engineering_cameras({
        "dimensions_mm": {
            "width": max(float(dims.x), 1.0),
            "depth": max(float(dims.y), 1.0),
            "height": max(float(dims.z), 1.0),
        },
        "center_mm": list(center),
        "margin": float(p.get("margin", 1.15)),
    })

    scene = bpy.context.scene
    old_camera = scene.camera
    old_x = scene.render.resolution_x
    old_y = scene.render.resolution_y
    old_pct = scene.render.resolution_percentage
    old_filepath = scene.render.filepath

    hidden = {}
    if p.get("part_ids"):
        target_set = set(targets)
        for obj in scene.objects:
            if obj.type == "MESH":
                hidden[obj.name] = obj.hide_render
                obj.hide_render = obj not in target_set

    layout = [
        ("Front", 0, 1),
        ("Back", 1, 1),
        ("Left", 2, 1),
        ("Right", 0, 0),
        ("Top", 1, 0),
        ("Bottom", 2, 0),
    ]
    sheet_w, sheet_h = size * 3, size * 2
    sheet_pixels = [0.0] * (sheet_w * sheet_h * 4)

    try:
        scene.render.resolution_x = size
        scene.render.resolution_y = size
        scene.render.resolution_percentage = 100

        for label, tile_x, tile_y in layout:
            cam = bpy.data.objects.get("ENG_" + label)
            if not cam or cam.type != "CAMERA":
                raise ValueError("Missing engineering camera: " + label)
            scene.camera = cam
            bpy.ops.render.render()
            rr = bpy.data.images.get("Render Result")
            if rr is None:
                raise RuntimeError("Render Result unavailable")
            src = list(rr.pixels[:])
            row_len = size * 4
            for row in range(size):
                s0 = row * row_len
                d0 = ((tile_y * size + row) * sheet_w + tile_x * size) * 4
                sheet_pixels[d0:d0 + row_len] = src[s0:s0 + row_len]

        old_sheet = bpy.data.images.get("__SMART_ENGINEERING_SHEET__")
        if old_sheet:
            bpy.data.images.remove(old_sheet)

        sheet = bpy.data.images.new(
            "__SMART_ENGINEERING_SHEET__",
            width=sheet_w,
            height=sheet_h,
            alpha=True,
            float_buffer=False,
        )
        sheet.pixels[:] = sheet_pixels
        sheet.update()
        path = bpy.path.abspath("//smart_engineering_sheet.png")
        sheet.filepath_raw = path
        sheet.file_format = "PNG"
        sheet.save()

        return result(
            path=path,
            width=sheet_w,
            height=sheet_h,
            tile_size=size,
            layout=[x[0] for x in layout],
            parts=[str(o.get("smart_mcp_part_id", o.name)) for o in targets][:64],
            scene_h=scene_digest(),
        )
    finally:
        scene.camera = old_camera
        scene.render.resolution_x = old_x
        scene.render.resolution_y = old_y
        scene.render.resolution_percentage = old_pct
        scene.render.filepath = old_filepath
        for name, state in hidden.items():
            obj = bpy.data.objects.get(name)
            if obj:
                obj.hide_render = state

def _plan_points_normalized(part, view, mode):
    points = part.get("views", {}).get(view, [])
    if not points:
        return []
    if mode == "normalized":
        return [(float(u), float(v)) for u, v in points]

    dims = part.get("dimensions_mm", {})
    w = float(dims.get("width", 1.0))
    d = float(dims.get("depth", 1.0))
    h = float(dims.get("height", 1.0))
    out = []
    for a, b in points:
        a = float(a); b = float(b)
        if view == "front":
            out.append((a / w + 0.5, b / h + 0.5))
        elif view == "side":
            out.append((a / d + 0.5, b / h + 0.5))
        elif view == "top":
            out.append((a / w + 0.5, b / d + 0.5))
    return out


def _point_in_polygon_2d(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)):
            denom = (yj - yi)
            if abs(denom) < 1e-12:
                denom = 1e-12
            cross_x = (xj - xi) * (y - yi) / denom + xi
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def _point_in_triangle_2d(px, py, a, b, c):
    ax, ay = a; bx, by = b; cx, cy = c
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(den) < 1e-14:
        return False
    alpha = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
    beta = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
    gamma = 1.0 - alpha - beta
    eps = -1e-7
    return alpha >= eps and beta >= eps and gamma >= eps


def _raster_polygon(poly, resolution):
    mask = bytearray(resolution * resolution)
    if len(poly) < 3:
        return mask
    min_x = max(0, int(math.floor(min(p[0] for p in poly) * resolution)))
    max_x = min(resolution - 1, int(math.ceil(max(p[0] for p in poly) * resolution)))
    min_y = max(0, int(math.floor(min(p[1] for p in poly) * resolution)))
    max_y = min(resolution - 1, int(math.ceil(max(p[1] for p in poly) * resolution)))
    for iy in range(min_y, max_y + 1):
        y = (iy + 0.5) / resolution
        row = iy * resolution
        for ix in range(min_x, max_x + 1):
            x = (ix + 0.5) / resolution
            if _point_in_polygon_2d(x, y, poly):
                mask[row + ix] = 1
    return mask


def _project_local_vertex(co, view, dims):
    w, d, h = dims
    if view == "front":
        return (float(co.x) / w + 0.5, float(co.z) / h + 0.5)
    if view == "side":
        return (float(co.y) / d + 0.5, float(co.z) / h + 0.5)
    if view == "top":
        return (float(co.x) / w + 0.5, float(co.y) / d + 0.5)
    raise ValueError("Unsupported silhouette view: " + view)


def _raster_mesh_projection(obj, view, dims, resolution):
    mask = bytearray(resolution * resolution)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        projected = [_project_local_vertex(v.co, view, dims) for v in mesh.vertices]
        for tri in mesh.loop_triangles:
            a, b, c = [projected[i] for i in tri.vertices]
            min_x = max(0, int(math.floor(min(a[0], b[0], c[0]) * resolution)))
            max_x = min(resolution - 1, int(math.ceil(max(a[0], b[0], c[0]) * resolution)))
            min_y = max(0, int(math.floor(min(a[1], b[1], c[1]) * resolution)))
            max_y = min(resolution - 1, int(math.ceil(max(a[1], b[1], c[1]) * resolution)))
            if min_x > max_x or min_y > max_y:
                continue
            for iy in range(min_y, max_y + 1):
                py = (iy + 0.5) / resolution
                row = iy * resolution
                for ix in range(min_x, max_x + 1):
                    idx = row + ix
                    if mask[idx]:
                        continue
                    px = (ix + 0.5) / resolution
                    if _point_in_triangle_2d(px, py, a, b, c):
                        mask[idx] = 1
    finally:
        evaluated.to_mesh_clear()
    return mask


def silhouette_fit(p):
    part = p.get("part", {})
    part_id = str(p.get("part_id") or part.get("id", ""))
    if not part_id:
        return result(False, error="part_id is required")

    obj = next(
        (
            o for o in bpy.context.scene.objects
            if o.type == "MESH" and str(o.get("smart_mcp_part_id", "")) == part_id
        ),
        None,
    )
    if obj is None:
        return result(False, error="Reconstructed part not found: " + part_id)

    dims_d = part.get("dimensions_mm", {})
    try:
        dims = (
            float(dims_d["width"]),
            float(dims_d["depth"]),
            float(dims_d["height"]),
        )
        if min(dims) <= 0:
            raise ValueError
    except Exception:
        return result(False, error="Part dimensions_mm are invalid")

    resolution = max(24, min(int(p.get("resolution", 64)), 160))
    mode = str(p.get("coordinate_mode", "normalized"))
    scores = {}
    values = []

    for view in ("front", "side", "top"):
        target_poly = _plan_points_normalized(part, view, mode)
        if len(target_poly) < 3:
            continue
        target_mask = _raster_polygon(target_poly, resolution)
        mesh_mask = _raster_mesh_projection(obj, view, dims, resolution)
        intersection = sum(1 for a, b in zip(target_mask, mesh_mask) if a and b)
        union = sum(1 for a, b in zip(target_mask, mesh_mask) if a or b)
        target_px = sum(target_mask)
        mesh_px = sum(mesh_mask)
        iou = float(intersection) / union if union else 1.0
        scores[view] = {
            "iou": round(iou, 4),
            "target_px": target_px,
            "mesh_px": mesh_px,
        }
        values.append(iou)

    if not values:
        return result(False, error="No canonical plan silhouettes available for fit scoring")

    mean_iou = sum(values) / len(values)
    return result(
        part_id=part_id,
        resolution=resolution,
        views=scores,
        mean_iou=round(mean_iou, 4),
        fit_ready=mean_iou >= float(p.get("threshold", 0.90)),
        object_h=object_digest(obj),
    )


def viewport_snapshot(p):
    scene = bpy.context.scene
    width = max(64, min(int(p.get("width", 512)), 4096))
    height = max(64, min(int(p.get("height", 512)), 4096))

    old_camera = scene.camera
    old_x = scene.render.resolution_x
    old_y = scene.render.resolution_y
    old_pct = scene.render.resolution_percentage
    old_filepath = scene.render.filepath

    temp_camera = None
    try:
        if scene.camera is None:
            meshes = [o for o in scene.objects if o.type == "MESH" and not o.hide_render]
            if not meshes:
                raise ValueError("Preview requires a camera or at least one visible mesh")
            lo, hi = _world_bounds(meshes)
            center = (lo + hi) * 0.5
            dims = hi - lo
            dist = max(float(dims.x), float(dims.y), float(dims.z), 1.0) * 2.5
            cam_data = bpy.data.cameras.new("__SMART_PREVIEW_CAMERA_DATA__")
            temp_camera = bpy.data.objects.new("__SMART_PREVIEW_CAMERA__", cam_data)
            scene.collection.objects.link(temp_camera)
            temp_camera.location = center + Vector((dist, -dist, dist * 0.75))
            direction = center - temp_camera.location
            temp_camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            cam_data.type = "ORTHO"
            cam_data.ortho_scale = max(float(dims.x), float(dims.y), float(dims.z), 1.0) * 1.4
            scene.camera = temp_camera

        path = bpy.path.abspath("//smart_mcp_preview.png")
        scene.render.filepath = path
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        bpy.ops.render.render(write_still=True)
        return result(path=path, w=width, h=height, scene_h=scene_digest())
    finally:
        scene.camera = old_camera
        scene.render.resolution_x = old_x
        scene.render.resolution_y = old_y
        scene.render.resolution_percentage = old_pct
        scene.render.filepath = old_filepath
        if temp_camera is not None:
            cam_data = temp_camera.data
            bpy.data.objects.remove(temp_camera, do_unlink=True)
            if cam_data and cam_data.users == 0:
                bpy.data.cameras.remove(cam_data)


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


def smart_batch(p):
    steps = p.get("steps", [])
    if not isinstance(steps, list) or not steps:
        return result(False, error="smart_batch requires a non-empty steps list")

    use_checkpoint = bool(p.get("checkpoint", True))
    rollback = bool(p.get("rollback_on_error", True))
    return_steps = bool(p.get("return_steps", False))
    if use_checkpoint:
        bpy.ops.ed.undo_push(message="Smart MCP transaction")

    summaries = []
    for i, step in enumerate(steps):
        kind = step.get("do")
        payload = {k: v for k, v in step.items() if k != "do"}
        try:
            if kind == "create":
                out = op_create(payload)
            elif kind == "object":
                payload["checkpoint"] = False
                out = do_batch(payload)
            elif kind == "mesh":
                payload["checkpoint"] = False
                out = mesh_edit_batch(payload)
            elif kind == "topology":
                payload["checkpoint"] = False
                out = topology_batch(payload)
            elif kind == "lathe":
                out = op_lathe(payload)
            elif kind == "loft":
                out = op_loft(payload)
            elif kind == "sweep":
                out = op_sweep(payload)
            elif kind == "curve":
                out = op_curve(payload)
            elif kind == "radial":
                out = op_radial_array(payload)
            elif kind == "orthographic":
                payload["checkpoint"] = False
                out = reconstruct_blueprint(payload)
            elif kind == "cameras":
                out = setup_engineering_cameras(payload)
            elif kind == "validate":
                out = validate(payload)
            else:
                raise ValueError("Unknown smart_batch step: " + str(kind))

            if not out.get("ok", False):
                raise RuntimeError(out.get("error", "step failed"))

            if return_steps:
                summary = {"i": i, "do": kind, "ok": True}
                if "object" in out:
                    summary["object"] = out["object"]
                if "ops" in out:
                    summary["ops"] = out["ops"]
                if kind == "validate":
                    for key in ("v", "e", "f", "boundary", "nonmanifold", "loose"):
                        if key in out:
                            summary[key] = out[key]
                summaries.append(summary)

        except Exception as exc:
            rolled_back = False
            if rollback and use_checkpoint:
                try:
                    bpy.ops.ed.undo()
                    rolled_back = True
                except Exception:
                    rolled_back = False
            return result(
                False,
                error=str(exc),
                failed_step=i,
                failed_do=kind,
                rolled_back=rolled_back,
                scene_h=scene_digest(),
            )

    final_obj = bpy.context.active_object
    out = result(
        steps=len(steps),
        scene_h=scene_digest(),
        active=compact_obj(final_obj) if final_obj else None,
    )
    if return_steps:
        out["results"] = summaries
    return out


def dispatch(a, p):
    if a == "smart_batch":
        return smart_batch(p)
    if a == "reconstruct_blueprint":
        return reconstruct_blueprint(p)
    if a == "setup_engineering_cameras":
        return setup_engineering_cameras(p)
    if a == "engineering_contact_sheet":
        return engineering_contact_sheet(p)
    if a == "silhouette_fit":
        return silhouette_fit(p)
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
        return viewport_snapshot(p)
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
        if n <= 0 or n > MAX_MESSAGE_BYTES:
            raise ValueError("Invalid MCP message size")
        req = json.loads(recv_exact(c, n))
        evt = threading.Event()
        box = {}
        _requests.put((req, evt, box))
        evt.wait(300)
        raw = json.dumps(
            box.get("v", result(False, error="timeout")),
            separators=(",", ":"),
        ).encode()
        c.sendall(len(raw).to_bytes(4, "big") + raw)
    finally:
        c.close()


def loop():
    global _running, _server, _last_error
    s = _server
    if s is None:
        return
    while _running:
        try:
            c, _ = s.accept()
            threading.Thread(target=client, args=(c,), daemon=True).start()
        except socket.timeout:
            pass
        except OSError as exc:
            if _running:
                _last_error = str(exc)
            break
    _running = False


def start():
    global _running, _server, _last_error
    if _running:
        return True
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(8)
        s.settimeout(1)
    except OSError as exc:
        _last_error = str(exc)
        try:
            s.close()
        except Exception:
            pass
        _server = None
        _running = False
        return False

    _server = s
    _last_error = ""
    _running = True
    threading.Thread(target=loop, daemon=True).start()
    if not bpy.app.timers.is_registered(pump):
        bpy.app.timers.register(pump)
    return True


def stop():
    global _running, _server
    _running = False
    if _server:
        try:
            _server.close()
        except Exception:
            pass
    _server = None


def run_self_test():
    global _self_test_status
    created = []
    try:
        if bpy.context.object is not None and getattr(bpy.context.object, "mode", "OBJECT") != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "__SMART_TEST_CUBE__"
        created.append(cube)

        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.45, depth=3.0, location=(0, 0, 0))
        cutter = bpy.context.object
        cutter.name = "__SMART_TEST_CUTTER__"
        created.append(cutter)

        active(cube.name)
        mod = cube.modifiers.new("__SMART_TEST_BOOLEAN__", "BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.solver = "EXACT"
        mod.object = cutter
        bpy.ops.object.modifier_apply(modifier=mod.name)

        bm = bmesh.new()
        bm.from_mesh(cube.data)
        faces = len(bm.faces)
        verts = len(bm.verts)
        bm.free()
        if faces <= 0 or verts <= 0:
            raise RuntimeError("Boolean self-test produced empty geometry")

        _self_test_status = "PASS - Boolean/mesh OK"
        return True, _self_test_status
    except Exception as exc:
        _self_test_status = "FAIL - " + str(exc)
        return False, _self_test_status
    finally:
        for obj in created:
            if obj and obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)


class SMARTMCP_OT_self_test(bpy.types.Operator):
    bl_idname = "smartmcp.self_test"
    bl_label = "Run Self Test"

    def execute(self, ctx):
        ok, message = run_self_test()
        self.report({"INFO"} if ok else {"ERROR"}, message)
        return {"FINISHED"}


class SMARTMCP_OT_toggle(bpy.types.Operator):
    bl_idname = "smartmcp.toggle"
    bl_label = "Start / Stop"

    def execute(self, ctx):
        if _running:
            stop()
            self.report({"INFO"}, "Smart MCP stopped")
        else:
            ok = start()
            if ok:
                self.report({"INFO"}, "Smart MCP listening on 127.0.0.1:9877")
            else:
                self.report({"ERROR"}, _last_error or "Failed to start Smart MCP")
        return {"FINISHED"}


class SMARTMCP_PT_panel(bpy.types.Panel):
    bl_label = "Smart Modeling MCP"
    bl_idname = "SMARTMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Smart MCP"

    def draw(self, ctx):
        self.layout.label(text=("Running 127.0.0.1:9877" if _running else "Stopped"))
        self.layout.label(text="AI Engineer V0.5.1")
        if _last_error:
            self.layout.label(text=_last_error[:80], icon="ERROR")
        self.layout.operator("smartmcp.toggle")
        self.layout.operator("smartmcp.self_test")
        self.layout.label(text=_self_test_status[:80])


classes = (SMARTMCP_OT_self_test, SMARTMCP_OT_toggle, SMARTMCP_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    stop()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
