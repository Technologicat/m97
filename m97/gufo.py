"""Reading gufo: its Prometheus metrics, its `/slots` view, and its log lines.

Everything here parses defensively. These formats are gufo's, they can change between versions, and a
dashboard that crashes on an unexpected line is worse than one that shows a blank where the line was. So a
line or field that does not parse is skipped, and a missing field comes back as `None`.
"""

import datetime
import re

__all__ = ["parse_metrics",
           "parse_slots", "with_slot_rates",
           "parse_log_line", "completed_request", "output_tps"]

# What the metrics mean, as observed on gufo 0.34.0 (2026-11-18), three concurrent chat requests:
#
#   - `llamacpp:tokens_predicted_total` counts up *during* a request (204 tokens four seconds in), so its
#     change over a sampling interval is the server's total generation throughput, live.
#   - `llamacpp:tokens_predicted_seconds_total` and `prompt_seconds_total` stay at 0 until a request
#     finishes, and then add that request's own decode (prefill) time. Summed over concurrent requests,
#     they are per-request time, not wall time, so tokens over seconds from them is the average
#     per-request speed, which is below the total throughput whenever requests overlap.
#   - The gauges `predicted_tokens_seconds` and `prompt_tokens_seconds` read 0 mid-request, and afterwards
#     hold the *last finished* request's speed: 17.89 and 189.0 matched that request's logged `decode_tps`
#     and `prefill_tps` exactly. Not a window average, so not worth plotting.

_METRIC = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+(\S+)")


def parse_metrics(text):
    """Parse Prometheus text exposition into `{name: value}`, values as floats.

    Comments and lines that do not parse are skipped. A metric with labels is keyed by its name plus the
    label set as written, e.g. `name{a="b"}`; gufo 0.34.0 uses none.
    """
    metrics = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        match = _METRIC.match(line)
        if match is None:
            continue
        name, labels, value = match.groups()
        try:
            metrics[name + (labels or "")] = float(value)
        except ValueError:
            continue
    return metrics


def parse_slots(data):
    """Summarize gufo's `/slots` JSON (already decoded) as a list of dicts, one per session.

    Each has `id`, `busy` (bool), `n_ctx`, `prompt_tokens`, `decoded` (tokens generated so far in the
    current request) and `fill`, the share of the session's context in use, from 0 to 1. A field gufo did not
    send is `None`, as is `fill` when a field it needs is missing; an entry that is not a JSON object is
    skipped.
    """
    if not isinstance(data, list):
        return []
    slots = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        next_token = entry.get("next_token")
        maybe_first = next_token[0] if isinstance(next_token, list) and next_token else None
        n_ctx, prompt, decoded = (entry.get("n_ctx"), entry.get("n_prompt_tokens"),
                                  maybe_first.get("n_decoded") if isinstance(maybe_first, dict) else None)
        # `n_prompt_tokens` is the whole prompt, the part reused from the prompt cache included (6,509 tokens
        # both computed fresh and fully cached, on gufo 0.34.0), so prompt plus generated is what the
        # context holds.
        numbers = all(isinstance(x, (int, float)) for x in (n_ctx, prompt, decoded))
        slots.append({"id": entry.get("id"),
                      "busy": bool(entry.get("is_processing", False)),
                      "n_ctx": n_ctx,
                      "prompt_tokens": prompt,
                      "decoded": decoded,
                      "fill": min(1.0, (prompt + decoded) / n_ctx) if numbers and n_ctx > 0 else None})
    return slots


def with_slot_rates(slots, previous, now):
    """Add each busy session's live generation speed to `slots`, from the change in its decoded tokens.

    `slots` is what `parse_slots` returns, `previous` what the last call returned as its second value (start
    with `{}`), `now` a monotonic time in seconds. Returns `(slots, previous)`: copies of the slot dicts with
    `tps` added (`None` when idle, on the first sample of a request, or if the count went backwards, which
    means a new request began in that session), and the state for the next call.
    """
    rated, seen = [], {}
    for slot in slots:
        slot = dict(slot, tps=None)
        decoded = slot["decoded"]
        if slot["busy"] and isinstance(decoded, (int, float)):
            seen[slot["id"]] = (now, decoded)
            if slot["id"] in previous:
                t0, decoded0 = previous[slot["id"]]
                if now > t0 and decoded >= decoded0:
                    slot["tps"] = (decoded - decoded0) / (now - t0)
        rated.append(slot)
    return rated, seen


# `2026-11-18 08:56:50 [INFO] [http] key=value key=value ...`. gufo writes the timestamp in UTC.
_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\[(\w+)\]\s+\[([\w.-]+)\]\s+(.*)$")
_FIELD = re.compile(r"(\w+)=(\S+)")


def _number_or_string(text):
    for convert in (int, float):
        try:
            return convert(text)
        except ValueError:
            pass
    return text


def parse_log_line(line):
    """Parse one gufo log line into a dict, or return `None` if it is not in gufo's key=value format.

    The dict holds `time` (an aware UTC `datetime`), `level`, `component`, and every `key=value` field,
    with values converted to `int` or `float` where they look like numbers.
    """
    match = _LOG_LINE.match(line.strip())
    if match is None:
        return None
    stamp, level, component, rest = match.groups()
    try:
        time = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.UTC)
    except ValueError:
        return None
    record = {key: _number_or_string(value) for key, value in _FIELD.findall(rest)}
    record.update(time=time, level=level, component=component)
    return record


def completed_request(line):
    """Return the parsed record if `line` is a finished chat or completion request, else `None`.

    That is an `[http]` line with `event=completed` whose path ends in `/completions`: the requests with
    token counts and speeds, as opposed to health checks and model listings.
    """
    record = parse_log_line(line)
    if record is None or record["component"] != "http" or record.get("event") != "completed":
        return None
    path = record.get("path")
    if not isinstance(path, str) or not path.endswith("/completions"):
        return None
    return record


def output_tps(record):
    """The generation speed a finished request delivered: tokens generated over the wall-clock time from the
    first token to the end, from a `completed_request` record. `None` if a field it needs is missing.
    """
    # gufo's own `decode_tps` differs when requests overlap: on gufo 0.34.0, 838 tokens over 92.7 s beside a
    # long prefill came to 9.0 tok/s, against a logged `decode_tps` of 40.4, which would be 21 s of decoding.
    # It appears to leave out the time a request waits while another session is served.
    generated, duration, ttft = (record.get(k) for k in ("generated_tokens", "duration_ms", "ttft_ms"))
    if not all(isinstance(x, (int, float)) for x in (generated, duration, ttft)) or duration <= ttft:
        return None
    return generated / ((duration - ttft) / 1000)
