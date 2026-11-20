import datetime
import json
import time
import types

from m97 import report
from m97.access import Decision
from m97.cli import main
from m97.services import Service

UTC = datetime.UTC


def make_hub(*, ready=True):
    """A stand-in for `m97.hub.Hub`, carrying the state `report.snapshot` reads."""
    gufo = Service(key="gufo", title="gufo", container="gufo", run_args=None, ready_url="", start_timeout=1,
                   port=8080, url_path="/v1")
    owui = Service(key="openwebui", title="Open WebUI", container="open-webui", run_args=None, ready_url="",
                   start_timeout=1, port=3000, browsable=True)
    now = time.monotonic()
    hub = types.SimpleNamespace(
        access=Decision(allowed=True, pin_required=False, reason="", network="home"),
        services={"gufo": gufo, "openwebui": owui},
        container={"gufo": "running" if ready else None, "openwebui": "running"},
        ready={"gufo": ready, "openwebui": True},
        scope={"gufo": "all" if ready else None, "openwebui": "local"},
        error={"gufo": "", "openwebui": ""},
        problem={"gufo": "" if ready else "gufo stopped unexpectedly at 2026-11-20 09:00.\n\nIts last log lines:\nboom",
                 "openwebui": ""},
        props={"model_alias": "flash-next", "build_info": "0.34.0 (2845939)",
               "default_generation_settings": {"n_ctx": 262144}},
        slots=[{"id": 0, "busy": True, "n_ctx": 262144, "prompt_tokens": 24, "decoded": 180, "tps": 22.0,
                "fill": (24 + 180) / 262144},
               {"id": 1, "busy": False, "n_ctx": 262144, "prompt_tokens": None, "decoded": None, "tps": None,
                "fill": None}],
        metrics={"llamacpp:requests_deferred": 0.0},
        gen_tps=51.0,
        latest={"ppt_w": 104.1, "cpu_temp_c": None, "gpu_busy_pct": 99, "cpu_thread_pct": {0: 3.0, 16: 74.0},
                "ram_used_gib": 113.3, "ram_total_gib": 123.5},
        cpu_cores=[[0, 16]],
        requests=[{"time": datetime.datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC), "prompt_tokens": 6509,
                   "cached_tokens": 6509, "generated_tokens": 300, "duration_ms": 6400.0, "ttft_ms": 10.0,
                   "decode_tps": 46.7, "prefill_tps": 0.0, "finish": "length"},
                  {"time": datetime.datetime(2026, 11, 20, 9, 1, 0, tzinfo=UTC), "prompt_tokens": 17,
                   "cached_tokens": 0, "generated_tokens": 838, "duration_ms": 92861.3, "ttft_ms": 125.6,
                   "decode_tps": 40.4, "prefill_tps": 138.8, "finish": "stop"}],
        history=[(now - 2.0, {"ppt_w": 20.0, "gpu_busy_pct": 3, "cpu_busy_pct": 1.0, "gen_tps": None}),
                 (now - 1.0, {"ppt_w": 104.1, "gpu_busy_pct": 99, "cpu_busy_pct": 7.0, "gen_tps": 51.0})],
        interval=1.0,
    )
    hub.status = lambda key: "running" if hub.ready[key] else "stopped"
    return hub


def test_snapshot_with_gufo_ready():
    payload = report.snapshot(make_hub())
    json.dumps(payload)                                                   # serializable as it stands
    assert payload["schema"] == report.SCHEMA
    assert payload["network"] == {"trusted": True, "name": "home"}
    assert payload["services"]["gufo"]["url"].endswith(":8080/v1")
    assert payload["services"]["openwebui"]["url"] == "http://127.0.0.1:3000"
    assert payload["model"] == {"name": "flash-next", "gufo_version": "0.34.0", "context_tokens": 262144}
    assert payload["generation"] == {"tps": 51.0, "deferred": 0.0}
    assert payload["sessions"][0]["generated_tokens"] == 180
    assert payload["host"]["cpu_thread_pct"] == {"0": 3.0, "16": 74.0}
    assert payload["host"]["cpu_temp_c"] is None, "a missing sensor is null"
    assert "history" not in payload


def test_requests_newest_first_with_derived_speeds():
    requests = report.snapshot(make_hub())["requests"]
    assert [r["time"] for r in requests] == ["2026-11-20T09:01:00Z", "2026-11-20T09:00:00Z"]
    assert requests[0]["output_tps"] == round(838 / ((92861.3 - 125.6) / 1000), 1)   # as the page shows it
    assert requests[0]["first_token_s"] == 0.13 and requests[0]["prefill_tps"] == 139
    assert requests[0]["decode_tps"] == 40.4
    assert requests[1]["prompt_cached"] is True and requests[0]["prompt_cached"] is False


def test_snapshot_with_gufo_down_reports_the_problem():
    payload = report.snapshot(make_hub(ready=False))
    assert payload["model"] is None and payload["generation"] is None and payload["sessions"] == []
    assert payload["services"]["gufo"]["url"] is None
    assert payload["services"]["gufo"]["error"].startswith("gufo stopped unexpectedly")


def test_history_only_on_request():
    payload = report.snapshot(make_hub(), history=True)
    history = payload["history"]
    assert history["interval_s"] == 1.0
    assert history["ppt_w"] == [20.0, 104.1]
    assert history["seconds_ago"][0] > history["seconds_ago"][1], "oldest first"


def test_format_report():
    text = report.format_report(report.snapshot(make_hub()))
    assert 'trusted network "home"' in text
    assert "flash-next, 256 Ki context, gufo 0.34.0" in text
    assert "51.0 tok/s; sessions: 22.0 tok/s, 1 idle; fullest context 0.1 %" in text
    assert "busiest thread 74 %" in text
    assert "17 → 838 tokens" in text and "(40.4 decoding)" in text
    assert "cached" in text


def test_format_report_with_gufo_down_shows_only_the_summary():
    text = report.format_report(report.snapshot(make_hub(ready=False)))
    assert "model       not ready" in text
    assert "gufo stopped unexpectedly" in text
    assert "boom" not in text, "the log lines are left to the page and `podman logs`"


def test_report_command_exits_1_when_nothing_answers(capsys):
    assert main(["report", "--url", "http://127.0.0.1:1"]) == 1
    assert "no answer from http://127.0.0.1:1/api/status" in capsys.readouterr().err


def test_numbers_have_the_page_decimals():
    hub = make_hub()
    hub.gen_tps = 3.997884663252176
    hub.latest = dict(hub.latest, ppt_w=59.6123, gpu_busy_pct=99, load1=2.0123, ram_total_gib=123.46,
                      some_new_sensor=1.23456)
    payload = report.snapshot(hub)
    assert payload["generation"]["tps"] == 4.0
    assert payload["host"]["ppt_w"] == 59.6
    assert payload["host"]["load1"] == 2.01
    assert payload["host"]["ram_total_gib"] == 123 and isinstance(payload["host"]["ram_total_gib"], int)
    assert payload["host"]["some_new_sensor"] == 1.23456, "a key the table does not know passes through"
    assert payload["sessions"][0]["fill"] == 0.001
