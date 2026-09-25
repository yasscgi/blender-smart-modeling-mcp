bl_info = {
    "name": "Smart Modeling MCP", "author": "Yasscgi",
    "version": (0, 1, 0), "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Smart MCP", "category": "3D View",
}
import bpy, bmesh, json, socket, threading, queue, math
from mathutils import Vector

HOST, PORT = "127.0.0.1", 9877
_requests = queue.Queue()
_running = False
_server = None

def compact_obj(o):
    d={"n":o.name,"t":o.type,"l":[round(x,4) for x in o.location],
       "d":[round(x,4) for x in o.dimensions]}
    if o.type=="MESH":
        d["v"]=len(o.data.vertices); d["f"]=len(o.data.polygons)
        if o.modifiers: d["m"]=[m.type for m in o.modifiers]
    return d

def result(ok=True, **kw): return {"ok":ok, **kw}

def active(name=""):
    o=bpy.data.objects.get(name) if name else bpy.context.active_object
    if not o: raise ValueError("Object not found")
    bpy.context.view_layer.objects.active=o; o.select_set(True); return o

def op_create(p):
    kind=p["kind"].upper(); loc=p["location"]; q=p.get("params",{})
    fn={"CUBE":bpy.ops.mesh.primitive_cube_add,"CYLINDER":bpy.ops.mesh.primitive_cylinder_add,
        "SPHERE":bpy.ops.mesh.primitive_uv_sphere_add,"CONE":bpy.ops.mesh.primitive_cone_add,
        "TORUS":bpy.ops.mesh.primitive_torus_add,"PLANE":bpy.ops.mesh.primitive_plane_add}.get(kind)
    if not fn: raise ValueError("Unsupported primitive")
    args={"location":loc}
    if kind in {"CYLINDER","CONE"}: args["vertices"]=int(q.get("vertices",32))
    if kind=="SPHERE": args.update(segments=int(q.get("segments",32)), ring_count=int(q.get("rings",16)))
    fn(**args); o=bpy.context.object
    if p.get("name"): o.name=p["name"]
    o.scale=p["scale"]; bpy.ops.object.transform_apply(location=False,rotation=False,scale=True)
    return result(object=compact_obj(o))

def add_mod(o, typ, vals):
    m=o.modifiers.new(vals.get("name",typ.title()),typ)
    for k,v in vals.items():
        if k!="name" and hasattr(m,k): setattr(m,k,v)
    return m

def do_batch(p):
    if p.get("checkpoint",True): bpy.ops.ed.undo_push(message="Smart MCP batch")
    changed=[]
    for x in p["ops"]:
        typ=x["op"]; o=active(x.get("name",""))
        if typ=="transform":
            if "location" in x:o.location=x["location"]
            if "rotation" in x:o.rotation_euler=x["rotation"]
            if "scale" in x:o.scale=x["scale"]
        elif typ=="bevel": add_mod(o,"BEVEL",{"width":x.get("width",.01),"segments":x.get("segments",3)})
        elif typ=="solidify": add_mod(o,"SOLIDIFY",{"thickness":x.get("thickness",.01)})
        elif typ=="subdivide": add_mod(o,"SUBSURF",{"levels":x.get("levels",2),"render_levels":x.get("levels",2)})
        elif typ=="mirror":
            m=add_mod(o,"MIRROR",{}); m.use_axis[0]=x.get("x",True); m.use_axis[1]=x.get("y",False); m.use_axis[2]=x.get("z",False)
        elif typ=="boolean":
            target=bpy.data.objects.get(x["target"]); m=add_mod(o,"BOOLEAN",{})
            m.operation=x.get("operation","DIFFERENCE"); m.solver="EXACT"; m.object=target
        elif typ=="shade_smooth":
            for f in o.data.polygons: f.use_smooth=True
        elif typ=="apply_modifier":
            bpy.context.view_layer.objects.active=o; bpy.ops.object.modifier_apply(modifier=x["modifier"])
        elif typ=="duplicate":
            n=o.copy(); n.data=o.data.copy() if o.data else None; bpy.context.collection.objects.link(n); n.name=x.get("new_name",o.name+"_copy")
        elif typ=="delete":
            bpy.data.objects.remove(o,do_unlink=True)
        else: raise ValueError("Unsupported batch op: "+typ)
        changed.append(x.get("name",o.name))
    return result(changed=changed, count=len(changed))

def op_lathe(p):
    pts=p["points"]; seg=max(3,int(p.get("segments",64))); verts=[]; faces=[]
    # points are [radius,height], revolved around Z. Compact profile is far cheaper than raw mesh transfer.
    for i in range(seg):
        a=2*math.pi*i/seg; c,s=math.cos(a),math.sin(a)
        verts += [(r*c,r*s,z) for r,z in pts]
    n=len(pts)
    for i in range(seg):
        j=(i+1)%seg
        for k in range(n-1): faces.append((i*n+k,j*n+k,j*n+k+1,i*n+k+1))
    me=bpy.data.meshes.new(p["name"]+"Mesh"); me.from_pydata(verts,[],faces); me.update()
    o=bpy.data.objects.new(p["name"],me); bpy.context.collection.objects.link(o)
    return result(object=compact_obj(o))

