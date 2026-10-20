#!/usr/bin/env bash
# Read-only evidence collection. No restarts, process kills, package installs or config changes.
# Run: bash diagnose-host.sh ["2 days ago"]
set -u
export TZ=Asia/Shanghai
export LC_ALL=C
since=${1:-"2 days ago"}

section() { printf '\n=== %s ===\n' "$1"; }
privileged() {
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo -n "$@"
    else
        "$@"
    fi
}

section 'Time, boot and resources'
date -Is
uptime
uname -sr
getconf _NPROCESSORS_ONLN
free -h
df -h / /opt/ntrend 2>/dev/null
df -i / /opt/ntrend 2>/dev/null
swapon --show 2>/dev/null

section 'Largest processes (RSS is KiB; CPU is lifetime average)'
ps -eo pid,ppid,user,comm,stat,pcpu,pmem,rss,etimes --sort=-rss | head -26
section 'Processes ordered by CPU'
ps -eo pid,ppid,user,comm,stat,pcpu,pmem,rss,etimes --sort=-pcpu | head -21
section 'Pressure and short resource sample'
for path in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
    if [ -r "$path" ]; then printf '%s\n' "$path"; cat "$path"; fi
done
vmstat 1 3

section 'Backend service state and resource limits'
systemctl show ntrend-server.service --no-pager \
    -p ActiveState -p SubState -p MainPID -p NRestarts -p Result \
    -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p ExecMainCode -p ExecMainStatus \
    -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax -p MemorySwapMax \
    -p CPUUsageNSec -p CPUQuotaPerSecUSec -p TasksCurrent -p TasksMax -p ControlGroup
section 'Failed services and timers'
systemctl --failed --no-pager
systemctl list-timers --all --no-pager
section 'Network totals'
ss -s

section 'Boot and shutdown history'
privileged journalctl --list-boots --no-pager | tail -16
last -x -n 25 2>/dev/null
section 'Kernel resource, disk and lockup events'
privileged journalctl -k --since "$since" --no-pager -o short-iso 2>&1 \
    | grep -Ei 'oom|out of memory|killed process|blocked for more|hung task|soft lockup|hard lockup|watchdog|I/O error|read-only|no space|thermal|memory cgroup' \
    | tail -160
section 'Previous boot kernel warnings'
privileged journalctl -k -b -1 -p warning --no-pager -o short-iso -n 70 2>&1
section 'Backend warnings and errors'
privileged journalctl -u ntrend-server.service --since "$since" -p warning --no-pager -o short-iso -n 100 2>&1
section 'SSH warnings and errors'
privileged journalctl -u ssh.service -u sshd.service --since "$since" -p warning --no-pager -o short-iso -n 80 2>&1

section 'Backend cgroup resource counters'
cg=$(systemctl show ntrend-server.service -p ControlGroup --value 2>/dev/null)
if [ -n "$cg" ] && [ "$cg" != / ]; then
    for name in memory.events memory.peak memory.current memory.swap.current cpu.stat; do
        path="/sys/fs/cgroup${cg}/${name}"
        if [ -r "$path" ]; then printf '%s\n' "$path"; cat "$path"; fi
    done
fi
section 'End of read-only collection'
