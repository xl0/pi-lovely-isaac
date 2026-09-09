# Scenario: physics-step callback measuring a bouncing cube, telemetry via agent.emit.
# Run falling_cube.py first (fresh). Pattern: register callback -> play -> inspect live.
import omni.physx
import omni.usd
from pxr import UsdPhysics, UsdShade

stage = omni.usd.get_context().get_stage()
agent.stop()  # restores authored transforms (cube back to z=3)

cube = stage.GetPrimAtPath("/World/AgentDemo/Cube")
ground = stage.GetPrimAtPath("/World/AgentDemo/Ground")
assert cube and ground, "run falling_cube.py first"

mat = UsdShade.Material.Define(stage, "/World/AgentDemo/BouncyMat")
pmat = UsdPhysics.MaterialAPI.Apply(mat.GetPrim())
pmat.CreateRestitutionAttr(0.9)
for prim in (cube, ground):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat, materialPurpose="physics")

bounce = {"t": 0.0, "z": [], "peak_after_first_hit": 0.0, "hit": False}

def on_physics_step(dt):
    bounce["t"] += dt
    z = agent.state("/World/AgentDemo/Cube")["/World/AgentDemo/Cube"]["pose"]["pos"][2]
    prev = bounce["z"][-1] if bounce["z"] else z
    bounce["z"].append(z)
    if not bounce["hit"] and z < 0.3 and prev >= z:
        bounce["hit"] = True
        agent.emit("bounce.hit", {"t": round(bounce["t"], 3)})
    if bounce["hit"]:
        bounce["peak_after_first_hit"] = max(bounce["peak_after_first_hit"], z)
    if int(bounce["t"] * 10) != int((bounce["t"] - dt) * 10):
        agent.emit("bounce.telemetry", {"t": round(bounce["t"], 2), "z": round(z, 3)})

bounce_sub = omni.physx.get_physx_interface().subscribe_physics_step_events(on_physics_step)
agent.play()
"bouncing — check `bounce` dict in later execs; unsubscribe with `bounce_sub = None`"
