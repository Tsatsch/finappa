# finappa

A single-user Telegram bot that turns free-text messages like
"30€ in Rewe and 15€ for fuel" into structured ledger entries, tracks recurring
transactions (rent, salary, ETF contributions), and answers `/summary`.

Full design rationale: [`docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md`](docs/superpowers/specs/2026-08-10-telegram-finance-tracker-design.md).
Architecture notes for AI coding tools: [`AGENTS.md`](AGENTS.md).

## Requirements

- A Telegram bot token (from [@BotFather](https://t.me/BotFather)).
- An OpenAI API key.
- Any always-on host that can run Docker and reach the internet outbound — no
  domain, no open ports, no static IP required (spec §10.1). ~150 MB RAM is
  enough.

## One-time setup

1. Create the bot with `@BotFather`; copy the token.
2. Get your numeric Telegram user ID from `@userinfobot`.
3. Create a **private** Telegram channel for backups, add the bot as an admin
   with post rights, and get the channel's ID (forward any message from the
   channel to `@userinfobot`; it starts with `-100`).
4. On the host:
   ```bash
   git clone <this-repo-url> finappa
   cd finappa
   cp .env.example .env
   ```
5. Fill in `.env`: `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_ID`,
   `OPENAI_API_KEY`, `BACKUP_CHANNEL_ID`. Generate `BACKUP_ENCRYPTION_KEY`:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   **Store that key in a password manager.** Losing it makes every remote backup
   permanently unreadable (spec §13.2) — local snapshots on disk are unaffected.
   Also set `STATS_USERNAME` and `STATS_PASSWORD` for the read-only stats
   one-pager (a strong unique password — this endpoint is public via nip.io).
6. Edit `recurring.yml` for your actual rent/salary/etc.
7. Create the data directory with the right owner **before** first start — the
   container runs as uid 1000, and a bind mount created by Docker itself would
   default to root ownership:
   ```bash
   mkdir -p data
   sudo chown 1000:1000 data
   chmod 700 data
   ```
8. Start it:
   ```bash
   docker compose up -d --build
   docker compose logs -f --tail=50
   ```
9. Send `/help` to the bot in Telegram to confirm it's alive.
10. Open the stats page. On a host that already runs barcheck with Caddy,
    prefer HTTPS — add the block in [`deploy/caddy-finappa.caddy`](deploy/caddy-finappa.caddy)
    to your barcheck Caddyfile, point `finappa.<your-domain>` at the server IP,
    set `STATS_CADDY_NETWORK` in `.env` (default `barcheck_default`), then:
    ```bash
    docker compose up -d --build
    ```
    Browse `https://finappa.<your-domain>/` and sign in with Basic Auth.
    Caddy terminates TLS; finappa stats stays on the shared Docker network only
    (`127.0.0.1:${STATS_PORT}` on the host is for local curl debugging).
## Host hardening

The bot itself has no listening ports and no public identity, so the VPS's SSH
is the actual attack surface (spec §13.3). Do this once, on any fresh host:

**Deploying onto a host that already runs something else?** Skip the `ufw`
block below and check its current rules first (`sudo ufw status`) — running
`ufw enable` with a blind `default deny incoming` will cut off whatever port
your other app needs unless you explicitly `ufw allow` it first. finappa
itself needs no inbound rule at all (no `ports:` key, ever), so it never
requires opening anything new.

```bash
# Key-only SSH, no root login
sudo sed -i \
  -e 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
  -e 's/^#*PermitRootLogin.*/PermitRootLogin no/' \
  /etc/ssh/sshd_config
sudo systemctl restart ssh

# Deny all inbound except SSH
sudo ufw default deny incoming
sudo ufw allow 22
sudo ufw enable

# Unattended security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Confirm your SSH key works in a **second** terminal before closing the first —
locking yourself out of a fresh cloud box means a rescue-console trip.

## Deploying a change

```bash
ssh <host>
cd finappa
git pull
docker compose up -d --build
```

See [`.claude/skills/deploy/SKILL.md`](.claude/skills/deploy/SKILL.md) for the
full checklist including how to tell a bad deploy from a good one.

## Restoring from a backup

See [`.claude/skills/restore-backup/SKILL.md`](.claude/skills/restore-backup/SKILL.md).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in test/dummy values
pytest -v
```

Tests never hit the network — the OpenAI client is stubbed everywhere, and
SQLite runs against a `tmp_path` file (WAL mode needs a real file, not
`:memory:`).
