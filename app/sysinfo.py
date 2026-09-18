import os
import threading

from . import auth

# Everything here reads directly from /proc and /sys. Docker doesn't
# namespace either by default (no --pid=host needed), so a plain container
# on TrueNAS SCALE normally sees the host's real memory/thermal/network
# figures - which is what's actually useful for a home-server admin page.
# Every reader is defensive: some hosts/kernels don't expose a given file
# (e.g. no thermal zone in a VM), and that should show as "unavailable"
# rather than break the admin page.


def format_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    if n < 1024:
        return f"{int(n)} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        n /= 1024
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} ТБ"


def _read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if not parts:
                    continue
                try:
                    info[key] = int(parts[0]) * 1024  # values are in kB
                except ValueError:
                    continue
    except OSError:
        return None
    return info or None


def get_memory_stats():
    """used/cached/available are a mutually-exclusive split of total (they
    add up to it exactly) so they can be drawn as three slices of one pie -
    MemAvailable already counts most reclaimable cache as "available", so
    used is whatever's left over once available and cache are both
    subtracted, rather than the more common (but overlapping-with-cache)
    total-minus-available definition."""
    info = _read_meminfo()
    if not info:
        return None
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    cached = (info.get("Buffers") or 0) + (info.get("Cached") or 0)
    used = max(0, total - available - cached) if (total is not None and available is not None) else None
    return {"total": total, "available": available, "used": used, "cached": cached}


def get_cpu_temperature():
    """Returns the highest reading across thermal zones (°C), or None if
    /sys/class/thermal isn't exposed to this container."""
    base = "/sys/class/thermal"
    try:
        zones = os.listdir(base)
    except OSError:
        return None
    readings = []
    for zone in zones:
        try:
            with open(os.path.join(base, zone, "temp")) as f:
                raw = int(f.read().strip())
            readings.append(raw / 1000.0)
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


# Guards _cpu_last below, same reasoning as _net_lock further down.
_cpu_lock = threading.Lock()

# (idle, total) jiffies last seen this process's lifetime, or None before
# the first read - /proc/stat only ever gives cumulative counters since
# boot, so a single reading can't tell you a percentage on its own, only
# the delta between two readings can.
_cpu_last = None


def _read_cpu_times():
    try:
        with open("/proc/stat") as f:
            first_line = f.readline()
    except OSError:
        return None
    parts = first_line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    # Columns: user, nice, system, idle, iowait, irq, softirq, steal, ...
    # iowait counts as idle (the CPU wasn't doing anything, just waiting on
    # disk), same convention `top`/`htop` use.
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def get_cpu_usage_percent():
    """Overall CPU utilization (%) across all cores since the last call.
    Returns None on the very first call after startup (and if /proc/stat
    isn't readable) since there's no earlier sample yet to diff against."""
    sample = _read_cpu_times()
    if sample is None:
        return None
    idle, total = sample
    global _cpu_last
    with _cpu_lock:
        prev = _cpu_last
        _cpu_last = (idle, total)
        if prev is None:
            return None
        prev_idle, prev_total = prev
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total <= 0:
            return None
        usage = (1 - delta_idle / delta_total) * 100
    return max(0.0, min(100.0, usage))


def get_network_stats():
    """Cumulative rx/tx bytes since container start, summed across all
    non-loopback interfaces. Obelisk is the only process in this container,
    so this is effectively the service's own network usage. Resets to zero
    on every container restart (a fresh container gets a fresh network
    namespace) - see get_persisted_network_stats() for a total that survives
    that."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    rx_total = 0
    tx_total = 0
    found = False
    for line in lines:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
            found = True
        except ValueError:
            continue
    return {"rx_bytes": rx_total, "tx_bytes": tx_total} if found else None


# Guards _net_last_raw below - the admin page can poll /admin/api/sysinfo
# from more than one concurrent request (page load + the 1s timer racing
# each other right after a tab switch), and read-modify-write on a bare
# module global isn't safe against that on its own.
_net_lock = threading.Lock()

# Raw (rx, tx) last seen this process's lifetime, or None before the first
# read. Deliberately not persisted itself - only used to compute how much
# NEW traffic happened since the last time this function ran, so it can be
# added onto the DB-persisted running total.
_net_last_raw = None


def get_persisted_network_stats(db):
    """Same shape as get_network_stats(), but as an all-time running total
    that survives container restarts - the raw /proc/net/dev counters reset
    to zero every time (fresh network namespace), so the true total has to
    be carried forward in the database instead of read fresh each time."""
    raw = get_network_stats()
    if raw is None:
        return None
    global _net_last_raw
    with _net_lock:
        stored_rx = int(auth.get_setting(db, "net_rx_total", "0") or 0)
        stored_tx = int(auth.get_setting(db, "net_tx_total", "0") or 0)
        if _net_last_raw is None:
            # First read since this process started - the raw counters
            # always start at zero on a fresh container, so the current
            # reading in full is exactly what's new since the restart.
            delta_rx, delta_tx = raw["rx_bytes"], raw["tx_bytes"]
        else:
            last_rx, last_tx = _net_last_raw
            delta_rx = max(0, raw["rx_bytes"] - last_rx)
            delta_tx = max(0, raw["tx_bytes"] - last_tx)
        _net_last_raw = (raw["rx_bytes"], raw["tx_bytes"])
        total_rx = stored_rx + delta_rx
        total_tx = stored_tx + delta_tx
        if delta_rx or delta_tx:
            auth.set_setting(db, "net_rx_total", str(total_rx))
            auth.set_setting(db, "net_tx_total", str(total_tx))
    return {"rx_bytes": total_rx, "tx_bytes": total_tx}
