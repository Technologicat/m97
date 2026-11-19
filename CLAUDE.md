# CLAUDE.md

Guidance for Claude Code working in this repository. Fleet-wide conventions live in the global
`~/.claude/CLAUDE.md`; this file holds only what is specific to m97.

## What m97 is

A web dashboard and control panel for a gufo LLM server on AMD Strix Halo: what the machine and the model
are doing, and buttons to start and stop gufo and Open WebUI, for people who never open a shell. Built for
the fleet's local AI server, and public because other gufo users may want it. The README is the user's
manual; [`briefs/dashboard.md`](briefs/dashboard.md) records the design and the decisions behind it.

The package, `m97/`:

- `cli.py`: the `m97` verbs (`serve`, `install`, `uninstall`, `start`, `stop`, `status`, `logs`); the
  systemd user unit is written by `install`.
- `app.py`: the NiceGUI page. It only renders, from the hub, on a per-page timer.
- `hub.py`: the one shared view of the machine: the sampling loop, gufo's HTTP endpoints, gufo's log,
  podman events, `nmcli monitor`, history, actions, autostart and crash reports.
- `services.py`: the podman command lines, start and stop, the event follower, where a service listens.
- `gufo.py`: parsers for gufo's metrics, `/slots` and log lines, plus the derived speeds.
- `sensors.py`: the sysfs and procfs sampler, and the CPU topology.
- `access.py`: the trusted-network and PIN rule. `intent.py`: which services should run, kept on disk.
- `settings.py` with `settings.toml`: the shipped defaults and the per-user override rule.

## Development

PDM, Python 3.14 in the project venv; m97 is an app, so `pdm.lock` is committed. Run what CI runs:

```bash
pdm run pytest tests/
pdm run ruff check . --ignore SIM103
pdm run python -m pycodestyle --select=E121,E122,E123,E124,E125,E128,E129,E131 --exclude=.git,__pycache__,build,dist,.venv,00_stuff,lint_canary_fixture.py .
pdm run python scripts/check_lint_canary.py
flake8 --config ~/.config/flake8 m97 tests
```

CI is Linux and CPython only (3.11–3.15): m97 reads amdgpu sysfs, runs podman and asks NetworkManager, so
macOS, Windows and PyPy have nothing to test. No CI step reads Markdown, so a docs-only push needs no
watch.

## The live server

On the AI server, m97 runs as the systemd user service `m97`, from this checkout's `.venv`. **A code
change reaches it only after `systemctl --user restart m97`** (or `m97 stop` and `m97 start`).

- Restarting m97 leaves gufo and Open WebUI running: m97 starts each container in a scope of its own
  (`services._in_own_scope`). It does clear the page's history, so let it collect a minute or two before
  judging a sparkline or taking a screenshot.
- gufo and Open WebUI serve the household and the other machines. Restarting gufo means reloading a
  ~104 GiB model, about a minute with the files in page cache. Ask before restarting either of them.
- This machine's settings are in `~/.config/m97/settings.toml` (trusted networks, PIN), and the remembered
  service state in `~/.local/state/m97/state.json`. Both are private; neither goes in the repo.

## Checking the page

Use the Playwright MCP server: open `http://127.0.0.1:8097/`, snapshot or screenshot, press the buttons.
Its files land in `.playwright-mcp/`, which is gitignored. To exercise the panels, send a few concurrent
chat requests to `http://127.0.0.1:8080/v1/chat/completions` (model `flash-next`, `reasoning_effort` set
to `none` for short answers).

**The README screenshot** (`docs/media/dashboard.png`) is taken with two chats running, after the page has
collected some history, at a 1280 px wide viewport with a full-page capture. The page shows the server's
hostname in its title and in the service addresses, so before capturing, replace it in the page's text
nodes with `ai-server`, and check that `document.body.innerText` no longer contains it.

## Pitfalls found the hard way

- **Run commands through `services.run`, which waits for the process, not for end-of-file.** Under uvloop,
  which NiceGUI runs on, `podman run -d` leaves conmon holding the command's stdout, and reading a pipe to
  its end waited for the container to exit: every start took 60 s and then reported an error.
- **A probe container whose main process is `sleep` cannot show a SIGTERM.** PID 1 in a container ignores
  SIGTERM unless it installs a handler, so a test of "does X stop the container" needs a probe that traps
  it, such as `sh -c 'trap "exit 0" TERM; sleep 300 & wait'`.
- **A deliberate stop removes the container; a crash leaves it behind, exited.** That difference is how
  `hub._check_crash` tells the two apart, so keep `--rm` off the containers m97 runs.
- **Quasar sets its colour variables as inline styles on `<body>`.** Overriding one from CSS needs
  `!important`; an ordinary rule loses, whatever its specificity.
- **gufo's speed gauges are not averages.** `predicted_tokens_seconds` and `prompt_tokens_seconds` hold the
  last finished request's figures, and the log's `decode_tps` leaves out time a request waits while another
  session is served. The live total comes from the change in `tokens_predicted_total`.
- **The page must not start processes on a timer.** Container state comes from `podman events` and the
  network from `nmcli monitor`; an `nmcli` call per refresh per tab was the one regression of this kind so
  far. Measure with `/proc/<pid>/stat` over 30 s before and after anything that might add load: about
  0.5 % of one core for m97, plus about 0.1 % per open page, as of 2026-11-18.
