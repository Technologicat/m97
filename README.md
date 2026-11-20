# m97

![CI status](https://img.shields.io/github/actions/workflow/status/Technologicat/m97/ci.yml?branch=main)
[![codecov](https://codecov.io/gh/Technologicat/m97/branch/main/graph/badge.svg)](https://codecov.io/gh/Technologicat/m97)
![version on PyPI](https://img.shields.io/pypi/v/m97)
![supported Python versions](https://img.shields.io/pypi/pyversions/m97)
![license](https://img.shields.io/github/license/Technologicat/m97)

A web dashboard and control panel for a [gufo](https://github.com/gufo-org/gufo) LLM server on AMD Strix
Halo: what the machine and the model are doing — power, temperatures, memory, GPU and CPU load, generation
speed, recent requests — and buttons to start and stop gufo and [Open WebUI](https://github.com/open-webui/open-webui),
for the people in the household who never open a shell.

![The m97 dashboard while gufo serves two chat requests](https://raw.githubusercontent.com/Technologicat/m97/main/docs/media/dashboard.png)

For my stance on AI contributions, see the [collaboration guidelines](https://github.com/Technologicat/substrate-independent/blob/main/collaboration.md).

## Contents

- [Requirements](#requirements)
- [Install and uninstall](#install-and-uninstall)
- [Running it](#running-it)
- [Opening the dashboard](#opening-the-dashboard)
- [The data as JSON](#the-data-as-json)
- [Configuration](#configuration)
- [Who can press the buttons](#who-can-press-the-buttons)
- [What it reads](#what-it-reads)
- [Development](#development)
- [License](#license)

## Requirements

- Linux, on an AMD APU with the `amdgpu` driver. The sensors are read from sysfs; m97 was built on a Strix
  Halo machine (Radeon 8060S, `gfx1151`).
- Rootless [podman](https://podman.io/), with gufo's host preparation done as in gufo's
  [install guide](https://github.com/gufo-org/gufo/blob/main/docs/install.md): GPU device access, the GTT
  size, unlimited locked memory. m97 runs gufo from its `gufo-runtime` container image.
- The model files, in a directory on the host (`~/models` by default).
- Python 3.11 or newer, and [pipx](https://pipx.pypa.io/) to install m97 in a virtualenv of its own.
- NetworkManager, to recognize trusted networks. Without it, m97 still runs, and treats every network as
  untrusted.

## Install and uninstall

```bash
pipx install m97
```

That puts the `m97` command on your `PATH`. For the development version instead, `pipx install
git+https://github.com/Technologicat/m97`. To update, `pipx upgrade m97`, then `m97 install` again if you
run it as a service (see below), so the service picks up the new version.

To uninstall, remove the service first if you installed one, then the software:

```bash
m97 uninstall
pipx uninstall m97
```

Your settings file, `~/.config/m97/settings.toml`, is left in place; delete it by hand if you no longer
want it.

## Running it

**As a service**, which is the usual way on a headless server. m97 then starts at boot and restarts if it
crashes:

```bash
m97 install          # install and enable the systemd user service, and start it
m97 status           # running or not, and the URL to open
m97 stop             # stop it
m97 start            # start it again
m97 logs [-f]        # its log (-f keeps following it)
m97 uninstall        # stop, disable and remove the service
```

The service serves the whole network. `m97 install --local` serves this machine only instead.

A systemd user service runs only while you are logged in, unless lingering is enabled for your user. For a
server that should start m97 at boot, before anyone logs in, enable it once:

```bash
loginctl enable-linger "$USER"
```

**Autostart.** m97 remembers which services were running, and when it starts, say at boot, it starts
them again. It waits for the network first, since a service started before the machine has joined a
trusted network would listen on this machine only. gufo comes first, and Open WebUI once gufo is up.

- A service counts as wanted running once m97 sees it running, however it was started: from the page, or
  from a shell. It counts as wanted stopped once it has been down for half a minute while m97 runs.
- A service that stops by itself is reported on the page, with its exit code, its last log lines and what
  to do. m97 does not restart it, and does not start it at boot until it has run again. The report stays
  across a restart of m97.
- The state is kept in `~/.local/state/m97/state.json`. To turn autostart off, set `autostart = false` under
  `[server]`.

**In the foreground**, for trying it out or for debugging. Ctrl+C stops it:

```bash
m97 serve            # this machine only
m97 serve --lan      # the whole network
```

## Opening the dashboard

Point a web browser at port **8097** of the machine m97 runs on:

```text
http://<server-name>:8097
```

- `<server-name>` is the machine's hostname, if your router resolves hostnames; otherwise its mDNS name,
  `<hostname>.local`, or its IP address.
- On the server itself, `http://127.0.0.1:8097` always works.
- `m97 status` prints the address, and says whether m97 listens on all interfaces or on this machine only;
  `m97 serve` prints it at startup.

The port is a setting (`port` under `[server]`, below). The page needs no login: viewing is open to anyone
who can reach it, and only the buttons are guarded (see [who can press the buttons](#who-can-press-the-buttons)).

## The data as JSON

Everything the page shows is also served as JSON, at `/api/status` on the same port, for scripts and for AI
agents working on other machines. Like the page, it is read-only and needs no PIN.

```bash
curl -s http://<server-name>:8097/api/status            # the current state
curl -s "http://<server-name>:8097/api/status?history=1"  # plus the graphs' series
```

On a machine with m97 installed, `m97 report` prints the same as text:

```bash
m97 report                                   # this machine's m97
m97 report --url http://<server-name>:8097   # another machine's
m97 report --json [--history]                # the JSON, unformatted
```

`m97 report` exits with status 1 when m97 does not answer; a stopped service is a normal report. The keys:

- **`schema`**: the payload's version. It goes up when a key changes meaning or goes away, not when one is
  added.
- **`m97_version`**, **`time`** (ISO 8601, UTC), **`hostname`**, **`network`** (`trusted`, and the trusted
  network's `name`).
- **`services`**: for `gufo` and `openwebui`, the `status` the page shows, the `container` state, the `url`, the
  `scope` it listens on (`all`, `local`, or `null` when not listening), and any `error`.
- **`model`** (`name`, `gufo_version`, `context_tokens`), **`generation`** (total `tps`, requests `deferred`)
  and **`sessions`** (per session: `busy`, live `tps`, `prompt_tokens`, `generated_tokens`, `context_tokens`,
  and the context `fill` from 0 to 1). `null` or empty while gufo is not ready.
- **`host`**: the sensors, with units in the key names (`ppt_w`, `cpu_temp_c`, `gpu_busy_pct`, `ram_used_gib`
  …), per-thread CPU load in `cpu_thread_pct`, and `cpu_cores`, the threads grouped by physical core.
- **`requests`**: the recent finished requests, newest first, with `output_tps` (the speed delivered,
  wall-clock from the first token) beside gufo's own `decode_tps`.
- **`history`**, only when asked for: `interval_s`, `seconds_ago`, and the series `ppt_w`, `gpu_busy_pct`,
  `cpu_busy_pct` and `gen_tps`, oldest first.

A value m97 does not have is `null`.

## Configuration

m97 reads its settings from **`~/.config/m97/settings.toml`**. The file is optional: without it, m97 runs
on its defaults, which are in [`m97/settings.toml`](https://github.com/Technologicat/m97/blob/main/m97/settings.toml), each with a comment saying what it
does.

Your file overrides the defaults key by key, so it only needs the keys you want to change. A table in your
file merges into the default table of the same name; any other value, a list included, replaces the default
whole. A key m97 does not know is reported in its log as a probable typo and ignored. A value of the wrong
type stops m97 at startup with a message naming the key.

A typical file:

```toml
[access]
pin = "2468"                       # a string, so that leading zeros survive

[[access.trusted_networks]]
name = "home"
connections = ["…UUID of the home Wi-Fi profile…"]
subnet = "192.168.1.0/24"          # optional

[[access.trusted_networks]]
name = "cabin"
connections = ["…2.4 GHz profile…", "…5 GHz profile…"]

[gufo]
image = "ghcr.io/gufo-org/toolboxes/gufo-runtime:0.34.0"   # pin a gufo version
sessions = 3
context = 262144
```

The settings, by table:

- **`[server]`**: `port` (8097), and `autostart` (on; see [running it](#running-it)).
- **`[access]`**: the `pin`, and the `trusted_networks`; see [who can press the buttons](#who-can-press-the-buttons).
- **`[gufo]`**: how m97 runs gufo. `image`; `models_dir` (mounted read-only at `/models` in the container);
  `model` and `mtp_model`, as paths relative to `models_dir` (an empty `mtp_model` turns speculative
  decoding off); `served_name`, the model ID clients ask for; `sessions` and `context` (0 means gufo's own
  default); `port`; `extra_args`, a list appended to the `gufo serve ... llm` command line; `container`, the
  container's name; `start_timeout` in seconds. The defaults serve Qwen3.8 Flash-Next as in gufo's
  quickstart.
- **`[openwebui]`**: `image`, `volume` (the podman volume that holds accounts and chats), `port`,
  `container`, `start_timeout`. Open WebUI is pointed at gufo's port.
- **`[history]`**: `seconds` the graphs reach back (600), and the sampling `interval` (1.0).

m97 recognizes a service by its container name, so a container started some other way, by hand or by a
script, shows up on the page as long as the name matches.

## Who can press the buttons

Viewing is always open. The start and stop buttons are open **while the server is on a trusted network**,
and need the **PIN** on any other network. With no PIN set, off the trusted networks they stay locked, and
the page says what to configure.

A trusted network is recognized by the UUID of the NetworkManager connection the server is using. List the
saved connections and their UUIDs with:

```bash
nmcli -t -f NAME,UUID connection show
```

A network with several saved profiles, say one per Wi-Fi band, lists all of them under `connections`. The
optional `subnet` adds a second condition: the server's address must also be inside it. A network that
has never been joined has no saved profile, and so no UUID yet; join it once, then add it.

NetworkManager offers a saved Wi-Fi profile only to an access point with the same name *and* compatible
security, so an open network that borrows a trusted network's name does not count as trusted. Anyone who
knows the Wi-Fi password could still set up a look-alike.

Two more things follow from the network:

- **Services started from the page listen on all interfaces only on a trusted network.** Elsewhere they
  listen on this machine only, whoever pressed the button.
- **The PIN travels unencrypted**, since m97 serves plain HTTP. On an untrusted network, someone watching the
  traffic could read it. Wrong guesses are limited to five per five minutes for each client address.

## What it reads

Everything without root:

- **Host sensors** from sysfs and procfs: socket power, CPU (Tctl) and GPU edge temperatures, GPU load and
  clock, CPU load, RAM, and GTT, the system memory the GPU has mapped. The sensors are found by driver
  name, since hwmon indices change between boots.
- **gufo's HTTP endpoints**, only while its container runs: `/ready`, `/props`, `/slots` (which sessions are
  busy, and their live speed), `/metrics` (the total generation throughput).
- **gufo's log**, for the recent requests: each finished chat request's token counts, speeds and timings.
- **Container state** from `podman events`, and **the network** from `nmcli monitor`, so m97 starts no
  processes periodically. It takes about 0.5 % of one CPU core, plus about 0.1 % per open page (measured
  on the server above).

## Development

```bash
git clone https://github.com/Technologicat/m97
cd m97
pdm install          # creates .venv, installs m97 editable with the dev tools
pdm run pytest tests/
```

The design, and the decisions behind it, are in [`briefs/done/dashboard.md`](https://github.com/Technologicat/m97/blob/main/briefs/done/dashboard.md).

## License

BSD 2-Clause, see [`LICENSE.md`](https://github.com/Technologicat/m97/blob/main/LICENSE.md).
