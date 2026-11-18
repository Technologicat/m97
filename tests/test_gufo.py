"""The parsers, against output captured from gufo 0.34.0 on 2026-11-18: idle, mid-way through three concurrent
chat requests, and just after them."""

import datetime
import json
import pathlib

import pytest

from m97.gufo import completed_request, parse_log_line, parse_metrics, parse_slots, with_slot_rates

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name):
    return json.loads(fixture_text(name))


# --------------------------------------------------------------------------------
# Metrics

def test_metrics_idle():
    metrics = parse_metrics(fixture_text("idle-metrics.txt"))
    assert metrics["llamacpp:tokens_predicted_total"] == 0
    assert metrics["llamacpp:requests_processing"] == 0
    assert metrics["gufo_device_lost_total"] == 0
    assert not any(name.startswith("#") for name in metrics)


def test_metrics_busy():
    metrics = parse_metrics(fixture_text("busy-metrics.txt"))
    assert metrics["llamacpp:requests_processing"] == 3
    assert metrics["llamacpp:tokens_predicted_total"] == 204
    assert metrics["llamacpp:kv_cache_usage_ratio"] == pytest.approx(0.0003509521484375)


def test_metrics_after_has_no_comment_lines():
    metrics = parse_metrics(fixture_text("after-metrics.txt"))
    assert metrics["llamacpp:tokens_predicted_total"] == 1200
    assert metrics["llamacpp:tokens_predicted_seconds_total"] == pytest.approx(61.229611329)


def test_metrics_missing_and_malformed_lines():
    text = fixture_text("busy-metrics.txt")
    assert "llamacpp:requests_processing 3\n" in text, "the fixture changed, so this test removes nothing"
    text = text.replace("llamacpp:requests_processing 3\n", "")         # a metric gufo stopped sending
    text += "garbage line without a value\nllamacpp:broken notanumber\n"
    metrics = parse_metrics(text)
    assert "llamacpp:requests_processing" not in metrics
    assert "llamacpp:broken" not in metrics
    assert metrics["llamacpp:tokens_predicted_total"] == 204


def test_metrics_labels_and_exponents():
    metrics = parse_metrics('a_total{model="x"} 1e3\nb 2.5\n')
    assert metrics == {'a_total{model="x"}': 1000.0, "b": 2.5}


# --------------------------------------------------------------------------------
# Slots

def test_slots_idle():
    slots = parse_slots(fixture_json("idle-slots.json"))
    assert len(slots) == 3
    assert not any(slot["busy"] for slot in slots)
    assert {slot["n_ctx"] for slot in slots} == {262144}


def test_slots_busy():
    slots = parse_slots(fixture_json("busy-slots.json"))
    assert [slot["busy"] for slot in slots] == [True, True, True]
    assert [slot["decoded"] for slot in slots][:2] == [73, 88]
    assert slots[0]["prompt_tokens"] == 24


def test_slots_missing_fields():
    data = fixture_json("busy-slots.json")
    del data[0]["next_token"]
    del data[1]["is_processing"]
    data[2]["next_token"] = []
    data.append("not an object")
    slots = parse_slots(data)
    assert len(slots) == 3
    assert slots[0]["decoded"] is None
    assert slots[1]["busy"] is False
    assert slots[2]["decoded"] is None


@pytest.mark.parametrize("data", [None, {}, "text", 42])
def test_slots_not_a_list(data):
    assert parse_slots(data) == []


# --------------------------------------------------------------------------------
# Log lines

def test_log_completed_chat():
    lines = fixture_text("gufo-log.txt").splitlines()
    requests = [r for r in map(completed_request, lines) if r is not None]
    assert [r["request"] for r in requests] == ["r349", "r351", "r350"]
    first = requests[0]
    assert first["time"] == datetime.datetime(2026, 11, 18, 8, 56, 50, tzinfo=datetime.UTC)
    assert first["status"] == 200
    assert first["generated_tokens"] == 400
    assert first["decode_tps"] == pytest.approx(20.8)
    assert first["finish"] == "length"
    assert first["plan"] == "batched-w3"


def test_log_other_lines_are_not_requests():
    lines = fixture_text("gufo-log.txt").splitlines()
    listening, received, models_done = lines[0], lines[1], lines[2]
    assert parse_log_line(listening)["component"] == "server"
    assert completed_request(listening) is None
    assert completed_request(received) is None                         # not finished yet
    assert parse_log_line(models_done)["event"] == "completed"
    assert completed_request(models_done) is None                      # finished, but not a chat


def test_log_field_missing():
    line = next(line for line in fixture_text("gufo-log.txt").splitlines() if "r349 event=completed" in line)
    assert " decode_tps=20.8" in line, "the fixture changed, so this test removes nothing"
    record = completed_request(line.replace(" decode_tps=20.8", ""))
    assert record is not None
    assert record.get("decode_tps") is None
    assert record["generated_tokens"] == 400


@pytest.mark.parametrize("line", ["", "plain text", "2026-11-18 [INFO] [http] event=completed",
                                  "2026-13-45 08:56:50 [INFO] [http] event=completed path=/v1/chat/completions"])
def test_log_unparseable(line):
    assert parse_log_line(line) is None
    assert completed_request(line) is None


# --------------------------------------------------------------------------------
# Live speed per session

def slot(sid, busy, decoded):
    return {"id": sid, "busy": busy, "n_ctx": 262144, "prompt_tokens": 24, "decoded": decoded}


def test_slot_rates():
    slots, previous = with_slot_rates([slot(0, True, 73), slot(1, True, 88), slot(2, False, None)], {}, 10.0)
    assert [s["tps"] for s in slots] == [None, None, None], "a first sample has nothing to compare with"
    slots, previous = with_slot_rates([slot(0, True, 95), slot(1, True, 5), slot(2, False, None)], previous, 11.0)
    assert slots[0]["tps"] == pytest.approx(22.0)
    assert slots[1]["tps"] is None, "the count went down: a new request began in that session"
    assert slots[2]["tps"] is None
    slots, previous = with_slot_rates([slot(0, False, None), slot(1, True, 25), slot(2, False, None)], previous, 12.0)
    assert slots[0]["tps"] is None and 0 not in previous, "an idle session forgets its count"
    assert slots[1]["tps"] == pytest.approx(20.0)
