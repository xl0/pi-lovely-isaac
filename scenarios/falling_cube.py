# Scenario: dynamic cube drops onto the ground; verify physics state via agent.state.
import omni.usd
from pxr import Gf, UsdGeom, UsdLux, UsdPhysics

stage = omni.usd.get_context().get_stage()
agent.stop()

if stage.GetPrimAtPath("/World/AgentDemo"):
    stage.RemovePrim("/World/AgentDemo")

if not any(p.IsA(UsdPhysics.Scene) for p in stage.Traverse()):
    UsdPhysics.Scene.Define(stage, "/World/physicsScene")

UsdGeom.Xform.Define(stage, "/World/AgentDemo")
light = UsdLux.DistantLight.Define(stage, "/World/AgentDemo/Sun")
light.CreateIntensityAttr(3000.0)

ground = UsdGeom.Cube.Define(stage, "/World/AgentDemo/Ground")
ground.GetSizeAttr().Set(1.0)
api = UsdGeom.XformCommonAPI(ground)
api.SetScale(Gf.Vec3f(20, 20, 0.1))
api.SetTranslate(Gf.Vec3d(0, 0, -0.05))
UsdPhysics.CollisionAPI.Apply(ground.GetPrim())

cube = UsdGeom.Cube.Define(stage, "/World/AgentDemo/Cube")
cube.GetSizeAttr().Set(0.5)
UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(0, 0, 3.0))
UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())

start = agent.state("/World/AgentDemo/Cube")
f"scene ready, cube at z={start['/World/AgentDemo/Cube']['pose']['pos'][2]}"
