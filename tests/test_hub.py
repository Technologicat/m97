import asyncio

from m97 import hub as hub_module
from m97.settings import load


def make_hub(monkeypatch, *, start, stop):
    monkeypatch.setattr(hub_module.services, "start", start)
    monkeypatch.setattr(hub_module.services, "stop", stop)
    hub = hub_module.Hub(load("/nonexistent/settings.toml"))

    async def decision():
        return hub.access
    monkeypatch.setattr(hub, "decision", decision)
    return hub


def test_stop_cancels_a_start_under_way(monkeypatch):
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
        hub = make_hub(monkeypatch, start=start, stop=stop)
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


def test_failed_start_reports_error(monkeypatch):
    async def start(service, *, lan):
        return "gufo stopped while starting"

    async def stop(service):
        return None

    async def scenario():
        hub = make_hub(monkeypatch, start=start, stop=stop)
        hub.start_service("gufo")
        await asyncio.sleep(0.01)
        return hub

    hub = asyncio.run(scenario())
    assert hub.error["gufo"] == "gufo stopped while starting"
    assert hub.action["gufo"] is None
