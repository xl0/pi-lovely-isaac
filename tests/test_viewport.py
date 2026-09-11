# ruff: noqa: F811
import asyncio
import sys
import types
from unittest.mock import Mock

import numpy as np
import pytest
from test_server import helpers as _helpers  # noqa: F401 - imported pytest fixture


@pytest.mark.parametrize(
    ("kwargs", "shape"),
    [
        ({"width": 960}, (540, 960, 4)),
        ({"height": 360}, (360, 640, 4)),
        ({"width": 960, "height": 720}, (540, 960, 4)),
        ({"width": 960, "height": 360}, (360, 640, 4)),
        ({"width": 1, "height": 1}, (1, 1, 4)),
    ],
)
def test_active_viewport_sizing_preserves_aspect(_helpers, kwargs, shape):
    async def capture():
        return np.zeros((720, 1280, 4), dtype=np.uint8)

    agent = _helpers.Agent(None)
    agent._capture_active_viewport = capture

    assert asyncio.run(agent.viewport(**kwargs)).shape == shape


@pytest.mark.parametrize("kwargs", [{"width": 0}, {"height": -1}, {"width": 1.5}, {"height": True}])
def test_viewport_size_validation_precedes_native_capture(_helpers, kwargs):
    agent = _helpers.Agent(None)
    agent._capture_active_viewport = Mock()

    with pytest.raises(ValueError, match="must be a positive integer"):
        asyncio.run(agent.viewport(**kwargs))

    agent._capture_active_viewport.assert_not_called()


def test_offscreen_camera_dimensions_still_pass_through_to_camera_capture(_helpers):
    seen = []

    async def capture(camera, width, height):
        seen.append((camera, width, height))
        return np.zeros((1, 1, 4), dtype=np.uint8)

    agent = _helpers.Agent(None)
    agent._capture_camera = capture

    asyncio.run(agent.viewport(camera="/Camera", width=960, height=720))

    assert seen == [("/Camera", 960, 720)]


@pytest.mark.parametrize("initialized", [False, True])
def test_active_viewport_timeout_reports_loaded_diagnostics(_helpers, monkeypatch, initialized):
    settings = {"/app/asyncRendering": False, "/rtx/ecoMode/enabled": True}
    _helpers.omni.kit.viewport.utility.get_active_viewport.return_value = types.SimpleNamespace(
        camera_path="/World/ViewerCamera",
        updates_enabled=False,
        fps=12.5,
    )
    _helpers.omni.kit.viewport.utility.capture_viewport_to_buffer.side_effect = lambda _vp, _cb: None
    monkeypatch.setattr(_helpers, "_ACTIVE_VIEWPORT_CAPTURE_TIMEOUT_S", 0)
    sys.modules["carb.settings"].get_settings = lambda: types.SimpleNamespace(get=settings.get)
    get_status = (
        Mock(return_value="STEPPED") if initialized else Mock(side_effect=AttributeError("not initialized"))
    )
    sys.modules["omni.replicator.core"].orchestrator = types.SimpleNamespace(get_status=get_status)
    _helpers.omni.timeline.get_timeline_interface = lambda: types.SimpleNamespace(is_playing=lambda: True)

    with pytest.raises(RuntimeError) as e:
        asyncio.run(_helpers.Agent(None)._capture_active_viewport())

    message = str(e.value)
    assert "active viewport capture timed out after 0 s" in message
    assert "camera='/World/ViewerCamera'" in message
    assert "updates_enabled=False" in message
    expected = "STEPPED" if initialized else "unavailable: orchestrator not initialized"
    assert f"replicator_status={expected!r}" in message
    assert "asyncRendering=False" in message
    assert "eco=True" in message
    assert "timeline_playing=True" in message
    assert "fps=12.5" in message


def test_active_viewport_timeout_does_not_import_unloaded_replicator(_helpers, monkeypatch):
    _helpers.omni.kit.viewport.utility.get_active_viewport.return_value = types.SimpleNamespace(
        camera_path="/Camera",
        updates_enabled=True,
    )
    _helpers.omni.kit.viewport.utility.capture_viewport_to_buffer.side_effect = lambda _vp, _cb: None
    monkeypatch.setattr(_helpers, "_ACTIVE_VIEWPORT_CAPTURE_TIMEOUT_S", 0)
    monkeypatch.delitem(sys.modules, "omni.replicator.core", raising=False)
    settings = {"/app/asyncRendering": True, "/rtx/ecoMode/enabled": True}
    sys.modules["carb.settings"].get_settings = lambda: types.SimpleNamespace(get=settings.get)
    _helpers.omni.timeline.get_timeline_interface = lambda: types.SimpleNamespace(is_playing=lambda: False)

    with pytest.raises(RuntimeError) as e:
        asyncio.run(_helpers.Agent(None)._capture_active_viewport())

    assert "replicator_status='unavailable: module not loaded'" in str(e.value)
    assert "omni.replicator.core" not in sys.modules
