"""Which services should be running: remembered across restarts, so m97 can bring them back at boot.

What is remembered is the last thing decided, not a snapshot taken at shutdown, since at shutdown the
containers and m97 stop at the same time and m97 may see the containers die first. A service seen running
counts as wanted running, whoever started it; one that stays down for a grace period while m97 is up counts
as wanted stopped. A shutdown stops m97 well within the grace period, so it never records the stop.
"""

import json
import logging
import os
import pathlib

__all__ = ["default_state_path", "load_state", "save_state",
           "track"]

logger = logging.getLogger(__name__)


def default_state_path():
    """`$XDG_STATE_HOME/m97/state.json`, by default `~/.local/state/m97/state.json`."""
    base = os.environ.get("XDG_STATE_HOME") or pathlib.Path("~/.local/state").expanduser()
    return pathlib.Path(base, "m97", "state.json")


def load_state(path):
    """Read the state file into `{"want": {key: "running" | "stopped"}, "problem": {key: message}}`.

    A missing or unreadable file gives empty tables, so m97 starts nothing it was not asked to.
    """
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        logger.warning(f"{path}: unreadable, so no service will be started automatically: {exc}")
        data = {}
    want = data.get("want") if isinstance(data, dict) else None
    problem = data.get("problem") if isinstance(data, dict) else None
    return {"want": {k: v for k, v in (want or {}).items() if v in ("running", "stopped")},
            "problem": {k: v for k, v in (problem or {}).items() if isinstance(v, str)}}


def save_state(path, state):
    """Write the state file atomically: a crash mid-write leaves the previous version."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def track(want, down_since, *, running, action, now, grace=30.0):
    """Update what is wanted for one service. Returns `(want, down_since)`.

    `want` is `"running"`, `"stopped"` or `None` (nothing known); `down_since` the monotonic time the service
    was first seen down, or `None`. `running` is whether its container runs, `action` the start or stop m97
    is running for it (`"start"`, `"stop"` or `None`), `now` a monotonic time. A start or stop pressed in m97
    counts at once; otherwise a running container means wanted running, and one down for `grace` seconds
    means wanted stopped.
    """
    if action == "start":
        return "running", None
    if action == "stop":
        return "stopped", None
    if running:
        return "running", None
    if down_since is None:
        down_since = now
    if want == "running" and now - down_since >= grace:
        want = "stopped"
    return want, down_since
