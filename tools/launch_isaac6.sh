#!/usr/bin/env bash
# Launch Isaac 6 with Jupyter, the agent server, and the research launcher's GPU limits.
set -euo pipefail
TOOLS="$(cd "$(dirname "$0")" && pwd)"
export ISAACSIM_ENV="$HOME/miniforge3/envs/isaacsim6"
exec "$TOOLS/launch_isaac.sh" \
  --/app/runLoops/main/rateLimitEnabled=true \
  --/app/runLoops/main/rateLimitFrequency=60 \
  --/rtx/ecoMode/enabled=true \
  "$@"
