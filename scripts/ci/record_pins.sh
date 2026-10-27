#!/usr/bin/env bash
# Write the commits of the submodules scripts/ci/pin_submodules.sh moves to scripts/ci/PINS. Needs git only.
#
# Usage: scripts/ci/record_pins.sh
#
# Run after a submodule moved, then commit scripts/ci/PINS.
set -euo pipefail

cd "$(dirname "$0")/../.."
for p in parcore parcore/libstf parcore/libstf/coyote; do
    echo "submodule: $p $(git -C "$p" rev-parse HEAD)"
done > scripts/ci/PINS
cat scripts/ci/PINS
