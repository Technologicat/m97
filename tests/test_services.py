import asyncio
import dataclasses
import pathlib
import sys

import pytest

from m97 import services
from m97.services import (Service, configured_services, derive_status, gufo_run_args, listen_scope,
                          openwebui_run_args, run, parse_proc_net_tcp, read_only_mounts, start, stop, unit_info,
                          unit_status)
from m97.settings import load, merge

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
    assert not any(a.endswith(":ro") for a in args)     # no mounts by default: the data volume only


def test_openwebui_args_mounts(tmp_path):
    (tmp_path / "lorebook").mkdir()
    openwebui = dict(SETTINGS["openwebui"], mounts=[f"{tmp_path}/lorebook:/lorebook"])
    args = openwebui_run_args(openwebui, SETTINGS["gufo"], lan=False)
    assert args[-3:] == ["-v", f"{tmp_path}/lorebook:/lorebook:ro", SETTINGS["openwebui"]["image"]]


def test_read_only_mounts(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "lorebook").mkdir()
    (tmp_path / "with:colon").mkdir()
    args = read_only_mounts(["~/lorebook:/lorebook",
                             "~/missing:/missing",
                             "~/with:colon:/inside",
                             "nocolon"])
    assert args == ["-v", f"{tmp_path}/lorebook:/lorebook:ro",
                    "-v", f"{tmp_path}/with:colon:/inside:ro"]
    assert "missing" in caplog.text and "nocolon" in caplog.text


# --------------------------------------------------------------------------------
# Start and stop, against a fake podman

def make_podman(state=None, *, dies=False, run_fails=False):
    """A fake `run` for podman: keeps one container's state, and records the commands it was given."""
    box = {"state": state, "calls": []}

    async def run(*args, timeout=60):
        args = args[args.index("podman"):]           # past any systemd-run prefix
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
                  ready_url="http://127.0.0.1:1/ready", start_timeout=10, port=1)


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
# Units

def make_systemd(active="inactive", *, exits=False, run_fails=False):
    """A fake `run` for systemctl, systemd-run and journalctl, keeping one unit's `ActiveState` in `box`."""
    box = {"active": active, "calls": []}

    async def run(*args, timeout=60):
        box["calls"].append(" ".join(args[:3]))
        if args[:3] == ("systemctl", "--user", "show"):
            result = "exit-code" if box["active"] == "failed" else "success"
            return 0, f"ActiveState={box['active']}\nResult={result}\nExecMainStatus=3\n", ""
        if args[0] == "systemd-run":
            box["command"] = args[args.index("--") + 1:]
            if run_fails:
                return 1, "", "Unit m97-x.service was already loaded or has a fragment file."
            box["active"] = "failed" if exits else "active"
            return 0, "", ""
        if args[:3] == ("systemctl", "--user", "reset-failed"):
            box["active"] = "inactive" if box["active"] == "failed" else box["active"]
            return 0, "", ""
        if args[:3] == ("systemctl", "--user", "stop"):
            box["active"] = "inactive"
            return 0, "", ""
        if args[0] == "journalctl":
            return 0, "Traceback (most recent call last):\nOSError: address already in use\n", ""
        raise AssertionError(f"unexpected call {args}")

    return box, run


UNIT = Service(key="raven-mcp", title="raven-mcp", unit="m97-x",
               run_args=lambda lan: [sys.executable, "-c", "pass", *(["--listen"] if lan else [])],
               ready_url="", start_timeout=10, port=1)


@pytest.fixture
def port(monkeypatch):
    """Whether something listens on the unit's port, as `listen_scope` will say; a dict to set `open` in."""
    box = {"open": False}
    monkeypatch.setattr(services, "listen_scope", lambda port: "local" if box["open"] else None)
    return box


def run_unit_start(run, ready, *, lan=False):
    return asyncio.run(start(UNIT, lan=lan, run=run, ready=ready, clock=fake_clock(), poll=0))


