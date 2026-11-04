#!/usr/bin/env bash
# Check out the submodule commits of scripts/ci/PINS, fetched from the repositories that hold them.
#
# Usage: scripts/ci/pin_submodules.sh
#   scripts/ci/PINS holds one `submodule: <path> <commit>` line per submodule (scripts/ci/record_pins.sh)
#
# Oasis pins parcore, parcore pins libstf and libstf pins Coyote. parcore pins a libstf without the
# Verilator fixes (libstf#33), and the Verilator simulation of Coyote sits on a fork branch, so a plain
# submodule checkout gives sources that Verilator refuses. The dictionary ids output of the decoder
# (emit_ids) sits on the parcore fork branch features/emit-ids until it is merged upstream; the Oasis
# tree keeps pinning the upstream parcore commit it starts from. This script moves the three nested
# checkouts to the recorded commits, top-down, and stops with exit 1 when a recorded commit does not
# contain the commit that the parent tree pins: then the pin moved after the record, and record_pins.sh
# must run again.
#
# Environment: PARCORE_URL, LIBSTF_URL, COYOTE_URL, the repositories that hold the recorded commits
# (default: the Girjoaba forks of parcore and Coyote, upstream libstf on GitHub).
set -euo pipefail

cd "$(dirname "$0")/../.."
PINS=scripts/ci/PINS
[ -f "$PINS" ] || { echo "$PINS missing" >&2; exit 1; }
: "${PARCORE_URL:=https://github.com/Girjoaba/parcore.git}"
: "${LIBSTF_URL:=https://github.com/fpgasystems/libstf.git}"
: "${COYOTE_URL:=https://github.com/Girjoaba/Coyote.git}"

pin() {
    local path=$1 url=$2 sha recorded
    sha=$(awk -v p="$path" '$1 == "submodule:" && $2 == p {print $3}' "$PINS")
    [ -n "$sha" ] || { echo "$PINS has no submodule line for $path" >&2; exit 1; }
    [ -d "$path/.git" ] || [ -f "$path/.git" ] || { echo "$path is not initialized" >&2; exit 1; }
    # The commit the parent tree pins; read after the parent was moved, so it is the pin of the recorded parent.
    recorded=$(git -C "$(dirname "$path")" ls-tree HEAD "$(basename "$path")" | awk '{print $3}')
    # A shallow clone may already hold the commit without its history (the tip of the default branch), so
    # the fetch is skipped only when the history between the two commits is there.
    if ! git -C "$path" merge-base --is-ancestor "$recorded" "$sha" 2> /dev/null; then
        git -C "$path" fetch -q --depth 50 "$url" "$sha"
        if ! git -C "$path" merge-base --is-ancestor "$recorded" "$sha"; then
            echo "$path: the tree pins $recorded, which is not in the recorded $sha; run scripts/ci/record_pins.sh after the move" >&2
            exit 1
        fi
    fi
    git -C "$path" checkout -q --detach "$sha"
    [ "$(git -C "$path" rev-parse HEAD)" = "$sha" ] || { echo "$path: HEAD is not $sha" >&2; exit 1; }
    echo "$path: $sha (contains the pin $recorded)"
}

pin parcore "$PARCORE_URL"
pin parcore/libstf "$LIBSTF_URL"
pin parcore/libstf/coyote "$COYOTE_URL"
