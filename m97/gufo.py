"""Reading gufo: its Prometheus metrics, its `/slots` view, and its log lines.

Everything here parses defensively. These formats are gufo's, they can change between versions, and a
dashboard that crashes on an unexpected line is worse than one that shows a blank where the line was. So a
line or field that does not parse is skipped, and a missing field comes back as `None`.
"""

import datetime
import re

__all__ = ["parse_metrics",
           "parse_slots", "with_slot_rates", "prefill_summary",
           "parse_log_line", "completed_request", "output_tps", "prompt_fully_cached"]

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

    Each has `id`, `busy` (bool), `task` (gufo's id of the request it serves), `n_ctx`, `prompt_tokens`,
    `decoded` (tokens generated so far in the current request) and `fill`, the share of the session's context
    in use, from 0 to 1. For the prompt's prefill: `cached` (tokens of the prompt reused from the cache),
    `prefill_total` (the rest, which have to be computed), `prefill_done` (how many of those are), and
    `prefilling` (bool), true while some are left. A field gufo did not send is `None`, as are the derived
    ones when a field they need is missing; an entry that is not a JSON object is skipped.
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
        busy = bool(entry.get("is_processing", False))
        # While a prompt is prefilled, `n_prompt_tokens_processed` counts the tokens computed so far, in steps of
        # gufo's prefill chunk (4,096 tokens, every 3 s or so for a 20k prompt beside another session's decoding,
        # on gufo 0.34.0); the prefill is done once that plus the cached part makes the whole prompt. `state`
        # read 1 throughout, prefill and decoding alike, so it cannot tell them apart.
        cached, done = entry.get("n_prompt_tokens_cache"), entry.get("n_prompt_tokens_processed")
        prefill_numbers = all(isinstance(x, (int, float)) for x in (prompt, cached, done))
        total = max(0, prompt - cached) if prefill_numbers else None
        slots.append({"id": entry.get("id"),
                      "busy": busy,
                      "task": entry.get("id_task"),
                      "n_ctx": n_ctx,
                      "prompt_tokens": prompt,
                      "decoded": decoded,
                      "fill": min(1.0, (prompt + decoded) / n_ctx) if numbers and n_ctx > 0 else None,
                      "cached": cached if prefill_numbers else None,
                      "prefill_total": total,
                      "prefill_done": min(done, total) if prefill_numbers else None,
                      "prefilling": busy and prefill_numbers and done < total})
    return slots


def with_slot_rates(slots, previous, now):
    """Add each busy session's live generation and prefill speeds to `slots`.

    `slots` is what `parse_slots` returns, `previous` what the last call returned as its second value (start
    with `{}`), `now` a monotonic time in seconds. Returns `(slots, previous)`: copies of the slot dicts with
    these added, and the state for the next call:

    - `tps`, the generation speed, from the change in decoded tokens since the last call; `None` when idle,
      on the first sample of a request, or if the count went backwards, which means a new request began.
    - `prefill_tps`, while prefilling, the speed over the prefill so far; `None` until it is known, which is
      at its first step for a prefill seen from its start, and at its second for one first seen midway.
    - `prefill_eta_s`, the seconds of prefill left at that speed, or `None` without one.
    """
    rated, seen = [], {}
    for slot in slots:
        slot = dict(slot, tps=None, prefill_tps=None, prefill_eta_s=None)
        decoded, maybe_before = slot["decoded"], previous.get(slot["id"])
        if slot["busy"] and isinstance(decoded, (int, float)):
            state = seen[slot["id"]] = {"t": now, "decoded": decoded, "task": slot["task"], "prefill": None}
            same_request = maybe_before is not None and maybe_before["task"] == slot["task"]
            if same_request and now > maybe_before["t"] and decoded >= maybe_before["decoded"]:
                slot["tps"] = (decoded - maybe_before["decoded"]) / (now - maybe_before["t"])
            if slot["prefilling"]:
                state["prefill"] = _prefill_progress(slot, maybe_before["prefill"] if same_request else None, now)
                slot["prefill_tps"], slot["prefill_eta_s"] = _prefill_speed(slot, state["prefill"], now)
        rated.append(slot)
    return rated, seen


# The prefill count moves in whole chunks, a few seconds apart, so its change between two samples is 0 most of
# the time and a whole chunk now and then. Measured from first seen to each sample, the speed would sag between
# chunks and jump at each, a sawtooth; so it is measured between two moments the count was seen to change,
# the first and the latest. A prefill first seen with nothing done yet has just begun, at most one sample ago,
# so that moment serves as the first: the speed is then known at the first chunk. One first seen midway, say
# after m97 restarted, has to wait for a second change. The time left counts down from the latest change.

def _prefill_progress(slot, maybe_progress, now):
    """When the prefill's count was seen to change: `{"done": count, "t0", "n0", "t", "n"}`, the first change
    and the latest; both `None` until there is one."""
    done = slot["prefill_done"]
    if maybe_progress is None:
        if done == 0:   # just begun
            return {"done": done, "t0": now, "n0": 0, "t": now, "n": 0}
        return {"done": done, "t0": None, "n0": None, "t": None, "n": None}
    progress = dict(maybe_progress, done=done)
    if done != maybe_progress["done"]:
        if progress["t0"] is None:
            progress["t0"], progress["n0"] = now, done
        progress["t"], progress["n"] = now, done
    return progress


def _prefill_speed(slot, progress, now):
    """`(prefill_tps, prefill_eta_s)` from `_prefill_progress`'s record, `None` for what is not known yet."""
    if progress["t0"] is None or progress["t"] <= progress["t0"] or progress["n"] <= progress["n0"]:
        return None, None
    tps = (progress["n"] - progress["n0"]) / (progress["t"] - progress["t0"])
    left = (slot["prefill_total"] - progress["n"]) / tps - (now - progress["t"])
    return tps, max(0.0, left)


def prefill_summary(slots):
    """The prefill under way across the sessions of `with_slot_rates`' slots, or `None` if there is none.

    A dict: `sessions`, how many are prefilling; `done` and `total` tokens, summed; `tps`, their summed speed,
    or `None` while none is known; and `eta_s`, the longest time left, or `None` while any is unknown.
    """
    prefilling = [s for s in slots if s.get("prefilling")]
    if not prefilling:
        return None
    speeds = [s["prefill_tps"] for s in prefilling if s["prefill_tps"] is not None]
    etas = [s["prefill_eta_s"] for s in prefilling]
    return {"sessions": len(prefilling),
            "done": sum(s["prefill_done"] for s in prefilling),
            "total": sum(s["prefill_total"] for s in prefilling),
            "tps": sum(speeds) if speeds else None,
            "eta_s": None if None in etas else max(etas)}


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


def prompt_fully_cached(record):
    """Whether a finished request's whole prompt came from gufo's prompt cache, from a `completed_request`
    record. Its `prefill_tps` is then 0, nothing having been prefilled."""
    prompt, cached = record.get("prompt_tokens"), record.get("cached_tokens")
    return isinstance(prompt, int) and isinstance(cached, int) and prompt > 0 and cached >= prompt
