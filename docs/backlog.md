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

None currently.

## Shipped after v1

- **Reliability & ledger UX** (2026-08-25) — backup staleness alerts +
  `/backup_status`, startup missed-recurring DMs, `/summary [YYYY-MM]`,
  `/find`. Spec:
  `docs/superpowers/specs/2026-08-25-reliability-and-ledger-ux-design.md`.
- **Read-only stats website** (2026-08-11) — sibling `stats` compose service,
  HTTPS via barcheck Caddy (or nip.io fallback), live SQLite reads, Chart.js
  one-pager. Spec:
  `docs/superpowers/specs/2026-08-11-stats-website-design.md`.
- **Refund type removed** (2026-08-12) — ledger is EXPENSE/INCOME only.
- **Transaction management & backdating** (2026-08-12) — `/recent`, `/delete`,
  `/edit`, message backdating. Spec:
  `docs/superpowers/specs/2026-08-12-transaction-management-and-backdating-design.md`.

Dropped permanently (not wanted):

- Voice-note input via transcription.
- Receipt photos via vision.
