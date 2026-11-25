"""The parsers, against output captured from gufo 0.34.0 on 2026-11-18: idle, mid-way through three concurrent
chat requests, and just after them."""

import datetime
import json
import pathlib

import pytest

from m97.gufo import (completed_request, output_tps, parse_log_line, parse_metrics, parse_slots, prefill_summary,
                      with_slot_rates)

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
    assert slots[0]["fill"] == pytest.approx((24 + 73) / 262144)


def test_slots_fill_needs_every_field():
    data = fixture_json("busy-slots.json")
    del data[0]["n_prompt_tokens"]
    data[1]["n_ctx"] = 0
    slots = parse_slots(data)
    assert slots[0]["fill"] is None
    assert slots[1]["fill"] is None
    assert slots[2]["fill"] is not None


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


def test_slots_prefilling():
    # Taken mid-prefill on gufo 0.34.0: one chunk of a 40,410-token prompt computed, none of it cached.
    busy, idle = parse_slots(fixture_json("prefill-slots.json"))
    assert busy["prefilling"] and busy["task"] == 74
    assert (busy["cached"], busy["prefill_done"], busy["prefill_total"]) == (0, 4096, 40410)
    assert not idle["prefilling"] and idle["prefill_total"] == 0


def test_slots_generating_are_done_prefilling():
    # Decoding, its prompt computed: the cached part plus the processed part make the whole prompt.
    slots = parse_slots(fixture_json("busy-slots.json"))
    assert [slot["prefilling"] for slot in slots] == [False, False, False]


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

def slot(sid, busy, decoded, *, task=7, done=24, total=24):
    """A parsed slot: `done` of the prompt's `total` uncached tokens prefilled."""
    return {"id": sid, "busy": busy, "task": task, "n_ctx": 262144, "prompt_tokens": 24, "decoded": decoded,
            "fill": None, "cached": 0, "prefill_total": total, "prefill_done": done,
            "prefilling": busy and done < total}


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


def test_a_new_request_in_the_same_session_starts_over():
    _, previous = with_slot_rates([slot(0, True, 10, task=1)], {}, 10.0)
    slots, _ = with_slot_rates([slot(0, True, 30, task=2)], previous, 11.0)
    assert slots[0]["tps"] is None, "another task: its count has nothing to do with the last one's"


def prefill_run(samples, *, total=20224):
    """`with_slot_rates` over `(time, done)` samples of one session prefilling; the slots after each."""
    previous, out = {}, []
    for now, done in samples:
        slots, previous = with_slot_rates([slot(0, True, 0, done=done, total=total)], previous, now)
        out.append(slots[0])
    return out


def test_prefill_speed_from_its_start_is_known_at_the_first_chunk():
    # The shape seen on gufo 0.34.0: 4,096 tokens at a time, about 3 s apart.
    out = prefill_run([(0.0, 0), (1.0, 0), (2.0, 0), (3.0, 4096), (4.0, 4096), (5.0, 4096), (6.0, 8192)])
    assert [s["prefill_tps"] for s in out[:3]] == [None, None, None]
    assert out[3]["prefill_tps"] == pytest.approx(4096 / 3)
    assert out[4]["prefill_tps"] == pytest.approx(4096 / 3), "steady between chunks, not sagging"
    assert out[6]["prefill_tps"] == pytest.approx(8192 / 6)
    assert out[3]["prefill_eta_s"] == pytest.approx((20224 - 4096) / (4096 / 3))
    assert out[5]["prefill_eta_s"] == pytest.approx(out[3]["prefill_eta_s"] - 2), "counts down between chunks"


def test_prefill_first_seen_midway_waits_for_two_chunks():
    out = prefill_run([(0.0, 4096), (3.0, 8192), (6.0, 12288)])
    assert out[0]["prefill_tps"] is None and out[1]["prefill_tps"] is None, (
        "when the first chunk began is not known, one step is not a speed")
    assert out[2]["prefill_tps"] == pytest.approx(4096 / 3)


def test_prefill_done_is_not_prefilling():
    out = prefill_run([(0.0, 0), (3.0, 20224)])
    assert not out[1]["prefilling"] and out[1]["prefill_tps"] is None
    assert prefill_summary(out[1:]) is None


def test_prefill_summary_of_two_sessions():
    previous = {}
    for now, done in ((0.0, 0), (3.0, 4096)):
        slots, previous = with_slot_rates([slot(0, True, 0, done=done, total=8192),
                                           slot(1, True, 0, done=done // 2, total=10000),
                                           slot(2, True, 50)], previous, now)
    summary = prefill_summary(slots)
    assert summary["sessions"] == 2
    assert (summary["done"], summary["total"]) == (4096 + 2048, 8192 + 10000)
    assert summary["tps"] == pytest.approx(4096 / 3 + 2048 / 3)
    assert summary["eta_s"] == pytest.approx((10000 - 2048) / (2048 / 3)), "the longest time left"


# --------------------------------------------------------------------------------
# Delivered generation speed

def test_output_tps_is_wall_clock_from_first_token():
    lines = fixture_text("gufo-log.txt").splitlines()
    record = next(r for r in map(completed_request, lines) if r is not None and r["request"] == "r349")
    # 400 tokens over 19664.6 - 283.3 ms; gufo logged decode_tps=20.8 for the same request
    assert output_tps(record) == pytest.approx(400 / ((19664.6 - 283.3) / 1000))


@pytest.mark.parametrize("record", [{}, {"generated_tokens": 10, "duration_ms": 100.0},
                                    {"generated_tokens": 10, "duration_ms": 100.0, "ttft_ms": 100.0},
                                    {"generated_tokens": "x", "duration_ms": 100.0, "ttft_ms": 1.0}])
def test_output_tps_missing_or_degenerate(record):
    assert output_tps(record) is None
