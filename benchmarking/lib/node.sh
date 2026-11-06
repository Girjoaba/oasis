#!/usr/bin/env bash
# Prepares the booked U55C compute node: records the node, builds the Coyote driver for its kernel,
# programs the bitstream, loads the driver and allocates the 1 GiB hugepages (TUTORIAL.md, step 9).
#
# Usage: node.sh [info|driver|program|hugepages|check|all] [bitstream]   (default all, in this order)
#        node.sh release                                               end of the booking: hugepages back, bitstream reverted
#
# bitstream: a folder name in $BITSTREAMS (default $BITSTREAM_DEFAULT). program skips a bitstream that
# this boot of the node already holds (same md5, driver loaded); REPROGRAM=1 programs it again. The node
# facts go to $WORK/nodes/<host>_<date>/; bench.sh copies them into every run folder.
set -euo pipefail
. "$(dirname "$0")/env.sh"
HOST=$(hostname -s)
NODE=$WORK/nodes/${HOST}_$(date +%Y%m%d)
BITSTREAM_DIR=$BITSTREAMS/${2:-$BITSTREAM_DEFAULT}
BITSTREAM=$BITSTREAM_DIR/bitstreams/cyt_top.bit
HDEV=/opt/hdev/cli/hdev
# hdev keeps all hugepages below 25 % of the RAM of a NUMA node (63896 MB on hacc-u55c-01, one node):
# 15 pages of 1 GiB fit once the 2 MiB pages of the node default (8192, 16 GiB) are gone
HUGEPAGES=${HUGEPAGES:-15}
HP1G=/sys/kernel/mm/hugepages/hugepages-1048576kB
HP2M=/sys/kernel/mm/hugepages/hugepages-2048kB
PROGRAMMED=$WORK/nodes/${HOST}_programmed
mkdir -p "$NODE"

step() { echo; echo "== $* ($(date +%H:%M:%S))"; }

do_info() {
    step "node facts into $NODE"
    {
        echo "# uname -a"; uname -a
        echo "# lscpu"; lscpu
        echo "# cpufreq governor and boost"
        cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2> /dev/null || echo "no cpufreq"
        cat /sys/devices/system/cpu/cpufreq/boost 2> /dev/null || true
        echo "# free -g"; free -g
        echo "# numactl -H"; numactl -H 2> /dev/null || echo "no numactl"
        echo "# hugepages"; grep -i huge /proc/meminfo
        echo "# FPGA PCIe (vendor 10ee)"; lspci -vv -d 10ee: 2> /dev/null | grep -E "^[0-9a-f]|LnkCap:|LnkSta:" || true
        echo "# host NICs"; lspci | grep -i -E "ethernet|mellanox" || true
        ip -br addr | grep -v -E "^(lo|docker|veth|br-)" || true
        echo "# hdev get network"; $HDEV get network -d 1 2>&1 || true
        echo "# loadavg and users"; cat /proc/loadavg; who | wc -l
    } > "$NODE/node.txt" 2>&1
    lscpu -e > "$NODE/lscpu_e.txt"
    # bench.sh pins n threads to the first n CPUs; this shows whether CPUs 0..15 are 16 cores
    awk 'NR > 1 {print $4}' "$NODE/lscpu_e.txt" | head -16 | sort -u | wc -l |
        xargs -I{} echo "CPUs 0-15 sit on {} distinct cores" | tee -a "$NODE/node.txt"
    grep -E "Model name|^CPU\(s\)|Thread|Socket" "$NODE/node.txt" | head -5
}

do_driver() {
    local d=$WORK/build/driver-$(uname -r)
    step "Coyote driver for kernel $(uname -r) in $d"
    if [ ! -f "$d/build/coyote_driver.ko" ]; then
        rm -rf "$d"; mkdir -p "$d"
        cp -r "$CYT/driver/Makefile" "$CYT/driver/src" "$CYT/driver/include" "$d/"
        make -C "$d" > "$d.log" 2>&1 || { tail -20 "$d.log"; exit 1; }
    fi
    ls -la "$d/build/coyote_driver.ko"
}

# md5 of the bitstream and the boot id: a reboot or another bitstream needs the program step
program_key() { echo "$(md5sum "$BITSTREAM" | cut -d' ' -f1) $(cat /proc/sys/kernel/random/boot_id)"; }

