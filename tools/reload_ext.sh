#!/usr/bin/env bash
# Reload xl0.lovely.isaac in the running Isaac (dev loop; app config has no hot-reload).
# Prefers NVIDIA's 8226 exec bridge (Isaac 5.1 GUI); falls back to our own server:
# a detached task survives the exec connection and toggles the extension around us.
cd "$(dirname "$0")/.."
CODE='import omni.kit.app, sys; m=omni.kit.app.get_app().get_extension_manager(); m.set_extension_enabled_immediate("xl0.lovely.isaac", False); [sys.modules.pop(k) for k in list(sys.modules) if k.startswith("xl0.lovely.isaac")]; m.set_extension_enabled_immediate("xl0.lovely.isaac", True); print("reloaded")'
if python3 tools/kit_exec.py "$CODE" 2>/dev/null; then
  exit 0
fi
node cli/bin/isaac-cli.mjs exec "import asyncio, sys
async def _reload():
    await asyncio.sleep(0.5)
    import omni.kit.app
    m = omni.kit.app.get_app().get_extension_manager()
    m.set_extension_enabled_immediate('xl0.lovely.isaac', False)
    for k in [k for k in list(sys.modules) if k.startswith('xl0.lovely.isaac')]:
        sys.modules.pop(k)
    m.set_extension_enabled_immediate('xl0.lovely.isaac', True)
_reload_task = asyncio.ensure_future(_reload())
'reload scheduled'"
sleep 2
