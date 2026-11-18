# Brief: m97, a web dashboard and control panel for a gufo server

[gufo](https://github.com/gufo-org/gufo) is an inference engine for AMD Strix Halo (Ryzen AI Max+, Radeon
8060S, gfx1151). The maintainer runs it on a Strix Halo mini PC, the fleet's local AI server, which will
run mostly headless and serve LLMs to the other machines on the LAN. The desktop's system-monitor applet
is therefore no help, and the command-line wrappers (`gufo-serve`, `open-webui-serve`) suit the maintainer
and AI agents but not the rest of the household.

**m97 is a web page for both audiences**: at a glance, what the machine and its LLM server are doing; and
friendly buttons, with visual status, to start and stop the services. The inspiration for the view is
strata's terminal monitor (<https://github.com/Niko1221/Strata>, `docs/media/terminal-monitor.png` in its
repo): model activity, speed, memory and recent requests.

It is a public fleet project because it may be useful to other gufo users on Strix Halo. So nothing
machine-specific goes into the repo: hostnames, addresses, network names and the PIN live in a local config
file (see "Access" below). In docs, name machines by role.

**Before starting, read the AI server's local setup record**: `STATUS.md` in its setup repo,
`~/Documents/<hostname>` (local git, no remote; the hostname is in `~/.claude/SECRET-SAUCE.md`). It has the
gufo setup and the measurements that say what normal looks like: idle ~5 W, sustained inference ~64 W in
BIOS Quiet, three gufo sessions of 256 Ki, ~104 GiB of GTT in use while gufo is loaded.

## Decided

- **A custom small page**, not Prometheus plus Grafana (Juha, 2026-11-18): one small service, exactly the
  panels wanted, no extra daemons to run and update.
- **Start/stop buttons** for gufo and Open WebUI, with visual status, so that people who never use a shell
  can run the services (Juha, 2026-11-18).
- **Access** (Juha, 2026-11-18): open, with no login, while the machine is on one of a configured list of
  trusted networks; on any other network, the buttons need a configured shared PIN. Read-only viewing
  stays open. The trusted networks and the PIN go in a local config file under `~/.config/m97/`, never in
  the repo. How to recognize a network (Wi-Fi SSID, NetworkManager connection name, subnet) is the
  session's call; the machine's three trusted networks differ in all three.
