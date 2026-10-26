#!/usr/bin/env bash
# Check out the submodule commits a frozen build records, fetched from the forks that hold them.
#
# Usage: scripts/ci/pin_submodules.sh <frozen dir>
#   <frozen dir>  output of sim/vl/freeze.sh (hardware/sim_frozen); its FROZEN file holds one
#                 `submodule: <path> <commit>` line per submodule of the checkout the freeze was made from
#
# Oasis pins parcore, parcore pins libstf and libstf pins Coyote, all at upstream commits. The
# Verilator fixes sit on fork branches until the pull requests merge, so a plain submodule checkout
# gives sources that Verilator refuses. This script moves the three nested checkouts to the frozen
# commits, top-down, and stops with exit 1 when a frozen commit does not contain the commit that the
# parent tree pins: then the pin moved after the freeze, and the freeze must be made again.
#
# Environment: FORK_PARCORE, FORK_LIBSTF, FORK_COYOTE, the repositories that hold the frozen
# commits (default: the Girjoaba forks on GitHub). Run from the Oasis checkout root.
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 <frozen dir>" >&2; exit 1; }
FROZEN=$1/FROZEN
[ -f "$FROZEN" ] || { echo "$FROZEN missing" >&2; exit 1; }
: "${FORK_PARCORE:=https://github.com/Girjoaba/parcore.git}"
: "${FORK_LIBSTF:=https://github.com/Girjoaba/libstf.git}"
: "${FORK_COYOTE:=https://github.com/Girjoaba/Coyote.git}"

pin() {
    local path=$1 url=$2 sha recorded
    sha=$(awk -v p="$path" '$1 == "submodule:" && $2 == p {print $3}' "$FROZEN")
    [ -n "$sha" ] || { echo "$FROZEN has no submodule line for $path" >&2; exit 1; }
    [ -d "$path/.git" ] || [ -f "$path/.git" ] || { echo "$path is not initialized" >&2; exit 1; }
    # The commit the parent tree pins; read after the parent was moved, so it is the pin of the frozen parent.
    recorded=$(git -C "$(dirname "$path")" ls-tree HEAD "$(basename "$path")" | awk '{print $3}')
    # A shallow clone may already hold the commit without its history (the fork's default branch), so
    # the fetch is skipped only when the history between the two commits is there.
    if ! git -C "$path" merge-base --is-ancestor "$recorded" "$sha" 2> /dev/null; then
        git -C "$path" fetch -q --depth 50 "$url" "$sha"
        if ! git -C "$path" merge-base --is-ancestor "$recorded" "$sha"; then
            echo "$path: the tree pins $recorded, which is not in the frozen $sha; freeze again on the current pins" >&2
            exit 1
        fi
    fi
    git -C "$path" checkout -q --detach "$sha"
    [ "$(git -C "$path" rev-parse HEAD)" = "$sha" ] || { echo "$path: HEAD is not $sha" >&2; exit 1; }
    echo "$path: $sha (contains the pin $recorded)"
}

pin parcore "$FORK_PARCORE"
pin parcore/libstf "$FORK_LIBSTF"
pin parcore/libstf/coyote "$FORK_COYOTE"
