# Backlog

Parked topics. Each gets its own session; none block v1.

## Home self-hosting on a Raspberry Pi

Parked 2026-08-10. Cloud (Oracle Always Free) was chosen for v1 — see §10.2 of the
design spec. This is the likely successor once there's hardware.

The scope is bigger than this bot: the goal is **one box running several Docker
projects**, not a single-purpose appliance. That changes the hardware answer, so
resolve it as its own discussion rather than folding it into a finappa decision.

To work through:

- **Which Pi.** Multi-project changes the calculus — a Pi Zero 2 W (512 MB) is
  ample for this bot alone and too small for a stack. RAM is the binding
  constraint, not CPU. Also worth pricing against a used mini-PC (Intel N100 and
  similar), which is often better value per euro than a Pi 5 once storage, PSU, and
  case are counted, and runs x86 images without ARM caveats.
- **Storage.** SD cards die under sustained writes; SQLite in WAL mode writes
  constantly. SSD over USB, or a Pi with NVMe.
- **Running several projects together.** One Docker Compose stack vs. separate
  stacks; whether anything needs to be reachable from outside the LAN, and if so
  Tailscale/WireGuard rather than port forwarding.
- **Migrating finappa.** Should be `docker compose up` after copying `data/` and
  `.env`. ARM images need verifying but the base image is multi-arch.
- **What home hosting changes for this bot.** Power and internet outages become
  the failure mode. The daily idempotent recurring check already absorbs them.
  Backups keep working — they only need outbound network.
- **Whether backups should still go to Telegram** once the data lives at home, or
  to a second local disk as well.

## Deferred v1 features

From the design spec's non-goals — revisit only after a month of real use:

- **Static read-only stats website.** Requested 2026-08-11, after v1 went live.
  A simple static page (no editing, no auth flow beyond maybe basic access
  control) showing an overview of the current financial situation — charts/
  diagrams, not raw tables. Natural shape: a small script reads straight from
  `data/finappa.db` (read-only, WAL mode allows concurrent readers safely) and
  regenerates static HTML/charts, e.g. on a cron alongside the existing daily
  jobs, or on demand via a tiny script run over SSH. Do not add a live backend
  API or a database write path for this — it only needs to read. Revisit once
  there's a month of real transaction history to decide what's actually worth
  plotting (this was already the plan's original reasoning for deferring it).
- Voice-note input via transcription.
- Receipt photos via vision.
- Backdating ("yesterday I spent 30 at Rewe" currently logs as today).
- Editing a transaction's amount in place, rather than `/undo` and re-send.