- **Python** (Juha's preference). The recommendation is **NiceGUI** (FastAPI underneath): it pushes updates
  to the browser over a websocket (`ui.timer`), has Plotly and ECharts graphs, buttons and badges built in,
  and runs a slow command without blocking the page (`run.io_bound`). The alternative considered was Plotly
  Dash: excellent for plots, polling-based (`dcc.Interval`), and clumsier for long actions, which want its
  background callbacks and a job queue. **Start with a ten-line prototype of the hardest part**, a button
  running `gufo-serve start` (about a minute) while the status updates live, before committing to either.
- **Fleet setup per chandra** (`~/Documents/koodit/chandra`, the reference pure-Python project): PDM, the
  house ruff and flake8 config, CI with SHA-pinned actions and least-privilege permissions. Use the
  `project-setup` and `ci-setup` skills.

## Panels

- **RAM** use, and GTT (RAM the GPU has mapped: ~104 GiB while gufo is loaded).
- **CPU and GPU usage.**
- **Model status**: up and ready or not, which model, how many sessions busy.
- **Generation speed.**
- **Recent requests.**
- **Power and temperatures**, from what `powerlog` reads.
- **Service controls**: start and stop for gufo and Open WebUI, showing starting, running, stopping and
  stopped, and the error if a start fails.

A few minutes of history (sparklines or small graphs) for power, speed and GPU load make the page useful
during a run, not only as a snapshot.

## Data sources (all checked on 2026-11-18, gufo 0.34.0)

All readable without root.

- **Host sensors: reuse `powerlog`'s sampler.** `~/.claude/scripts/powerlog.py` (the public dotclaude repo)
  has `make_sampler()`, returning a function that gives one reading as a dict: `ppt_w`, `cpu_temp_c`,
  `gpu_temp_c`, `gpu_busy_pct`, `gpu_mhz`, `cpu_mhz_mean`, `cpu_mhz_max`, `load1`, `gtt_used_gib`. It finds
  the sensors by driver name (hwmon indices are not stable across boots). Vendoring or depending on it is
  the session's call; m97 should not need dotclaude installed to run. Its CPU clock is `scaling_cur_freq`,
  which is not CPU utilization: for utilization, read `/proc/stat` deltas. RAM: `/proc/meminfo`
  (`MemTotal`, `MemAvailable`).
- **gufo's HTTP endpoints**, on `127.0.0.1:8080` by default:
  - `GET /health`, `GET /ready`: liveness and readiness.
  - `GET /v1/models`: model id, `context_length`, `input_modalities`.
  - `GET /props`: `model_alias`, `total_slots` (sessions), `default_generation_settings.n_ctx`,
    `build_info` (gufo version).
  - `GET /slots`: one entry per session with `is_processing`, `n_ctx`, prompt-token counts and
    `next_token[0].n_decoded` (tokens generated so far): the live "who is busy" view.
  - `GET /metrics`: Prometheus text. Counters: `llamacpp:prompt_tokens_total`,
    `llamacpp:prompt_seconds_total`, `llamacpp:tokens_predicted_total`,
    `llamacpp:tokens_predicted_seconds_total`, `llamacpp:spec_decode_num_draft_tokens_total`,
    `llamacpp:spec_decode_num_accepted_tokens_total`, `gufo_device_lost_total`. Gauges:
    `llamacpp:prompt_tokens_seconds`, `llamacpp:predicted_tokens_seconds`, `llamacpp:requests_processing`,
    `llamacpp:requests_deferred`, `llamacpp:kv_cache_usage_ratio`. Rates over a window come from counter
    deltas; check what the two `_seconds` gauges average over before plotting them.
- **Recent requests: gufo's log**, `podman logs [-f] gufo`. Each finished request has a line with
  `[http] ... event=completed`; chat requests carry `path`, `status`, `duration_ms`, `prompt_tokens`,
  `generated_tokens`, `finish`, `ttft_ms`, `prefill_tps`, `decode_tps`, `batch_width`, `plan`,
  `draft_accepted`, `draft_proposed`, `cache`, `cached_tokens` and `queue_ms`, as space-separated
  key-value pairs after a timestamp. The format is gufo's and can change between versions, so parse
  defensively.
- **Service state and control**: the wrappers in dotclaude's `scripts/`, `gufo-serve` and
  `open-webui-serve`, with `start [--lan]`, `stop`, `status` and `logs`. They handle the awkward cases: a
  container stuck in a non-running state, Ctrl+C during startup, and timing the startup wait on the
  monotonic clock. Call them rather than re-implementing podman handling, or factor their logic out if m97
  should run without dotclaude. The container state is
  `podman container inspect --format '{{.State.Status}}' <name>`.

## Constraints

- **Headless and on the LAN**: listen on `127.0.0.1` by default and on all interfaces with `--lan`, like the
  wrappers. Pick a port that collides with nothing: gufo has 8080, Open WebUI 3000.
- **Light.** It runs beside a 104 GiB model on a machine that otherwise idles at ~5 W. Updating every second
  or two is plenty; it must not keep a CPU core busy or wake the GPU.
- **Survives gufo being down or restarting**: show it, and recover by itself.
- **Starts at boot without a login**: a systemd user service, with lingering enabled (it is, on the AI
  server). A wrapper with the same verbs as `gufo-serve` would match the existing tools.

## Decided in the first session (Juha, 2026-11-18)

- **NiceGUI.** The prototype (a start/stop button for gufo, status updating live through a ~35 s start)
  worked on NiceGUI 4.0.0 under Python 3.14, at about 0.2 % of one core with a tab open and gufo up.
- **Trusted network = the active NetworkManager connection's UUID**, with an optional subnet check on top.
  NM activates a saved Wi-Fi profile only when the SSID matches and the WPA handshake succeeds, so a network
  borrowing the name does not match. A bare SSID can be borrowed, and a subnet says little: the travel
  network's 192.168.1.0/24 is a common router default. Checked at each button press, so a network change
  while m97 runs is picked up. Gap: a wired profile matches any cable; when wired networking is used, the
  gateway's MAC address is the identifier to add.
- **Config: TOML, with overrides.** Defaults ship in the package as `settings.toml`; the user's
  `~/.config/m97/settings.toml` overrides them key by key (tables merge, other values replace whole, arrays
  included). A key with no default is reported as a probable typo. The PIN and the trusted networks live
  only in the user's file.
- **No PIN configured** → off the trusted networks the buttons stay locked, with a message beside them (or a
  tooltip) saying why and what to configure to enable them.
- **gufo is started with `--lan` only on a trusted network**; elsewhere it stays on `127.0.0.1`, whoever
  presses the button.
- **m97 runs the containers itself**, rather than calling dotclaude's wrappers: those hardcode one machine's
  model, and other gufo users most likely have a bare `podman run` line rather than scripts. Everything
  machine-specific comes from the config: for gufo the image, models directory, model, MTP model, served
  name, sessions, context, port and extra arguments; for Open WebUI the image, volume, port and gufo URL.
  The awkward cases the wrappers handle carry over (a leftover container in another state, a container
  dying during load, the startup wait on the monotonic clock).
- **The sensor sampler is vendored** from `powerlog.py`, returning `None` for a missing sensor instead of
  exiting.
- **Container state from `podman events`**, not polling: one `inspect` at startup, then follow the event
  stream (journald-backed here); if the stream process exits, restart it and inspect again, since events in
  the gap are lost. gufo's readiness, slots and metrics come over loopback HTTP, only while its container
  runs. So m97 spawns no periodic processes.
- **Shared state**: one sampling loop in the server; each open tab only renders from it. Service state is
  derived from observation, so a start from the CLI shows too and every viewer sees the same "starting";
  only a failed start's error is m97's own memory.
- **History**: an in-memory ring buffer of about ten minutes, not kept across a restart.
- **PIN over plain HTTP**: accepted for v1, with wrong guesses rate-limited; the README says so.
- **Port 8097.**
- **`pdm.lock` committed** (an app).

## Testing

Unit-test the parsers (metrics text, the log line, `/slots`) against captured samples, including one with a
field missing, and the access rule (trusted network, wrong PIN, right PIN). Check the page in a real browser:
the Playwright MCP server is configured for Claude Code (user scope), so a session can open the page, take
screenshots and see the panels update. Test with gufo idle, busy (a few concurrent chat requests to
`127.0.0.1:8080`) and stopped, and press the buttons.