@pytest.mark.parametrize("active, listening, expected", [
    ("active", True, "running"),
    ("activating", False, "running"),       # on its way up, not listening yet
    ("deactivating", True, "running"),
    ("failed", False, "failed"),
    ("inactive", True, "outside"),          # run by hand
    ("inactive", False, None),
    (None, False, None),                     # not asked yet
])
def test_unit_status(active, listening, expected):
    assert unit_status(active, listening=listening) == expected


def test_unit_info_of_an_unknown_unit_is_inactive():
    async def run(*args, timeout=60):
        return 1, "", "Unit m97-x.service could not be found."
    assert asyncio.run(unit_info("m97-x", run=run))["ActiveState"] == "inactive"


def test_unit_start_runs_the_command_in_a_transient_unit(port):
    box, run = make_systemd()
    assert run_unit_start(run, ready_after(2), lan=True) is None
    assert box["active"] == "active"
    assert box["command"] == (sys.executable, "-c", "pass", "--listen")
    assert "systemctl --user reset-failed" not in box["calls"], "nothing failed, so nothing to reset"


def test_unit_start_resets_a_failed_unit_first(port):
    box, run = make_systemd("failed")
    assert run_unit_start(run, ready_after(0)) is None
    assert box["calls"].index("systemctl --user reset-failed") < box["calls"].index("systemd-run --user --unit=m97-x")


def test_unit_start_refuses_a_port_taken_outside_m97(port):
    port["open"] = True
    box, run = make_systemd()
    assert "started outside m97" in run_unit_start(run, ready_after(0))
    assert not any(call.startswith("systemd-run") for call in box["calls"])


def test_unit_start_reports_a_missing_command(port):
    box, run = make_systemd()
    missing = dataclasses.replace(UNIT, run_args=lambda lan: ["/nonexistent/raven-mcp"])
    message = asyncio.run(start(missing, lan=False, run=run, ready=ready_after(0), clock=fake_clock(), poll=0))
    assert "/nonexistent/raven-mcp is not an executable file" in message
    assert not any(call.startswith("systemd-run") for call in box["calls"])


def test_unit_start_reports_systemd_run_failure(port):
    _, run = make_systemd(run_fails=True)
    assert "already loaded" in run_unit_start(run, ready_after(0))


def test_unit_start_reports_death_with_its_journal(port):
    _, run = make_systemd(exits=True)
    message = run_unit_start(run, ready_after(100))
    assert "stopped while starting (state: failed)" in message and "address already in use" in message


def test_unit_stop_leaves_it_inactive(port):
    box, run = make_systemd("active")
    assert asyncio.run(stop(UNIT, run=run)) is None
    assert box["active"] == "inactive"
    assert box["calls"].index("systemctl --user stop") < box["calls"].index("systemctl --user reset-failed")


def test_unit_stop_leaves_a_service_started_outside_m97_alone(port):
    port["open"] = True
    box, run = make_systemd()
    assert "started outside m97" in asyncio.run(stop(UNIT, run=run))
    assert "systemctl --user stop" not in box["calls"]


def test_raven_services_are_off_unless_configured():
    assert [s.key for s in configured_services(SETTINGS)] == ["gufo", "openwebui"]


def test_raven_services_when_configured():
    settings = merge(SETTINGS, {"raven-server": {"command": "~/raven/bin/raven-server"},
                                "raven-mcp": {"command": "~/raven/bin/raven-mcp", "extra_args": ["--no-lorebook"]}})
    by_key = {s.key: s for s in configured_services(settings)}
    assert list(by_key) == ["gufo", "openwebui", "raven-server", "raven-mcp"]
    server, mcp = by_key["raven-server"], by_key["raven-mcp"]
    assert server.run_args(True) == [str(pathlib.Path("~/raven/bin/raven-server").expanduser()),
                                     "--port", "5100", "--listen"]
    assert mcp.run_args(False)[1:] == ["--port", "8130", "--server-url", "http://127.0.0.1:5100", "--no-lorebook"]
    assert mcp.after == ("raven-server",) and mcp.url_path == "/mcp" and not mcp.ready_url


