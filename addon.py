bl_info = {
    "name": "Smart Modeling MCP",
    "author": "Yasscgi",
    "version": (0, 2, 0),
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
from mathutils import Vector

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
        self.layout.label(text="Semantic Modeling V0.2")
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
