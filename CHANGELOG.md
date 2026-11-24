# Changelog

All notable user-visible changes to **m97** are documented here.

This project adheres to [semantic versioning](https://semver.org/). Dates are ISO 8601 (YYYY-MM-DD).

---

## 0.3.0 (in progress)

### Added

- **How the machine's time is spent, per day.** A Usage card at the bottom of the page shows the last 14 days as stacked bars of hours: serving, loaded and idle, loading, stopped, and "m97 off" for the time m97 did not see.
  - Beside it, other GPU use: the GPU busy while gufo is not serving, whatever is using it.
  - Hovering over a day gives each category's energy in Wh as well.
  - For scripts and agents, `/api/usage?days=N`, and a `usage` key in `/api/status`; `m97 report` prints a line for today and one for the last 7 days.
  - m97 counts it as it samples, and keeps the tally in `~/.local/state/m97/usage.json`.
- **Host folders mounted read-only into Open WebUI**, for its plugins to read: `mounts` in `[openwebui]`, as `"host:container"` strings.
  - A host path that does not exist is skipped, with a warning in the log, rather than keeping Open WebUI from starting.

### Fixed

- **For about a second after m97 started, a service that had been running for hours read as "starting".**
  - m97 had not yet asked the service whether it was ready. Until it has, the status is now `unknown`, on the page, in `/api/status` and in `m97 report`.

## 0.2.0 — Owl post — 2026-11-20

### Added

- **The dashboard's data as JSON, at `/api/status`**, for scripts and for AI agents on other machines: services, model, sessions, sensors and recent requests, with the graphs' series on `?history=1`. Read-only, and open like the page.
  - `m97 report` prints the same as text, from this machine's m97 or another's (`--url`); `--json` gives the data unformatted.
  - Numbers carry the decimals the page shows them with, so a figure quoted from the data is the one a person sees on the page.

## 0.1.0 — Night watch — 2026-11-19

Initial release.