def test_raven_mcp_alone_uses_its_own_raven_server_setting():
    settings = merge(SETTINGS, {"raven-mcp": {"command": "/opt/raven-mcp"}})
    (mcp,) = [s for s in configured_services(settings) if s.key == "raven-mcp"]
    assert "--server-url" not in mcp.run_args(False)


# --------------------------------------------------------------------------------
# Status shown on the page

@pytest.mark.parametrize("state, ready, action, expected", [
    (None, False, None, "stopped"),
    ("exited", False, None, "stopped"),
    ("running", False, None, "starting"),       # started from a shell, still loading
    ("running", True, None, "running"),
    ("running", True, "stop", "stopping"),
    (None, False, "start", "starting"),
])
def test_derive_status(state, ready, action, expected):
    assert derive_status(state=state, ready=ready, action=action) == expected


def test_derive_status_unknown_until_readiness_is_checked():
    assert derive_status(state="running", ready=False, action=None, ready_known=False) == "unknown"
    assert derive_status(state=None, ready=False, action=None, ready_known=False) == "stopped"
    assert derive_status(state="running", ready=False, action="stop", ready_known=False) == "stopping"


# --------------------------------------------------------------------------------
# Where a service listens

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1
   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2
   2: 0100007F:0BB8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 3
   3: 3A32A8C0:1F90 1F32A8C0:D2F0 01 00000000:00000000 00:00000000 00000000  1000        0 4
"""


def test_parse_proc_net_tcp():
    assert parse_proc_net_tcp(TCP, 8080) == ["00000000"]       # the established connection is not listening
    assert parse_proc_net_tcp(TCP, 3000) == ["0100007F"]
    assert parse_proc_net_tcp(TCP, 9999) == []


def test_listen_scope(tmp_path):
    (tmp_path / "proc/net").mkdir(parents=True)
    (tmp_path / "proc/net/tcp").write_text(TCP)
    assert listen_scope(8080, root=tmp_path) == "all"
    assert listen_scope(3000, root=str(tmp_path)) == "local"
    assert listen_scope(9999, root=tmp_path) is None
    assert listen_scope(8080, root=tmp_path / "nowhere") is None


# --------------------------------------------------------------------------------
# Running commands

@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_run_returns_when_the_command_exits_even_if_a_child_keeps_its_output_open(loop):
    # `podman run -d` leaves conmon behind holding the command's stdout; waiting for end-of-file on it waited
    # for the container to exit (under uvloop, which NiceGUI runs on). A backgrounded `sleep` does the same.
    runner = asyncio.run if loop == "asyncio" else pytest.importorskip("uvloop").run
    code, out, err = runner(run("sh", "-c", "sleep 3 & echo hi", timeout=2))
    assert (code, out) == (0, "hi\n"), f"run waited for the background child: {code}, {err!r}"


def test_run_times_out():
    code, _, err = asyncio.run(run("sleep", "5", timeout=0.2))
    assert code == 124 and "timed out" in err


def test_podman_run_goes_in_a_scope_of_its_own(monkeypatch):
    # Otherwise conmon stays in m97's cgroup, and restarting m97 kills the containers.
    seen = []
    box, fake = make_podman()

    async def run(*args, timeout=60):
        seen.append(args)
        return await fake(*args, timeout=timeout)

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    asyncio.run(start(SERVICE, lan=False, run=run, ready=ready_after(0), clock=fake_clock(), poll=0))
    podman_run = next(a for a in seen if "run" in a and "podman" in a and a[a.index("podman") + 1] == "run")
    assert podman_run[:3] == ("systemd-run", "--user", "--scope")