do_program() {
    step "program $BITSTREAM"
    [ -f "$BITSTREAM" ] || { echo "no $BITSTREAM" >&2; exit 1; }
    if [ "${REPROGRAM:-0}" != 1 ] && [ -e /dev/coyote_fpga_0_v0 ] && [ "$(cat "$PROGRAMMED" 2> /dev/null)" = "$(program_key)" ]; then
        echo "programmed in this boot already (REPROGRAM=1 programs it again)"
        return
    fi
    local ko=$WORK/build/driver-$(uname -r)/build/coyote_driver.ko
    [ -f "$ko" ] || { echo "no driver, run: $0 driver" >&2; exit 1; }
    type module > /dev/null 2>&1 || source /etc/profile.d/modules.sh 2> /dev/null || true
    command -v vivado > /dev/null || module load vivado/2024.2 2> /dev/null || true
    command -v vivado > /dev/null || { echo "vivado not in PATH (program_hacc_local.sh needs it)" >&2; exit 1; }
    md5sum "$BITSTREAM" | tee "$NODE/bitstream.md5"
    (cd "$CYT/util" && ./program_hacc_local.sh "$BITSTREAM" "$ko") 2>&1 | tee "$NODE/program.log"
    sudo dmesg | tail -n 20 > "$NODE/dmesg_program.txt" || true
    if grep -q "probe returning 0" "$NODE/dmesg_program.txt"; then
        program_key > "$PROGRAMMED"
    else
        rm -f "$PROGRAMMED"
        echo "WARNING: no 'probe returning 0' in dmesg" >&2
    fi
}

do_hugepages() {
    step "$HUGEPAGES hugepages of 1 GiB"
    if [ "$(cat $HP1G/free_hugepages)" -ge "$HUGEPAGES" ]; then
        echo "nr $(cat $HP1G/nr_hugepages) free $(cat $HP1G/free_hugepages), nothing to do"
        return
    fi
    # release puts the 2 MiB pages back
    [ -f "$WORK/nodes/${HOST}_hugepages_2m" ] || cat $HP2M/nr_hugepages > "$WORK/nodes/${HOST}_hugepages_2m"
    sudo $HDEV set hugepages --num-2m 0 --num-1g "$HUGEPAGES" || true
    echo "nr $(cat $HP1G/nr_hugepages) free $(cat $HP1G/free_hugepages)"
    [ "$(cat $HP1G/free_hugepages)" -ge "$HUGEPAGES" ] ||
        echo "WARNING: fewer than $HUGEPAGES free pages: run it again, else hdev reboot and allocate right after boot" >&2
}

do_check() {
    step "check"
    ls /dev/coyote_fpga_0_v0
    cat /sys/kernel/coyote_sysfs_0/cyt_attr_nstats > "$NODE/nstats_setup.txt" 2>&1 || true
    grep -i huge /proc/meminfo
    for f in "$BENCH" "$DUCKDB_BUILD/src/libduckdb.so"; do [ -e "$f" ] || { echo "missing $f, run bench.sh prepare" >&2; exit 1; }; done
    ldd "$BENCH" | grep "not found" && { echo "unresolved libraries" >&2; exit 1; } || true
    echo "setup OK"
}

do_release() {
    step "release the node"
    local m=8192 max
    [ -f "$WORK/nodes/${HOST}_hugepages_2m" ] && m=$(cat "$WORK/nodes/${HOST}_hugepages_2m")
    # the node default can exceed what hdev allows: 8192 pages are 16 GiB, 25 % of the 62.4 GiB of
    # hacc-u55c-02 is 15.6 GiB (2026-11-06), so hdev refused them and kept the 1 GiB pages
    max=$((($(awk '/MemTotal/ {print $4}' /sys/devices/system/node/node0/meminfo) / 4 - 1) / 2048))
    [ "$m" -gt "$max" ] && { echo "$m pages of 2 MiB exceed the hdev limit, $max instead"; m=$max; }
    sudo $HDEV set hugepages --num-1g 0 --num-2m "$m" || true
    echo "1 GiB pages $(cat $HP1G/nr_hugepages), 2 MiB pages $(cat $HP2M/nr_hugepages)"
    [ "$(cat $HP1G/nr_hugepages)" -eq 0 ] && rm -f "$WORK/nodes/${HOST}_hugepages_2m" ||
        echo "WARNING: the 1 GiB pages are still allocated" >&2
    $HDEV program revert || true
    rm -f "$PROGRAMMED"
}

case "${1:-all}" in
    info)      do_info ;;
    driver)    do_driver ;;
    program)   do_program ;;
    hugepages) do_hugepages ;;
    check)     do_check ;;
    release)   do_release ;;
    all)       do_info; do_driver; do_program; do_hugepages; do_check ;;
    *) sed -n '2,10p' "$0"; exit 2 ;;
esac
