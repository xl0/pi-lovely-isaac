#!/usr/bin/env bash
# Launch Isaac Sim GUI with the agent protocol server (+ jupyter, parity with research env.sh).
# Runs in foreground; redirect output and use nohup/& yourself if needed.
set -euo pipefail
ISAACSIM_ENV="${ISAACSIM_ENV:-/home/xl0/miniforge3/envs/isaacsim}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export OMNI_KIT_ACCEPT_EULA=YES
exec "$ISAACSIM_ENV/bin/isaacsim" \
  --enable isaacsim.code_editor.jupyter \
  --/exts/isaacsim.code_editor.jupyter/notebook_dir=/home/xl0/work/work/tm/research/isaacsim \
  '--/exts/isaacsim.code_editor.jupyter/command_line_options=--allow-root --no-browser --JupyterApp.answer_yes=True --ServerApp.disable_check_xsrf=True' \
  --ext-folder "$REPO/exts" \
  --enable xl0.lovely.isaac \
  "$@"
