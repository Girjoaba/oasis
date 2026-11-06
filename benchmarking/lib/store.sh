#!/usr/bin/env bash
# The storage side of the benchmarks: oasis_rdma_server with the Parquet files of one dataset in RAM, one
# tmux session per dataset, on the storage node (the second booked U55C node; bench.sh run starts it over
# ssh). The node finds its RoCE address itself and writes it to $WORK/store/<host>.env, which the compute
# node reads over the shared filesystem.
#
# Usage: store.sh start <dataset>...   start the servers (tmux sessions oasis-store-<dataset>)
#        store.sh stop [<dataset>...]  stop them by their pids (default: every server of this host)
#        store.sh status               the servers of this host, the last lines of their logs
#        store.sh detect               the RoCE address of this host, into $WORK/store/<host>.env
#        store.sh counters             port and hw counters of the NIC (data moved, retransmissions)
#
# Datasets: tpch_sf<N>, clickbench, clickbench_sample (lib/env.sh). The NIC is the one of the interface
# STORE_IF (default data1); STORE_DEV, STORE_IB_PORT, STORE_GID override what detect finds. Logs and pid
# files: $WORK/store/<host>_<dataset>.*.
set -euo pipefail
. "$(dirname "$0")/env.sh"
S=$WORK/store
HOST=$(hostname -s)
mkdir -p "$S"

# The RDMA device of the interface, its index in the device list of libibverbs (the --device of the
# server), and the RoCE v2 GID that carries the IPv4 address of the interface
detect() {
    local ifc=${STORE_IF:-data1} ip dev idx port=${STORE_IB_PORT:-1} gid= d i
    ip=$(ip -4 -o addr show dev "$ifc" 2> /dev/null | awk '{print $4}' | cut -d/ -f1 | head -n 1 || true)
    [ -n "$ip" ] || { echo "no IPv4 address on $ifc (STORE_IF)" >&2; return 1; }
    dev=$(ls "/sys/class/net/$ifc/device/infiniband" 2> /dev/null | head -n 1 || true)
    [ -n "$dev" ] || { echo "$ifc has no RDMA device" >&2; return 1; }
    if command -v ibv_devices > /dev/null; then
        idx=$(ibv_devices | awk 'NR > 2 {print $1}' | grep -n -x "$dev" | cut -d: -f1 || true)
    else
        idx=$(ls /sys/class/infiniband | grep -n -x "$dev" | cut -d: -f1 || true)
    fi
    idx=$((${idx:-1} - 1))
    d=/sys/class/infiniband/$dev/ports/$port
    for i in $(ls "$d/gids" | sort -n); do
        [ "$(cat "$d/gid_attrs/ndevs/$i" 2> /dev/null)" = "$ifc" ] || continue
        [ "$(cat "$d/gid_attrs/types/$i" 2> /dev/null)" = "RoCE v2" ] || continue
        case "$(cat "$d/gids/$i")" in 0000:0000:0000:0000:0000:ffff:*) gid=$i; break ;; esac
    done
    [ -n "$gid" ] || { echo "no RoCE v2 IPv4 GID of $ifc on $dev port $port" >&2; return 1; }
    {
        echo "STORE_HOST=$(hostname -f 2> /dev/null || hostname)"
        echo "STORE_IP=$ip"
        echo "STORE_RDMA_DEV=$dev"
        echo "STORE_DEV=${STORE_DEV:-$idx}"
        echo "STORE_IB_PORT=$port"
        echo "STORE_GID=${STORE_GID:-$gid}"
    } > "$(store_env "$HOST")"
    cat "$(store_env "$HOST")"
}

counters() {
    [ -f "$(store_env "$HOST")" ] || detect > /dev/null
    . "$(store_env "$HOST")"
    local d=/sys/class/infiniband/$STORE_RDMA_DEV/ports/$STORE_IB_PORT
    for f in "$d"/counters/port_xmit_data "$d"/counters/port_rcv_data "$d"/counters/port_xmit_packets \
             "$d"/hw_counters/rx_read_requests "$d"/hw_counters/out_of_sequence "$d"/hw_counters/packet_seq_err \
             "$d"/hw_counters/duplicate_request "$d"/hw_counters/local_ack_timeout_err \
             "$d"/hw_counters/implied_nak_seq_err "$d"/hw_counters/out_of_buffer "$d"/hw_counters/rnr_nak_retry_err; do
        [ -r "$f" ] && echo "$(basename "$f") $(cat "$f")"
    done
}

