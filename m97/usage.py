"""How the machine's time is spent, per day: tallied by m97 as it samples, kept on disk.

Each sample adds the time since the previous one to one category, by what gufo was doing:

- `serving`: gufo ready, and at least one session busy;
- `idle`: gufo ready, no session busy (the model loaded and waiting);
- `loading`: gufo's container running, not ready yet (or not yet asked);
- `stopped`: gufo's container not running.

The rest of a day is time m97 did not see — the machine off or asleep, or m97 itself down — shown as
"m97 off" and computed when shown, never stored. Beside the categories runs a counter of other GPU use:
samples outside `serving` in which the GPU was busy, whatever was using it. Each category, and that counter,
also sums the energy the socket drew over its samples.

The tally is `{"YYYY-MM-DD": {"serving_s": …, "serving_wh": …, …}}`, by local date.
"""

import datetime
import json
import logging
import os
import pathlib

__all__ = ["CATEGORIES", "GPU_OTHER_PCT", "MAX_INTERVAL_S",
           "default_usage_path", "load_usage", "save_usage",
           "category", "account",
           "day_length_s", "day_summary"]

logger = logging.getLogger(__name__)

CATEGORIES = ("serving", "idle", "loading", "stopped")
GPU_OTHER_PCT = 30      # GPU load above this, outside serving, counts as other GPU use
MAX_INTERVAL_S = 10.0   # longest gap one sample may credit; a suspend or a stalled loop credits no more

_KEYS = tuple(f"{name}_{unit}" for name in (*CATEGORIES, "gpu_other") for unit in ("s", "wh"))


# --------------------------------------------------------------------------------
# On disk

def default_usage_path():
    """`$XDG_STATE_HOME/m97/usage.json`, by default `~/.local/state/m97/usage.json`."""
    base = os.environ.get("XDG_STATE_HOME") or pathlib.Path("~/.local/state").expanduser()
    return pathlib.Path(base, "m97", "usage.json")


def load_usage(path):
    """Read the tally. A missing file gives an empty one; so does an unreadable one, with a warning."""
    path = pathlib.Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(f"{path}: unreadable, so the usage tally starts afresh: {exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"{path}: not a table of days, so the usage tally starts afresh")
        return {}
    tally = {}
    for date, day in data.items():
        try:
            datetime.date.fromisoformat(date)
        except (TypeError, ValueError):
            continue
        if isinstance(day, dict):
            tally[date] = {key: float(day[key]) for key in _KEYS
                           if isinstance(day.get(key), (int, float)) and not isinstance(day.get(key), bool)}
    return tally


def save_usage(path, tally):
    """Write the tally atomically: a crash mid-write leaves the previous version."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    rounded = {date: {key: round(value, 3) for key, value in day.items()} for date, day in sorted(tally.items())}
    temporary.write_text(json.dumps(rounded, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------------
# Counting

def category(*, container, ready, ready_known, slots):
    """The category of one sample, from gufo's container state, its readiness and its parsed `/slots`."""
    if container != "running":
        return "stopped"
    if not (ready_known and ready):
        return "loading"
    return "serving" if any(slot.get("busy") for slot in slots) else "idle"


def account(day, *, category, interval, maybe_ppt_w, maybe_gpu_busy_pct):
    """One day's tally with one sample added. Returns a new dict; `day` (or `None`, for a new day) is untouched.

    `interval` is the real time since the previous sample, in seconds, and is capped at `MAX_INTERVAL_S`.
    A missing power reading adds the time without energy; a missing GPU reading counts no other GPU use.
    """
    day = dict(day or {})
    interval = min(max(0.0, interval), MAX_INTERVAL_S)
    wh = maybe_ppt_w * interval / 3600 if maybe_ppt_w is not None else 0.0

    def add(name):
        day[f"{name}_s"] = day.get(f"{name}_s", 0.0) + interval
        day[f"{name}_wh"] = day.get(f"{name}_wh", 0.0) + wh

    add(category)
    if category != "serving" and maybe_gpu_busy_pct is not None and maybe_gpu_busy_pct > GPU_OTHER_PCT:
        add("gpu_other")
    return day


# --------------------------------------------------------------------------------
# Reading

def day_length_s(date, *, tz=None):
    """The length of a local calendar day in seconds: 86400, or 82800 and 90000 on the days DST changes.

    `tz` is a `tzinfo` such as a `zoneinfo.ZoneInfo`; by default, the machine's local time zone.
    """
    def midnight(d):
        naive = datetime.datetime.combine(d, datetime.time())
        local = naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()
        return local.astimezone(datetime.UTC)
    return (midnight(date + datetime.timedelta(days=1)) - midnight(date)).total_seconds()


def day_summary(tally, date, *, now=None, tz=None):
    """One day as `{"date", "<category>_s", "m97_off_s", "gpu_other_s", "<category>_wh", "gpu_other_wh"}`.

    `m97_off_s` is the part of the day m97 did not count. For the day `now` falls on, that is the part so far,
    so today's figures add up to the time since midnight; `now` is an aware datetime, by default the present.
    """
    day = tally.get(date.isoformat(), {})
    summary = {"date": date.isoformat()}
    summary.update({f"{name}_s": day.get(f"{name}_s", 0.0) for name in CATEGORIES})
    length = day_length_s(date, tz=tz)
    now = now or datetime.datetime.now(datetime.UTC)
    local_now = now.astimezone(tz) if tz is not None else now.astimezone()
    if local_now.date() == date:
        start = datetime.datetime.combine(date, datetime.time())
        start = start.replace(tzinfo=tz) if tz is not None else start.astimezone()
        length = min(length, (now - start).total_seconds())
    # A sample just after midnight credits its whole interval to the new day, so the counted time can exceed
    # the day by a few seconds; m97 off never goes below zero for it.
    summary["m97_off_s"] = max(0.0, length - sum(summary[f"{name}_s"] for name in CATEGORIES))
    summary["gpu_other_s"] = day.get("gpu_other_s", 0.0)
    summary.update({f"{name}_wh": day.get(f"{name}_wh", 0.0) for name in (*CATEGORIES, "gpu_other")})
    return summary
