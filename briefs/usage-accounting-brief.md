# Brief: how the AI server's time is spent, per day

**Goal:** after a few weeks, know how the local AI server's wall time actually divides between serving LLMs,
sitting loaded and idle, and other uses such as gaming, without anyone having to remember. Raised by Juha on
2026-11-20, from a suggestion of claude.ai's: he does not yet know how the machine's time will divide in
practice. Planned for m97 **0.3.0**.

## Decided (Juha, 2026-11-20)

- **m97 counts the time itself, as it samples.** Not reconstructed from gufo's request log: that log goes with
  the container, which m97 removes on every stop. m97 already looks at the machine once a second (the hub's
  sampling loop), so each sample adds its interval to a per-day tally.
- **Categories, by what gufo is doing**, one per sample, exhaustive among the time m97 sees:
  - **serving:** gufo ready, and at least one session busy (generating or prefilling);
  - **loaded, idle:** gufo ready, no session busy;
  - **loading:** gufo's container running but not ready yet (including m97's own `unknown` second);
  - **stopped:** gufo's container not running.
- **"m97 off"** is the rest of the day: the time m97 did not see, whether the machine was off, asleep, or m97
  itself was down. Labelled honestly as that, with no attempt to tell those apart.
- **A separate counter for the gaming half of the question: GPU busy while gufo is not serving**, GPU load above
  30 % in a sample whose category is not serving. m97 cannot see a game as such; this catches gaming and any
  other GPU work, whether gufo is stopped or loaded and idle. It overlaps the categories rather than being one.
- **Energy per day, in Wh, per category** and for the GPU counter: the sampled socket power (`ppt_w`) times the
  interval, summed. Measured for scale (setup notes): about 5 W idle with nothing loaded, about 19 W with gufo
  loaded and idle, 60–100 W serving.
- **On the page: a "Usage" card, full width, under the recent requests.** A stacked bar per day for the last 14
  days, the four categories plus m97 off; hover gives the hours and the Wh. The GPU counter shown beside it.
- **For scripts and agents:** `GET /api/usage?days=N` (default 14), and a usage line in `m97 report`
  (today and the last 7 days, in hours).

## Building it

- **Day boundaries in local time**, at midnight; a sample is counted on the day it was taken. Days are keyed by
  ISO date (`2026-11-20`). A day m97 never saw has no entry, and reads as all m97 off.
- **Storage:** one JSON file, `$XDG_STATE_HOME/m97/usage.json` (`~/.local/state/m97/`, beside `state.json`):
  `{"YYYY-MM-DD": {"serving_s": …, "idle_s": …, "loading_s": …, "stopped_s": …, "gpu_other_s": …,
  "serving_wh": …, "idle_wh": …, "loading_wh": …, "stopped_wh": …, "gpu_other_wh": …}}`. Written atomically
  (as `intent.save_state` does) once a minute and on shutdown, so a crash loses at most a minute; read at start,
  so a restart of m97 continues the day. A few hundred bytes per day, kept indefinitely.
  - A missing or unreadable file starts a fresh tally, with a warning in the log; it must never stop m97.
- **The category comes from state the hub already has:** `container["gufo"]`, `ready["gufo"]`,
  `ready_known["gufo"]`, and the parsed slots (`busy`). The accounting is a pure function of those plus the
  sample, so it is testable without a server: `(tally, category inputs, ppt_w, gpu_busy_pct, interval, date)
  -> tally`.
- **The interval is the real elapsed time** between samples, not the nominal one, capped (say at 10 s): a
  stalled loop or a suspend must not credit minutes to whatever category was last seen.
- **m97 off for a day** is `86400 s` minus the seconds counted, computed when shown, not stored. DST days are 23
  or 25 hours long; take the day's real length from the local calendar.
- **Payload of `/api/usage`:** `{"schema": 1, "days": [{"date": …, "serving_h": …, "idle_h": …, "loading_h":
  …, "stopped_h": …, "m97_off_h": …, "gpu_other_h": …, "serving_wh": …, …}, …]}`, oldest first, rounded as the
  page shows them (the decimals rule in `report.py`).
- **Tests:** the category for each combination of gufo states; the GPU counter only outside serving and only
  above the threshold; energy summed; the interval cap; a sample at 23:59:59 and one at 00:00:01 landing on two
  days; the file round trip, and a corrupt file starting fresh; m97 off computed for a normal and a DST day.
- **Docs:** README (the card, the endpoint, the report line, what m97 off means); CLAUDE.md module map; changelog
  under 0.3.0.
- **Check live:** let it count for a while, then the card, the endpoint and `m97 report`; restart m97 and see
  the day continue.
