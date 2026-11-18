# Authors

*By fleet policy, both human and AI authors are listed.*

Juha Jeronen (@Technologicat):

- Author and maintainer: original concept, and the design decisions — a custom page rather than an
  off-the-shelf monitoring stack, start/stop controls for people who never use a shell, and the access rule
  (open on trusted networks, a PIN elsewhere).
- Direction, review of all AI-authored changesets, and live verification on the hardware.

Claude Opus 5.5 (Anthropic), as AI pair programmer:

- The design brief, including the survey of what gufo and the host expose for monitoring.
- The implementation: settings, parsers, sensors, container control, the access rule, the page, the
  command-line verbs and the systemd service, and their tests.