# A server that exited stays a zombie until tmux reaps it; kill -0 still finds it
alive() {
    [ -n "$1" ] && kill -0 "$1" 2> /dev/null && [ "$(ps -o stat= -p "$1" | cut -c1)" != Z ]
}

start() {
    local ds=$1 port dir log pid=$S/${HOST}_$1.pid
    port=$(dataset_port "$ds")
    dir=$(dataset_dir "$ds")
    [ -x "$RDMA_SERVER" ] || { echo "no $RDMA_SERVER, run bench.sh prepare on the build node" >&2; return 1; }
    [ -f "$dir/manifest.json" ] || { echo "no $dir/manifest.json, run bench.sh prepare" >&2; return 1; }
    if [ -f "$pid" ] && alive "$(cat "$pid")"; then
        echo "$ds: runs already (pid $(cat "$pid"), port $port)"; return 0
    fi
    log=$S/${HOST}_$ds.log
    : > "$log"
    tmux kill-session -t "oasis-store-$ds" 2> /dev/null || true
    tmux new -d -s "oasis-store-$ds" \
        "echo \$\$ > $pid; exec $RDMA_SERVER -p $port -d $STORE_DEV -b $STORE_IB_PORT -g $STORE_GID $dir > $log 2>&1"
    # The server reads every file into one region before it listens (SF 30: about a minute from NFS)
    for _ in $(seq 1 900); do
        grep -q "Waiting for connections" "$log" && break
        tmux has-session -t "oasis-store-$ds" 2> /dev/null || break
        sleep 1
    done
    grep -q "Waiting for connections" "$log" || { tail -n 20 "$log"; echo "$ds: the server did not come up" >&2; return 1; }
    grep -q "Using RDMA device: $STORE_RDMA_DEV" "$log" ||
        echo "WARNING: $ds: the server does not use $STORE_RDMA_DEV ($(grep 'Using RDMA device' "$log")); set STORE_DEV" >&2
    grep -q "Advertising RoCE IPv4: $STORE_IP" "$log" ||
        echo "WARNING: $ds: the server does not advertise $STORE_IP; set STORE_GID" >&2
    counters > "$S/${HOST}_$ds.counters_start"
    echo "$ds: $STORE_IP port $port, $(du -sh "$dir" | cut -f1)"
}

stop() {
    local ds=$1 pid
    pid=$(cat "$S/${HOST}_$ds.pid" 2> /dev/null || true)
    if alive "$pid"; then
        counters > "$S/${HOST}_$ds.counters_stop"
        kill "$pid"
        echo "$ds: stopped $pid"
    else
        echo "$ds: does not run"
    fi
}

servers() { for p in "$S/${HOST}"_*.pid; do [ -f "$p" ] && basename "$p" .pid | sed "s/^${HOST}_//"; done; }

case "${1:-}" in
    start)
        shift
        [ $# -gt 0 ] || { echo "start: name the datasets" >&2; exit 2; }
        detect > /dev/null
        . "$(store_env "$HOST")"
        need=$(for ds in "$@"; do du -sk "$(dataset_dir "$ds")" | cut -f1; done | awk '{s += $1} END {print int(s / 1048576) + 1}')
        avail=$(awk '/MemAvailable/ {print int($2 / 1048576)}' /proc/meminfo)
        echo "$HOST: $STORE_IP ($STORE_RDMA_DEV, device $STORE_DEV, GID $STORE_GID); datasets $need GB, available $avail GB"
        [ "$need" -lt "$avail" ] || { echo "not enough memory for $*" >&2; exit 1; }
        for ds in "$@"; do start "$ds"; done
        ;;
    stop)
        shift
        for ds in ${*:-$(servers)}; do stop "$ds"; done
        ;;
    status)
        for ds in $(servers); do
            if alive "$(cat "$S/${HOST}_$ds.pid")"; then echo "$ds: runs, pid $(cat "$S/${HOST}_$ds.pid")"; else echo "$ds: stopped"; fi
            tail -n 3 "$S/${HOST}_$ds.log"
        done
        ;;
    detect) detect ;;
    counters) counters ;;
    *) sed -n '2,16p' "$0"; exit 2 ;;
esac
