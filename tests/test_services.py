import asyncio
import pathlib

import pytest

from m97.services import (Service, derive_status, gufo_run_args, openwebui_run_args, start, stop)
from m97.settings import load

SETTINGS = load(pathlib.Path("/nonexistent/settings.toml"))   # the shipped defaults


# --------------------------------------------------------------------------------
# Command lines

def test_gufo_args_defaults():
    args = gufo_run_args(SETTINGS["gufo"], lan=False, devices=["/dev/kfd", "/dev/dri"], uid=1000, gid=1000)
    assert args[:3] == ["-d", "--name", "gufo"]
    assert "--userns=keep-id:uid=1000,gid=1000" in args
    assert args.count("--device") == 2
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--model") + 1].startswith("/models/flash-next/")
    assert "--speculative" in args
    assert "--sessions" not in args and "--context" not in args     # 0: gufo's own defaults
    models = args[args.index("-v") + 1]
    assert models.endswith(":/models:ro") and "~" not in models


def test_gufo_args_overridden():
    gufo = dict(SETTINGS["gufo"], sessions=3, context=262144, mtp_model="", extra_args=["--cache-ram-bytes", "off"])
    args = gufo_run_args(gufo, lan=True, devices=[], uid=1001, gid=1002)
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert args[args.index("--sessions") + 1] == "3"
    assert args[args.index("--context") + 1] == "262144"
    assert "--speculative" not in args
    assert args[-2:] == ["--cache-ram-bytes", "off"]
    assert "--device" not in args


def test_openwebui_args():
    args = openwebui_run_args(SETTINGS["openwebui"], dict(SETTINGS["gufo"], port=9090), lan=True)
    assert "HOST=0.0.0.0" in args and "PORT=3000" in args
    assert "OPENAI_API_BASE_URL=http://127.0.0.1:9090/v1" in args
    assert args[-1] == SETTINGS["openwebui"]["image"]


# --------------------------------------------------------------------------------
# Start and stop, against a fake podman

def make_podman(state=None, *, dies=False, run_fails=False):
    """A fake `run` for podman: keeps one container's state, and records the commands it was given."""
    box = {"state": state, "calls": []}

    async def run(*args, timeout=60):
        box["calls"].append(args[1])
        verb = args[1]
        if verb == "container":                                      # inspect
            return (0, f"{box['state']}\n", "") if box["state"] else (125, "", "no such container")
        if verb == "run":
            if run_fails:
                return 125, "", "Error: image not known"
            box["state"] = "exited" if dies else "running"
            return 0, "abc123\n", ""
        if verb == "stop":
            if box["state"]:
                box["state"] = "exited"
            return 0, "", ""
        if verb == "rm":
            box["state"] = None
            return 0, "", ""
        if verb == "logs":
            return 0, "loading model\n", "error: out of memory\n"
        raise AssertionError(f"unexpected podman call {args}")

    return box, run


SERVICE = Service(key="gufo", title="gufo", container="gufo", run_args=lambda lan: ["-d", "--name", "gufo", "img"],
                  ready_url="http://127.0.0.1:1/ready", start_timeout=10)


def fake_clock():
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]
    return clock


def ready_after(n):
    calls = [0]

    async def ready():
        calls[0] += 1
        return calls[0] > n
    return ready


def run_start(run, ready, **kwargs):
    return asyncio.run(start(SERVICE, lan=False, run=run, ready=ready, clock=fake_clock(), poll=0, **kwargs))


def test_start_waits_until_ready():
    box, run = make_podman()
    assert run_start(run, ready_after(3)) is None
    assert box["state"] == "running"
    assert "rm" not in box["calls"], "no leftover container, so nothing to remove"


def test_start_removes_leftover_container_first():
    box, run = make_podman("exited")
    assert run_start(run, ready_after(0)) is None
    assert box["calls"].index("rm") < box["calls"].index("run")


def test_start_refuses_when_running():
    box, run = make_podman("running")
    assert "already running" in run_start(run, ready_after(0))
    assert "run" not in box["calls"]


def test_start_reports_podman_failure():
    _, run = make_podman(run_fails=True)
    assert "image not known" in run_start(run, ready_after(0))


def test_start_reports_death_with_log():
    _, run = make_podman(dies=True)
    message = run_start(run, ready_after(100))
    assert "stopped while starting" in message and "out of memory" in message


def test_start_times_out_and_leaves_container():
    box, run = make_podman()
    message = run_start(run, ready_after(100))
    assert "did not answer within 10 s" in message
    assert box["state"] == "running"


def test_stop():
    box, run = make_podman("running")
    assert asyncio.run(stop(SERVICE, run=run)) is None
    assert box["state"] is None


def test_stop_when_absent_does_nothing():
    box, run = make_podman(None)
    assert asyncio.run(stop(SERVICE, run=run)) is None
    assert box["calls"] == ["container"]


# --------------------------------------------------------------------------------
# Status shown on the page

@pytest.mark.parametrize("container, ready, action, expected", [
    (None, False, None, "stopped"),
    ("exited", False, None, "stopped"),
    ("running", False, None, "starting"),       # started from a shell, still loading
    ("running", True, None, "running"),
    ("running", True, "stop", "stopping"),
    (None, False, "start", "starting"),
])
def test_derive_status(container, ready, action, expected):
    assert derive_status(container=container, ready=ready, action=action) == expected
