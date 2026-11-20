# Changelog

All notable user-visible changes to **m97** are documented here.

This project adheres to [semantic versioning](https://semver.org/). Dates are ISO 8601 (YYYY-MM-DD).

---

## 0.2.1 (in progress)

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
