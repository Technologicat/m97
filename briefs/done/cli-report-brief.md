# Brief: the dashboard's data from the command line

## What shipped (2026-11-20)

**Done**, in m97 0.1.1 (unreleased at the time of writing). The rest of this brief is the plan as written; the
README's "The data as JSON" describes what the endpoint serves, with a live example.

- **Built as planned:** `GET /api/status` with `?history=1`, and `m97 report` with `--url`, `--json` and
  `--history`, exiting 1 when m97 does not answer.
- **Added while building:**
  - A `hostname` key, which the text form needed.
  - **Numbers rounded to the decimals the page shows** (Juha, 2026-11-20), so that a figure an agent quotes is
    the one the person beside it sees; `fill` stays a fraction, to three decimals.
  - Shared helpers, so the page and the payload cannot drift apart: `access.Decision` carries the trusted
    network's name, `services.service_url` gives a service's address, `gufo.prompt_fully_cached` decides
    "cached".
- **Checked live:** `m97 report` on the AI server, and the curl line from the work machine.

**Goal:** the same data the dashboard shows, available outside a browser. The motivating case (Juha,
2026-11-20): a Claude Code session on the work machine, using the local AI server's LLM, should be able to
see what that server and its model are doing without opening the page.

## Decided (Juha, 2026-11-20)

- **The server exposes the data; the client stays thin.** The data lives in the running m97 service's hub, so
  a command started elsewhere has nothing to read from but the service itself.
- **A read-only JSON endpoint, `GET /api/status`**, on m97's own port (8097), built from the hub's current
  state. It is open the way the page is: viewing needs no PIN, and the endpoint can change nothing.
- **`m97 report`**: fetches the endpoint and prints a compact text summary; `--json` passes the data through
  unchanged. `--url` points it at another machine, defaulting to `http://127.0.0.1:<port from settings>`.
  A command of its own, because `m97 status` is about the m97 service itself.
- **History only on request:** `?history=1` on the endpoint, `--history` on the command. The sparkline series
  run to hundreds of points each, which most uses do not want.
- **For an agent on another machine, the endpoint alone is enough:** `curl -s http://<ai server>:8097/api/status`
  needs nothing installed there. `m97 report` is for people, or for a machine with m97 installed.

## The payload

One JSON object. Units go in the key names, as the hub's sensor sample already does (`ppt_w`, `_pct`,
`_gib`, `_s`); times are ISO 8601 in UTC; a value m97 does not have is `null`.

```json
{
  "schema": 1,
  "m97_version": "0.1.1",
  "time": "2026-11-20T09:30:00Z",
  "hostname": "ai-server",
  "network": {"trusted": true, "name": "home"},
  "services": {
    "gufo": {"status": "running", "container": "running", "url": "http://ai-server:8080/v1",
             "scope": "all", "error": null},
    "openwebui": {"status": "running", "container": "running", "url": "http://ai-server:3000",
                  "scope": "all", "error": null}
  },
  "model": {"name": "flash-next", "gufo_version": "0.34.0", "context_tokens": 262144},
  "generation": {"tps": 51.0, "deferred": 0},
  "sessions": [
    {"id": 0, "busy": true, "tps": 22.0, "prompt_tokens": 24, "generated_tokens": 180,
     "context_tokens": 262144, "fill": 0.0008}
  ],
  "host": {"ppt_w": 104.1, "cpu_temp_c": 66.0, "gpu_temp_c": 47.0, "gpu_busy_pct": 99, "gpu_mhz": 2860,
           "cpu_busy_pct": 7.0, "cpu_thread_pct": {"0": 3.0, "16": 1.0}, "cpu_cores": [[0, 16], [1, 17]],
           "load1": 1.2, "ram_used_gib": 113.3, "ram_total_gib": 123.5,
           "gtt_used_gib": 104.9, "gtt_total_gib": 116.0},
  "requests": [
    {"time": "2026-11-20T09:29:12Z", "prompt_tokens": 25, "generated_tokens": 900,
     "output_tps": 24.5, "decode_tps": 24.6, "prefill_tps": 181.0, "prompt_cached": false,
     "first_token_s": 1.63, "duration_s": 38.3, "finish": "length"}
  ]
}
```

- **`model`, `generation` and `sessions`** are `null` or empty while gufo is not ready.
- **`requests`** is newest first, as on the page. `output_tps` is the speed a request delivered, wall-clock
  from its first token; `decode_tps` is gufo's own figure, which leaves out time spent waiting while another
  session is served (see `gufo.output_tps`).
- **`schema`** is a version number for the payload's shape, so a consumer can tell a change from a bug. Bump
  it when a key changes meaning or goes away; adding a key does not need a bump.
- **With `?history=1`**, a `history` object adds the sparkline series, oldest first:
  `{"interval_s": 1.0, "seconds_ago": [...], "ppt_w": [...], "gpu_busy_pct": [...], "cpu_busy_pct": [...],
  "gen_tps": [...]}`.

## `m97 report`, the text form

Compact, one line per item, close to what the page shows. Roughly:

```text
m97 0.1.1 on ai-server — trusted network "home"
gufo        running   http://ai-server:8080/v1 (all interfaces)
Open WebUI  running   http://ai-server:3000 (all interfaces)
model       flash-next, 256 Ki context, gufo 0.34.0
generation  51.0 tok/s; sessions: 22.0 · 29.0 tok/s, 1 idle; fullest context 0.1 %
power       104.1 W   CPU 66 °C   GPU 47 °C
GPU         99 %, 2860 MHz
CPU         7 %, busiest thread 74 %, load 1.20
memory      RAM 113.3 of 123 GiB   GTT 104.9 of 116 GiB
recent      09:29:12  25 → 900 tokens  24.5 tok/s (24.6 decoding)  finish: length
            ...
```

- **Exit status:** 0 when the endpoint answered; 1 with a one-line message on stderr when it did not, so a
  script or an agent can tell "m97 is down" from "gufo is down" (the latter is a normal report).
- **`--json`** prints the payload as received, for a pipe into `jq`.

## Building it

- **The payload builder is a pure function of the hub** (`hub.snapshot(history=...)` or a function in a module
  of its own), so tests build a hub with known state and check the dict, with no server running. The route in
  `app.py` and the text formatter in `cli.py` are thin layers over it.
- **The page and the endpoint compute from the same values**: where the page derives something (a request's
  delivered speed, "cached" for a fully cached prompt, a session's context fill), the endpoint uses the same
  helper, so the two cannot drift apart.
- **Tests:** the payload with gufo ready and not ready; a missing sensor as `null`; history present only on
  request; the text formatter on a fixed payload; `m97 report` exiting 1 when nothing answers.
- **Docs:** a README section (the endpoint, the command, the curl line for another machine); the module map in
  `CLAUDE.md`.
- **Check live:** `curl` against the service on the AI server, `m97 report` there, and once from the work
  machine.
