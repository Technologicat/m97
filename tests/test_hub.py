import asyncio

from m97 import hub as hub_module
from m97.settings import load


def make_hub(monkeypatch, tmp_path, *, start, stop):
    monkeypatch.setattr(hub_module.services, "start", start)
    monkeypatch.setattr(hub_module.services, "stop", stop)
    hub = hub_module.Hub(load("/nonexistent/settings.toml"), state_path=tmp_path / "state.json")

    async def decision():
        return hub.access
    monkeypatch.setattr(hub, "decision", decision)
    return hub


def test_stop_cancels_a_start_under_way(monkeypatch, tmp_path):
    events = []

    async def start(service, *, lan):
        events.append("start")
        try:
            await asyncio.sleep(3600)          # a slow load
        except asyncio.CancelledError:
            events.append("start cancelled")
            raise

    async def stop(service):
        events.append("stop")

    async def scenario():
        hub = make_hub(monkeypatch, tmp_path, start=start, stop=stop)
        hub.start_service("gufo")
        await asyncio.sleep(0.01)
        assert hub.status("gufo") == "starting"
        hub.stop_service("gufo")
        await asyncio.sleep(0.01)
        return hub

    hub = asyncio.run(scenario())
    assert events == ["start", "start cancelled", "stop"]
    assert hub.action["gufo"] is None
    assert hub.error["gufo"] == ""


def test_failed_start_reports_error(monkeypatch, tmp_path):
    async def start(service, *, lan):
        return "gufo stopped while starting"

    async def stop(service):
        return None

    async def scenario():
        hub = make_hub(monkeypatch, tmp_path, start=start, stop=stop)
        hub.start_service("gufo")
        await asyncio.sleep(0.01)
        return hub

    hub = asyncio.run(scenario())
    assert hub.error["gufo"] == "gufo stopped while starting"
    assert hub.action["gufo"] is None


def test_wanted_state_is_remembered_and_a_crash_reported_after_a_reboot(monkeypatch, tmp_path):
    async def nothing(*args, **kwargs):
        return None

    hub = make_hub(monkeypatch, tmp_path, start=nothing, stop=nothing)
    hub.container["gufo"] = "running"
    hub._remember(now=100.0)
    assert hub.want["gufo"] == "running"
    # It dies by itself; the crash is seen live, then it stays down past the grace period.
    hub.container["gufo"] = "exited"
    hub._crash["gufo"] = "gufo stopped unexpectedly"
    hub._remember(now=101.0)
    assert hub.want["gufo"] == "running", "not yet: a shutdown would look the same for these few seconds"
    hub._remember(now=131.0)
    assert hub.want["gufo"] == "stopped"
    # After a restart of m97, the state and the reason are back.
    again = make_hub(monkeypatch, tmp_path, start=nothing, stop=nothing)
    assert again.want["gufo"] == "stopped"
    assert again.problem["gufo"] == "gufo stopped unexpectedly"


def test_pending_autostart_is_not_forgotten_while_waiting_for_the_network(monkeypatch, tmp_path):
    async def nothing(*args, **kwargs):
        return None

    hub = make_hub(monkeypatch, tmp_path, start=nothing, stop=nothing)
    hub.want["gufo"] = "running"
    hub._autostart_pending.add("gufo")
    hub._remember(now=0.0)
    hub._remember(now=100.0)       # longer than the grace period, still waiting
    assert hub.want["gufo"] == "running"
    hub._autostart_pending.clear()
    hub._remember(now=101.0)
    hub._remember(now=200.0)
    assert hub.want["gufo"] == "stopped", "control: without the pending autostart, the grace period applies"


def test_autostart_starts_gufo_first_and_open_webui_after_it(monkeypatch, tmp_path):
    events = []

    async def start(service, *, lan):
        events.append(f"start {service.key}")
        await asyncio.sleep(0.05)              # a load
        events.append(f"ready {service.key}")

    async def nothing(*args, **kwargs):
        return None

    async def scenario():
        hub = make_hub(monkeypatch, tmp_path, start=start, stop=nothing)
        monkeypatch.setattr(hub_module.services, "container_state", nothing)
        monkeypatch.setattr(hub_module.access, "active_connections", lambda: [("uuid", [
            hub_module.access.ipaddress.ip_interface("192.168.1.5/24")])])
        hub.want = {"openwebui": "running", "gufo": "running"}
        await hub._autostart()
        await asyncio.gather(*hub._action_tasks.values())

    asyncio.run(scenario())
    assert events == ["start gufo", "ready gufo", "start openwebui", "ready openwebui"]


def test_a_running_container_is_unknown_until_its_readiness_is_checked(monkeypatch, tmp_path):
    # Just after m97 starts, the container is known to run but nobody has asked the service yet: saying
    # "starting" there told an agent a server up for hours was still loading.
    async def nothing(*args, **kwargs):
        return None

    hub = make_hub(monkeypatch, tmp_path, start=nothing, stop=nothing)
    monkeypatch.setattr(hub_module.services, "answers", lambda url: True)
    monkeypatch.setattr(hub_module.services, "listen_scope", lambda port: "all")
    hub._sample = lambda: {}
    hub._on_container("open-webui", "running", None)
    assert hub.status("openwebui") == "unknown"
    asyncio.run(hub._tick())
    assert hub.status("openwebui") == "running"
    hub._on_container("open-webui", "exited", None)
    hub._on_container("open-webui", "running", None)
    assert hub.status("openwebui") == "unknown", "a restarted container is checked afresh"
