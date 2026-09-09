#!/usr/bin/env python3
"""Generate pyrightconfig.json (machine-local, gitignored) for this repo.

Kit assembles sys.path at runtime; this discovers the equivalent paths for static
analysis. Re-run after upgrading the isaacsim conda env (extension dirs are
version-pinned). Usage: python3 tools/gen_pyrightconfig.py [isaacsim-env-prefix]
"""

import glob
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# extensions the Kit extension imports from (resolved to newest match)
KIT_EXT_PREFIXES = [
    "omni.usd-",
    "omni.timeline-",
    "omni.kit.viewport.utility-",
    "omni.kit.widget.viewport-",
    "omni.replicator.core-",
    "omni.physx-",
]


def isaac_paths(env_prefix: str) -> list[str]:
    sp = glob.glob(os.path.join(env_prefix, "lib", "python3.*", "site-packages"))
    if not sp:
        sys.exit(f"no site-packages under {env_prefix}")
    isaac = os.path.join(sp[0], "isaacsim")
    paths = [os.path.join(isaac, "kit", "kernel", "py")]
    for prefix in KIT_EXT_PREFIXES:
        matches = sorted(
            glob.glob(os.path.join(isaac, "extscache", prefix + "*"))
            + glob.glob(os.path.join(isaac, "exts", prefix + "*"))
        )
        matches = [m for m in matches if os.path.isdir(m)]
        if matches:
            paths.append(matches[-1])
        else:
            print(f"warning: no match for {prefix}*", file=sys.stderr)
    client_lib = os.path.join(isaac, "kit", "extscore", "omni.client.lib")
    if os.path.isdir(client_lib):
        paths.append(client_lib)
    paths.append(sp[0])  # third-party deps (numpy, PIL, websockets)
    return paths


def system_site_packages() -> list[str]:
    out = subprocess.run(
        ["python3", "-c", "import site, json; print(json.dumps(site.getsitepackages()))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in json.loads(out.stdout) if os.path.isdir(p)]


def main() -> None:
    env_prefix = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/miniforge3/envs/isaacsim")
    mcp_venv = glob.glob(os.path.join(REPO, "mcp", ".venv", "lib", "python3.*", "site-packages"))
    isaac = isaac_paths(env_prefix)
    config = {
        "pythonVersion": "3.11",
        "typeCheckingMode": "basic",
        # pxr resolves from the typings/ overlay only (see typings/pxr) — no source module
        "reportMissingModuleSource": False,
        "exclude": ["mcp/.venv", "**/node_modules", "**/__pycache__", "**/.*", "scenarios"],
        # each directory runs under a different interpreter; mirror that per root
        "executionEnvironments": [
            {"root": "exts", "extraPaths": isaac},
            {"root": "mcp", "extraPaths": mcp_venv},
            {"root": "tests", "extraPaths": system_site_packages()},
            {"root": "tools", "extraPaths": [isaac[-1]]},
        ],
    }
    out = os.path.join(REPO, "pyrightconfig.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
