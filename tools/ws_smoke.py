#!/usr/bin/env python
"""Quick WS smoke test against the isaac-agent server. Run with isaacsim env python."""

import asyncio
import glob
import json
import os

import websockets


def read_lock():
    locks = sorted(glob.glob(os.path.expanduser("~/.isaac-agent/*.lock")))
    assert locks, "no lockfile"
    with open(locks[-1]) as f:
        return json.load(f)


async def main():
    lock = read_lock()
    uri = f"ws://127.0.0.1:{lock['port']}"
    headers = {"X-Isaac-Agent-Authorization": lock["token"]}
    async with websockets.connect(uri, extra_headers=headers, max_size=2**26) as ws:
        rpc_id = 0

        async def call(method, params=None):
            nonlocal rpc_id
            rpc_id += 1
            await ws.send(
                json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}})
            )
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == rpc_id:
                    return msg

        hello = await call(
            "hello",
            {
                "protocolVersion": 1,
                "client": {"name": "ws-smoke", "version": "0.0.1", "pid": os.getpid()},
                "subscriptions": ["log", "timeline", "event"],
            },
        )
        r = hello["result"]
        print(
            "hello: isaac",
            r["server"]["isaacVersion"],
            "kit",
            r["server"]["kitVersion"],
            "ext",
            r["server"]["extensionVersion"],
            "stage",
            r["stage"]["path"],
            "docs",
            len(r["helperDocs"]),
            "chars",
        )

        print("ping:", (await call("ping"))["result"])
        print("exec 1+1:", (await call("exec", {"code": "1+1"}))["result"])
        print("exec print:", (await call("exec", {"code": "print('hi'); x=41"}))["result"])
        print("exec persist:", (await call("exec", {"code": "x+1"}))["result"])
        print(
            "exec err:",
            {k: v for k, v in (await call("exec", {"code": "1/0"}))["result"].items() if k != "traceback"},
        )
        print(
            "exec await:",
            (
                await call(
                    "exec",
                    {
                        "code": "import omni.kit.app\nawait omni.kit.app.get_app().next_update_async()\n'frame passed'"
                    },
                )
            )["result"],
        )
        st = (await call("exec", {"code": "agent.status()"}))["result"]
        print("status:", st)
        img = (await call("exec", {"code": "agent.image(await agent.viewport(width=320), name='vp')"}))[
            "result"
        ]
        media = img.get("media") or []
        print(
            "viewport media:",
            [(m["name"], m["mimeType"], len(m["data"])) for m in media],
            "status",
            img["status"],
        )
        if media:
            import base64

            png = base64.b64decode(media[0]["data"])
            print("png magic:", png[:8].hex(), "bytes", len(png))


asyncio.run(main())
