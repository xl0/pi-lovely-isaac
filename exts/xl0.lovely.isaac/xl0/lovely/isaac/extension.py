"""Kit extension entry point for the isaac-agent protocol server."""

import asyncio

import omni.ext

from .server import AgentServer


class AgentExtension(omni.ext.IExt):
    def on_startup(self, ext_id: str) -> None:
        self._server = AgentServer(ext_id)
        self._start_task = asyncio.ensure_future(self._server.start())

    def on_shutdown(self) -> None:
        if self._start_task is not None:
            self._start_task.cancel()
            self._start_task = None
        if self._server is not None:
            self._server.stop_sync()
            self._server = None
