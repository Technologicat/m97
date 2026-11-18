"""Host sensors on an AMD APU: power, temperatures, GPU load and clock, CPU load, RAM and GTT.

Reads sysfs and procfs only, so it needs no root. The hwmon sensors are found by driver name, since hwmon
indices are not stable across boots. A sensor that is missing, or that fails to read, comes back as `None`,
so the page shows a blank rather than the sampler failing.
"""

import pathlib

__all__ = ["find_hwmon",
           "parse_proc_stat", "parse_meminfo",
           "cpu_cores",
           "make_sampler"]


def find_hwmon(name, *, root="/"):
    """Return the hwmon directory whose `name` file says `name`, or `None`."""
    for d in sorted(pathlib.Path(root, "sys/class/hwmon").glob("hwmon*")):
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            continue
    return None


def _read_int(path):
    if path is None:
        return None
    try:
        return int(pathlib.Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _scale(value, divisor):
    return None if value is None else value / divisor


def parse_proc_stat(text):
    """Return `{name: (busy, total)}` jiffies for the `cpu` lines of /proc/stat.

    `"cpu"` is the whole machine, `"cpu0"`, `"cpu1"`, ... each hardware thread. Busy is everything except
    idle and iowait. Guest time is already counted in user and nice, so it is left out of the total rather
    than counted twice. A line that does not parse is skipped.
    """
    stats = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        try:
            values = [int(v) for v in fields[1:9]]  # user nice system idle iowait irq softirq steal
        except ValueError:
            continue
        if len(values) < 4:
            continue
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        stats[fields[0]] = (total - idle, total)
    return stats


def _busy_pct(before, now):
    if before is None or now is None or now[1] <= before[1]:
        return None
    return 100.0 * (now[0] - before[0]) / (now[1] - before[1])


def parse_meminfo(text):
    """Return `{key: bytes}` for the `kB` lines of /proc/meminfo."""
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) == 2 and fields[1] == "kB":
            try:
                info[key] = int(fields[0]) * 1024
            except ValueError:
                continue
    return info


def cpu_cores(*, root="/"):
    """The hardware threads grouped by physical core: a list of cores, each a sorted list of CPU numbers.

    Threads share a core when their sysfs `topology` gives the same package, die and core ID. Cores are
    ordered by their lowest CPU number. A CPU whose core ID cannot be read counts as a core of its own.
    """
    cores = {}
    for d in pathlib.Path(root, "sys/devices/system/cpu").glob("cpu[0-9]*"):
        cpu = int(d.name[3:])
        ids = tuple(_read_int(d / "topology" / name) for name in ("physical_package_id", "die_id", "core_id"))
        cores.setdefault(ids if ids[2] is not None else ("alone", cpu), []).append(cpu)
    return sorted((sorted(threads) for threads in cores.values()), key=min)


def make_sampler(*, root="/"):
    """Locate the sensors once, and return a function that takes one sample as a dict.

    The keys: `ppt_w` (socket power), `cpu_temp_c` (Tctl), `gpu_temp_c` (edge), `gpu_busy_pct`, `gpu_mhz`,
    `cpu_busy_pct`, `cpu_thread_pct`, `load1`, `ram_used_gib`, `ram_total_gib`, `gtt_used_gib`,
    `gtt_total_gib`. Any of them may be `None`. `cpu_busy_pct` is the share of CPU time spent busy since the
    previous sample, so the first sample has `None` there; `cpu_thread_pct` is the same per hardware thread,
    a dict from CPU number to percentage, `None` for a thread that has no figure.

    `root` relocates every path, for testing against a fake tree.
    """
    root = pathlib.Path(root)
    amdgpu = find_hwmon("amdgpu", root=root)
    k10temp = find_hwmon("k10temp", root=root)
    # The DRM device behind the amdgpu hwmon holds gpu_busy_percent and the GTT counters.
    device = (amdgpu / "device") if amdgpu is not None else None
    proc = root / "proc"

    def at(base, name):
        return None if base is None else base / name

    previous = {}  # name -> (busy, total) jiffies at the last sample

    def cpu_busy():
        """`(whole machine %, [per thread %])` since the last call."""
        nonlocal previous
        try:
            now = parse_proc_stat((proc / "stat").read_text())
        except OSError:
            now = {}
        before, previous = previous, now
        per_thread = {int(name[3:]): _busy_pct(before.get(name), stat)
                      for name, stat in now.items() if name[3:].isdigit()}
        return _busy_pct(before.get("cpu"), now.get("cpu")), (per_thread or None)

    def load1():
        try:
            return float((proc / "loadavg").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def sample():
        cpu_pct, thread_pct = cpu_busy()
        try:
            meminfo = parse_meminfo((proc / "meminfo").read_text())
        except OSError:
            meminfo = {}
        ram_total = meminfo.get("MemTotal")
        ram_available = meminfo.get("MemAvailable")
        return {
            "ppt_w": _scale(_read_int(at(amdgpu, "power1_input")), 1e6),        # µW
            "cpu_temp_c": _scale(_read_int(at(k10temp, "temp1_input")), 1e3),   # m°C
            "gpu_temp_c": _scale(_read_int(at(amdgpu, "temp1_input")), 1e3),
            "gpu_busy_pct": _read_int(at(device, "gpu_busy_percent")),
            "gpu_mhz": _scale(_read_int(at(amdgpu, "freq1_input")), 1e6),       # Hz
            "cpu_busy_pct": cpu_pct,
            "cpu_thread_pct": thread_pct,
            "load1": load1(),
            "ram_used_gib": (_scale(ram_total - ram_available, 2**30)
                             if ram_total is not None and ram_available is not None else None),
            "ram_total_gib": _scale(ram_total, 2**30),
            "gtt_used_gib": _scale(_read_int(at(device, "mem_info_gtt_used")), 2**30),
            "gtt_total_gib": _scale(_read_int(at(device, "mem_info_gtt_total")), 2**30),
        }

    return sample
