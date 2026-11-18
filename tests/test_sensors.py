import pathlib

import pytest

from m97.sensors import make_sampler, parse_meminfo, parse_proc_stat

STAT_1 = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
STAT_2 = "cpu  160 0 70 900 70 0 0 0 0 0\n"   # +80 busy of +200 total
MEMINFO = "MemTotal:       129462172 kB\nMemFree:  1000 kB\nMemAvailable:   11726392 kB\nHugePages_Total: 0\n"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path):
    """A fake / with an amdgpu and a k10temp hwmon, as on a Strix Halo machine."""
    device = tmp_path / "sys/devices/pci0000:00/0000:bd:00.0"
    write(device / "gpu_busy_percent", "37\n")
    write(device / "mem_info_gtt_used", f"{104 * 2**30}\n")
    write(device / "mem_info_gtt_total", f"{116 * 2**30}\n")
    amdgpu = tmp_path / "sys/class/hwmon/hwmon3"
    write(amdgpu / "name", "amdgpu\n")
    write(amdgpu / "power1_input", "64000000\n")
    write(amdgpu / "temp1_input", "58000\n")
    write(amdgpu / "freq1_input", "2170000000\n")
    (amdgpu / "device").symlink_to(device)
    k10temp = tmp_path / "sys/class/hwmon/hwmon1"
    write(k10temp / "name", "k10temp\n")
    write(k10temp / "temp1_input", "59125\n")
    write(tmp_path / "proc/stat", STAT_1)
    write(tmp_path / "proc/meminfo", MEMINFO)
    write(tmp_path / "proc/loadavg", "0.42 0.30 0.20 1/900 12345\n")
    return tmp_path


def test_parse_proc_stat():
    stats = parse_proc_stat(STAT_1)
    assert stats["cpu"] == (150, 1000)
    assert stats["cpu0"] == (1 + 2 + 3 + 6 + 7 + 8, 1 + 2 + 3 + 4 + 5 + 6 + 7 + 8)
    assert parse_proc_stat("intr 1 2 3\n") == {}
    assert parse_proc_stat("cpu  x y z\n") == {}


def test_parse_meminfo():
    info = parse_meminfo(MEMINFO)
    assert info["MemTotal"] == 129462172 * 1024
    assert "HugePages_Total" not in info          # no unit, so not a size


@pytest.mark.parametrize("as_type", [str, pathlib.Path])
def test_sample(tree, as_type):
    sample = make_sampler(root=as_type(tree))
    first = sample()
    assert first["ppt_w"] == 64.0
    assert first["cpu_temp_c"] == pytest.approx(59.125)
    assert first["gpu_temp_c"] == 58.0
    assert first["gpu_busy_pct"] == 37
    assert first["gpu_mhz"] == 2170.0
    assert first["gtt_used_gib"] == 104.0
    assert first["gtt_total_gib"] == 116.0
    assert first["load1"] == pytest.approx(0.42)
    assert first["ram_total_gib"] == pytest.approx(129462172 / 2**20)
    assert first["ram_used_gib"] == pytest.approx((129462172 - 11726392) / 2**20)
    assert first["cpu_busy_pct"] is None, "there is no previous sample to take a difference against"
    write(tree / "proc/stat", STAT_2)
    assert sample()["cpu_busy_pct"] == pytest.approx(40.0)


def test_per_thread(tree):
    write(tree / "proc/stat", "cpu  0 0 0 0 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\ncpu0 0 0 0 100 0 0 0 0\n")
    sample = make_sampler(root=tree)
    assert sample()["cpu_thread_pct"] == [None, None]
    write(tree / "proc/stat", "cpu  0 0 0 0 0 0 0 0\ncpu1 50 0 0 150 0 0 0 0\ncpu0 25 0 0 175 0 0 0 0\n")
    assert sample()["cpu_thread_pct"] == [pytest.approx(25.0), pytest.approx(50.0)], "in CPU number order"


def test_missing_sensors_give_none(tmp_path):
    sample = make_sampler(root=tmp_path)            # an empty tree: no hwmon, no proc
    values = sample()
    assert all(value is None for value in values.values()), values


def test_unreadable_sensor_gives_none(tree):
    (tree / "sys/class/hwmon/hwmon3/power1_input").write_text("garbage\n")
    assert make_sampler(root=tree)()["ppt_w"] is None


def test_counter_reset_gives_none(tree):
    sample = make_sampler(root=tree)
    sample()
    write(tree / "proc/stat", "cpu  1 0 1 1 0 0 0 0 0 0\n")     # totals went backwards
    assert sample()["cpu_busy_pct"] is None
