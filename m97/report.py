"""The dashboard's data as one JSON-ready dict, and as text for `m97 report`.

`snapshot` builds the payload `GET /api/status` serves, from the hub; `format_report` turns a payload back
into a few lines of text. Both are pure: the first reads the hub and touches nothing else, the second reads
only the payload, so `m97 report` can format what another machine's m97 sent.
"""

import datetime
import socket
import time

from . import __version__, gufo, services

__all__ = ["SCHEMA", "snapshot", "format_report"]

# The payload's shape. Bump when a key changes meaning or goes away; adding a key needs no bump.
SCHEMA = 1


def _iso(when):
    """An aware `datetime` as ISO 8601 in UTC, seconds precision, with a `Z`."""
    return when.astimezone(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ms_to_s(value):
    return value / 1000 if isinstance(value, (int, float)) else None


def _request(record):
    return {"time": _iso(record["time"]),
            "prompt_tokens": record.get("prompt_tokens"),
            "generated_tokens": record.get("generated_tokens"),
            "output_tps": gufo.output_tps(record),
            "decode_tps": record.get("decode_tps"),
            "prefill_tps": record.get("prefill_tps"),
            "prompt_cached": gufo.prompt_fully_cached(record),
            "first_token_s": _ms_to_s(record.get("ttft_ms")),
            "duration_s": _ms_to_s(record.get("duration_ms")),
            "finish": record.get("finish")}


def snapshot(hub, *, history=False):
    """What the dashboard shows, as a JSON-ready dict; the README's "The data as JSON" lists the keys.

    `history` adds the sparkline series, oldest first.
    """
    access = hub.access
    payload = {
        "schema": SCHEMA,
        "m97_version": __version__,
        "time": _iso(datetime.datetime.now(datetime.UTC)),
        "hostname": socket.gethostname(),
        "network": {"trusted": access.allowed, "name": access.network},
        "services": {key: {"status": hub.status(key),
                           "container": hub.container[key],
                           "url": services.service_url(service, hub.scope[key]),
                           "scope": hub.scope[key],
                           "error": (hub.error[key] or hub.problem[key]) or None}
                     for key, service in hub.services.items()},
        "model": None,
        "generation": None,
        "sessions": [],
        "host": dict(hub.latest, cpu_thread_pct={str(cpu): pct for cpu, pct in
                                                 (hub.latest.get("cpu_thread_pct") or {}).items()},
                     cpu_cores=hub.cpu_cores),
        "requests": [_request(r) for r in reversed(hub.requests)],
    }
    if hub.ready["gufo"]:
        props = hub.props or {}
        payload["model"] = {"name": props.get("model_alias") or props.get("model"),
                            "gufo_version": (props.get("build_info") or "").split(" ")[0] or None,
                            "context_tokens": (props.get("default_generation_settings") or {}).get("n_ctx")}
        payload["generation"] = {"tps": hub.gen_tps,
                                 "deferred": hub.metrics.get("llamacpp:requests_deferred")}
        payload["sessions"] = [{"id": s["id"], "busy": s["busy"], "tps": s.get("tps"),
                                "prompt_tokens": s["prompt_tokens"], "generated_tokens": s["decoded"],
                                "context_tokens": s["n_ctx"], "fill": s["fill"]}
                               for s in hub.slots]
    if history:
        now = time.monotonic()
        series = ("ppt_w", "gpu_busy_pct", "cpu_busy_pct", "gen_tps")
        payload["history"] = {"interval_s": hub.interval,
                              "seconds_ago": [round(now - t, 1) for t, _ in hub.history],
                              **{name: [values.get(name) for _, values in hub.history] for name in series}}
    return payload


# --------------------------------------------------------------------------------
# Text

def _fmt(value, spec, unit=""):
    return "–" if value is None else f"{value:{spec}}{unit}"


def _service_line(title, service):
    where = {"all": "all interfaces", "local": "this machine only"}.get(service["scope"])
    address = f"   {service['url']} ({where})" if service["url"] else ""
    line = f"{title:<11} {service['status']:<9}{address}"
    if service["error"]:
        line += "\n" + " " * 12 + service["error"].partition("\n\n")[0]   # the summary, not the log lines
    return line


def format_report(payload):
    """`snapshot`'s payload as a few lines of text, close to what the page shows."""
    network = payload["network"]
    where = f'trusted network "{network["name"]}"' if network["trusted"] else "not on a trusted network"
    lines = [f"m97 {payload['m97_version']} on {payload['hostname']} — {where}"]
    titles = {"gufo": "gufo", "openwebui": "Open WebUI"}
    lines += [_service_line(titles.get(key, key), service) for key, service in payload["services"].items()]

    model = payload["model"]
    if model is None:
        lines.append("model       not ready")
    else:
        context = model["context_tokens"]
        parts = [model["name"] or "?", f"{context // 1024} Ki context" if context else None,
                 f"gufo {model['gufo_version']}" if model["gufo_version"] else None]
        lines.append("model       " + ", ".join(part for part in parts if part))
        sessions = payload["sessions"]
        busy = [s for s in sessions if s["busy"]]
        speeds = " · ".join(_fmt(s["tps"], ".1f") for s in busy)
        parts = [f"{_fmt(payload['generation']['tps'], '.1f')} tok/s"]
        if busy:
            parts.append(f"sessions: {speeds} tok/s, {len(sessions) - len(busy)} idle")
        elif sessions:
            parts.append("sessions idle")
        fills = [s["fill"] for s in busy if s["fill"] is not None]
        if fills:
            parts.append(f"fullest context {100 * max(fills):.{1 if max(fills) < 0.1 else 0}f} %")
        if payload["generation"]["deferred"]:
            parts.append(f"{payload['generation']['deferred']:.0f} waiting")
        lines.append("generation  " + "; ".join(parts))

    h = payload["host"]
    threads = [pct for pct in (h.get("cpu_thread_pct") or {}).values() if pct is not None]
    lines += [f"power       {_fmt(h.get('ppt_w'), '.1f', ' W')}   CPU {_fmt(h.get('cpu_temp_c'), '.0f', ' °C')}"
              f"   GPU {_fmt(h.get('gpu_temp_c'), '.0f', ' °C')}",
              f"GPU         {_fmt(h.get('gpu_busy_pct'), '.0f', ' %')}, {_fmt(h.get('gpu_mhz'), '.0f', ' MHz')}",
              f"CPU         {_fmt(h.get('cpu_busy_pct'), '.0f', ' %')}, busiest thread "
              f"{_fmt(max(threads) if threads else None, '.0f', ' %')}, load {_fmt(h.get('load1'), '.2f')}",
              f"memory      RAM {_fmt(h.get('ram_used_gib'), '.1f')} of {_fmt(h.get('ram_total_gib'), '.0f', ' GiB')}"
              f"   GTT {_fmt(h.get('gtt_used_gib'), '.1f')} of {_fmt(h.get('gtt_total_gib'), '.0f', ' GiB')}"]

    for i, r in enumerate(payload["requests"][:5]):
        local = datetime.datetime.fromisoformat(r["time"].replace("Z", "+00:00")).astimezone()
        prefill = "cached" if r["prompt_cached"] else f"{_fmt(r['prefill_tps'], '.0f')} tok/s prefill"
        lines.append(f"{'recent' if i == 0 else '':<12}{local:%H:%M:%S}  "
                     f"{_fmt(r['prompt_tokens'], ',d')} → {_fmt(r['generated_tokens'], ',d')} tokens  "
                     f"{_fmt(r['output_tps'], '.1f')} tok/s ({_fmt(r['decode_tps'], '.1f')} decoding), {prefill}  "
                     f"finish: {r['finish'] or '–'}")
    return "\n".join(lines)
