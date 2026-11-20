import datetime
import json
import zoneinfo

import pytest

from m97 import usage

HELSINKI = zoneinfo.ZoneInfo("Europe/Helsinki")


# --------------------------------------------------------------------------------
# Category of a sample

BUSY = [{"id": 0, "busy": True}, {"id": 1, "busy": False}]
IDLE = [{"id": 0, "busy": False}, {"id": 1, "busy": False}]


@pytest.mark.parametrize("container, ready, ready_known, slots, expected", [
    (None, False, False, [], "stopped"),
    ("exited", False, False, [], "stopped"),
    ("running", False, False, [], "loading"),      # just seen, not yet asked: m97's `unknown`
    ("running", False, True, [], "loading"),       # asked, still loading the model
    ("running", True, True, IDLE, "idle"),
    ("running", True, True, [], "idle"),           # ready, slots not read yet
    ("running", True, True, BUSY, "serving"),
])
def test_category(container, ready, ready_known, slots, expected):
    assert usage.category(container=container, ready=ready, ready_known=ready_known, slots=slots) == expected


# --------------------------------------------------------------------------------
# Counting

def sample(day, category, *, interval=1.0, ppt_w=20.0, gpu=0):
    return usage.account(day, category=category, interval=interval, maybe_ppt_w=ppt_w, maybe_gpu_busy_pct=gpu)


def test_time_and_energy_are_summed_per_category():
    day = None
    for _ in range(3600):
        day = sample(day, "idle", ppt_w=19.0)
    day = sample(day, "serving", interval=2.0, ppt_w=90.0)
    assert day["idle_s"] == pytest.approx(3600.0) and day["idle_wh"] == pytest.approx(19.0)
    assert day["serving_s"] == 2.0 and day["serving_wh"] == pytest.approx(90.0 * 2 / 3600)
    assert "loading_s" not in day and "stopped_s" not in day


def test_account_leaves_its_input_alone():
    day = {"idle_s": 1.0}
    assert sample(day, "idle")["idle_s"] == 2.0
    assert day == {"idle_s": 1.0}


def test_other_gpu_use_counts_only_outside_serving_and_above_the_threshold():
    assert usage.GPU_OTHER_PCT < 99
    day = sample(None, "stopped", gpu=99, ppt_w=80.0)
    assert day["gpu_other_s"] == 1.0 and day["gpu_other_wh"] == pytest.approx(80.0 / 3600)
    assert "gpu_other_s" in sample(None, "idle", gpu=99), "a game while the model sits loaded counts too"
    assert "gpu_other_s" not in sample(None, "serving", gpu=99), "serving is gufo's own GPU use"
    assert "gpu_other_s" not in sample(None, "stopped", gpu=usage.GPU_OTHER_PCT)
    assert "gpu_other_s" not in sample(None, "stopped", gpu=None)


def test_a_missing_power_reading_counts_the_time_without_energy():
    day = sample(None, "idle", ppt_w=None)
    assert day == {"idle_s": 1.0, "idle_wh": 0.0}


def test_interval_is_capped():
    # A suspend, or a loop stalled for a minute, must not credit the whole gap to the last category seen.
    day = sample(None, "idle", interval=600.0)
    assert day["idle_s"] == usage.MAX_INTERVAL_S
    assert sample(None, "idle", interval=-1.0)["idle_s"] == 0.0


# --------------------------------------------------------------------------------
# Days

def test_day_length_follows_dst():
    assert usage.day_length_s(datetime.date(2026, 11, 20), tz=HELSINKI) == 86400
    assert usage.day_length_s(datetime.date(2026, 3, 29), tz=HELSINKI) == 82800   # clocks forward
    assert usage.day_length_s(datetime.date(2026, 10, 25), tz=HELSINKI) == 90000  # clocks back
    assert usage.day_length_s(datetime.date(2026, 11, 20)) in (82800, 86400, 90000)   # the local zone