def op_curve(p):
    cu=bpy.data.curves.new(p["name"]+"Curve","CURVE"); cu.dimensions="3D"
    cu.bevel_depth=p["radius"]; cu.bevel_resolution=p["resolution"]
    sp=cu.splines.new("BEZIER"); sp.bezier_points.add(len(p["points"])-1)
    for b,co in zip(sp.bezier_points,p["points"]): b.co=co; b.handle_left_type="AUTO"; b.handle_right_type="AUTO"
    sp.use_cyclic_u=p["cyclic"]; o=bpy.data.objects.new(p["name"],cu); bpy.context.collection.objects.link(o)
    return result(object=compact_obj(o))

def validate(p):
    o=active(p.get("name","")); 
    if o.type!="MESH": return result(False,error="Not a mesh")
    bm=bmesh.new(); bm.from_mesh(o.data)
    boundary=sum(1 for e in bm.edges if e.is_boundary); nonman=sum(1 for e in bm.edges if not e.is_manifold)
    loose=sum(1 for v in bm.verts if not v.link_edges)
    if p.get("repair"):
        bmesh.ops.delete(bm,geom=[v for v in bm.verts if not v.link_edges],context="VERTS")
        bmesh.ops.recalc_face_normals(bm,faces=bm.faces); bm.to_mesh(o.data); o.data.update()
    out=result(name=o.name,v=len(bm.verts),e=len(bm.edges),f=len(bm.faces),boundary=boundary,nonmanifold=nonman,loose=loose)
    bm.free(); return out

def dispatch(a,p):
    if a=="scene_state":
        obs=[compact_obj(o) for o in bpy.context.scene.objects]
        return result(objects=obs,active=bpy.context.active_object.name if bpy.context.active_object else None,count=len(obs))
    if a=="create_primitive": return op_create(p)
    if a=="model_batch": return do_batch(p)
    if a=="lathe_profile": return op_lathe(p)
    if a=="curve_tube": return op_curve(p)
    if a=="mesh_validate": return validate(p)
    if a=="checkpoint": bpy.ops.ed.undo_push(message=p.get("label") or "Smart MCP"); return result(checkpoint=True)
    if a=="viewport_snapshot":
        path=bpy.path.abspath("//smart_mcp_preview.png"); bpy.context.scene.render.filepath=path
        bpy.context.scene.render.resolution_x=p["width"]; bpy.context.scene.render.resolution_y=p["height"]
        bpy.ops.render.render(write_still=True); return result(path=path,w=p["width"],h=p["height"])
    raise ValueError("Unknown action")

def pump():
    try:
        while True:
            req,evt,box=_requests.get_nowait()
            try: box["v"]=dispatch(req["action"],req.get("params",{}))
            except Exception as e: box["v"]=result(False,error=str(e))
            evt.set()
    except queue.Empty: pass
    return .02 if _running else None

def recv_exact(c,n):
    b=bytearray()
    while len(b)<n:
        q=c.recv(n-len(b))
        if not q: raise ConnectionError()
        b.extend(q)
    return bytes(b)

def client(c):
    try:
        n=int.from_bytes(recv_exact(c,4),"big"); req=json.loads(recv_exact(c,n)); evt=threading.Event(); box={}
        _requests.put((req,evt,box)); evt.wait(60)
        raw=json.dumps(box.get("v",result(False,error="timeout")),separators=(",",":")).encode()
        c.sendall(len(raw).to_bytes(4,"big")+raw)
    finally: c.close()

def loop():
    global _server
    s=socket.socket(); _server=s; s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((HOST,PORT)); s.listen(8); s.settimeout(1)
    while _running:
        try: c,_=s.accept(); threading.Thread(target=client,args=(c,),daemon=True).start()
        except socket.timeout: pass
        except OSError: break

def start():
    global _running
    if _running:return
    _running=True; threading.Thread(target=loop,daemon=True).start(); bpy.app.timers.register(pump)

def stop():
    global _running
    _running=False
    if _server:
        try:_server.close()
        except:pass

class SMARTMCP_OT_toggle(bpy.types.Operator):
    bl_idname="smartmcp.toggle"; bl_label="Start / Stop"
    def execute(self,ctx):
        stop() if _running else start(); return {"FINISHED"}

class SMARTMCP_PT_panel(bpy.types.Panel):
    bl_label="Smart Modeling MCP"; bl_idname="SMARTMCP_PT_panel"; bl_space_type="VIEW_3D"; bl_region_type="UI"; bl_category="Smart MCP"
    def draw(self,ctx):
        self.layout.label(text=("Running :9877" if _running else "Stopped")); self.layout.operator("smartmcp.toggle")

classes=(SMARTMCP_OT_toggle,SMARTMCP_PT_panel)
def register():
    for c in classes:bpy.utils.register_class(c)
def unregister():
    stop()
    for c in reversed(classes):bpy.utils.unregister_class(c)
if __name__=="__main__": register()