def test_m97_off_is_the_rest_of_the_day():
    tally = {"2026-11-19": {"serving_s": 3600.0, "idle_s": 7200.0},
             "2026-10-25": {"stopped_s": 3600.0}}
    now = datetime.datetime(2026, 11, 21, 12, 0, tzinfo=HELSINKI)
    summary = usage.day_summary(tally, datetime.date(2026, 11, 19), now=now, tz=HELSINKI)
    assert summary["m97_off_s"] == 86400 - 3600 - 7200
    assert summary["loading_s"] == 0.0 and summary["gpu_other_wh"] == 0.0
    dst = usage.day_summary(tally, datetime.date(2026, 10, 25), now=now, tz=HELSINKI)
    assert dst["m97_off_s"] == 90000 - 3600
    unseen = usage.day_summary(tally, datetime.date(2026, 11, 20), now=now, tz=HELSINKI)
    assert unseen["m97_off_s"] == 86400


def test_today_counts_only_the_part_so_far():
    tally = {"2026-11-20": {"idle_s": 3600.0}}
    now = datetime.datetime(2026, 11, 20, 6, 0, tzinfo=HELSINKI)
    summary = usage.day_summary(tally, datetime.date(2026, 11, 20), now=now, tz=HELSINKI)
    assert summary["m97_off_s"] == 5 * 3600


def test_m97_off_never_goes_negative():
    # A sample just after midnight credits its whole interval to the new day.
    tally = {"2026-11-19": {"idle_s": 86400.0 + 5}}
    now = datetime.datetime(2026, 11, 21, tzinfo=HELSINKI)
    assert usage.day_summary(tally, datetime.date(2026, 11, 19), now=now, tz=HELSINKI)["m97_off_s"] == 0.0


def test_samples_either_side_of_midnight_land_on_two_days(monkeypatch, tmp_path):
    from m97 import hub as hub_module
    from m97.settings import load

    class Clock(datetime.date):
        today_is = datetime.date(2026, 11, 19)

        @classmethod
        def today(cls):
            return cls.today_is

    hub = hub_module.Hub(load("/nonexistent/settings.toml"), state_path=tmp_path / "state.json")
    monkeypatch.setattr(hub_module.datetime, "date", Clock)
    hub.latest = {"ppt_w": 5.0}
    hub._count_usage(100.0)           # the first sample only sets the clock
    hub._count_usage(101.0)           # 23:59:59
    Clock.today_is = datetime.date(2026, 11, 20)
    hub._count_usage(102.0)           # 00:00:01
    assert hub.usage == {"2026-11-19": {"stopped_s": 1.0, "stopped_wh": pytest.approx(5 / 3600)},
                         "2026-11-20": {"stopped_s": 1.0, "stopped_wh": pytest.approx(5 / 3600)}}


# --------------------------------------------------------------------------------
# On disk

def test_round_trip(tmp_path):
    path = tmp_path / "m97" / "usage.json"
    tally = {"2026-11-20": sample(sample(None, "idle"), "stopped", gpu=99)}
    usage.save_usage(str(path), tally)                     # a str is as good as a Path
    loaded = usage.load_usage(path)
    assert loaded.keys() == tally.keys()
    assert loaded["2026-11-20"] == pytest.approx(tally["2026-11-20"], abs=1e-3)   # kept to 3 decimals on disk
    assert not path.with_name("usage.json.tmp").exists()


def test_a_missing_file_is_an_empty_tally(tmp_path):
    assert usage.load_usage(tmp_path / "nowhere.json") == {}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"'])
def test_a_corrupt_file_starts_afresh_with_a_warning(tmp_path, caplog, content):
    path = tmp_path / "usage.json"
    path.write_text(content)
    assert usage.load_usage(path) == {}
    assert "starts afresh" in caplog.text


def test_bad_entries_are_dropped_and_the_rest_kept(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"2026-11-20": {"idle_s": 10, "serving_s": "lots", "idle_wh": True, "extra": 1},
                                "yesterday": {"idle_s": 5},
                                "2026-11-19": [1, 2]}))
    assert usage.load_usage(path) == {"2026-11-20": {"idle_s": 10.0}}


def test_the_hub_continues_the_day_after_a_restart(tmp_path):
    from m97 import hub as hub_module
    from m97.settings import load

    settings = load("/nonexistent/settings.toml")
    first = hub_module.Hub(settings, state_path=tmp_path / "state.json")
    first.usage = {"2026-11-20": {"idle_s": 60.0}}
    first._save_usage()
    second = hub_module.Hub(settings, state_path=tmp_path / "state.json")
    assert second.usage == {"2026-11-20": {"idle_s": 60.0}}
    assert (tmp_path / "usage.json").exists(), "kept beside the state file"
