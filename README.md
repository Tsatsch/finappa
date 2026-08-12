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
6. Edit `recurring.yml` for your actual rent/salary/etc. See [Categories](#categories)
   for valid `category` names.
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
10. Open the stats page:
    ```text
    http://<host-public-ip>.nip.io:<STATS_PORT>/
    ```
    (default port `8080`). Sign in with `STATS_USERNAME` / `STATS_PASSWORD` from
    `.env`. Traffic is plain HTTP — use a strong unique password. Docker
    publishes this port past `ufw`; Basic Auth is the access control.

    **Optional HTTPS:** only if you run a reverse proxy on the host (e.g. Caddy)
    and accept wiring it to the stats container — see
    [`deploy/caddy-finappa.caddy`](deploy/caddy-finappa.caddy). finappa does not
    depend on any other app; HTTP on a high port is the default, isolated setup.
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

## Categories

Single source of truth: `app/categories.py::CategoryName`. The same names work
everywhere — free-text logging, `/recat`, `/edit`, `recurring.yml`, and the
stats site filters.

| Category | Typical use |
|----------|-------------|
| Groceries | Supermarkets, food shops |
| Dining | Restaurants, cafés, delivery |
| Transport | Public transit, taxis, parking |
| Fuel | Petrol, charging |
| Housing | Rent, mortgage |
| Utilities | Electricity, gas, water, internet |
| Health | Pharmacy, doctor, gym |
| Shopping | Clothes, electronics, general retail |
| Entertainment | Cinema, concerts, hobbies |
| Subscriptions | Netflix, Spotify, SaaS |
| Travel | Hotels, flights, holidays |
| Investment | ETF savings plan, broker deposits |
| Salary | Paycheque, freelance income |
| Other | Anything that doesn't fit |

Names are **case-sensitive** — use exactly as shown (e.g. `Housing`, not
`housing`). For `recurring.yml`, set `type: EXPENSE` or `type: INCOME` to match
the entry (rent → `Housing` + `EXPENSE`, salary → `Salary` + `INCOME`).

To add a new category, see [`.claude/skills/add-category/SKILL.md`](.claude/skills/add-category/SKILL.md).

## Backing up your data

You already have three ways to keep a copy outside the live app:

1. **CSV export (manual, human-readable)** — send `/export` in Telegram. The bot
   replies with `finappa_export.csv` containing every transaction:

   | Column | Contents |
   |--------|----------|
   | `timestamp` | UTC ISO timestamp |
   | `amount_eur` | Amount in euros (e.g. `30.00`) |
   | `type` | `EXPENSE` or `INCOME` |
   | `category` | Category name |
   | `merchant` | Merchant display name, if any |
   | `notes` | Free-text note |
   | `source` | `message`, `recurring`, etc. |

   Save the file somewhere safe (cloud drive, email to yourself). Run it whenever
   you want a snapshot — e.g. after a big logging session or before a deploy.

2. **Encrypted daily backup (automatic)** — if `BACKUP_CHANNEL_ID` and
   `BACKUP_ENCRYPTION_KEY` are set in `.env`, the bot sends an encrypted SQLite
   snapshot to your private Telegram backup channel every night (~03:30 Berlin).
   Full restore procedure:
   [`.claude/skills/restore-backup/SKILL.md`](.claude/skills/restore-backup/SKILL.md).

3. **Server copy (manual)** — on the host, `data/finappa.db` is the live database.
   Stop the container and copy `data/` before risky changes. Local plaintext
   snapshots also land in `data/backup-YYYY-MM-DD.db` (newest 7 kept).

For "app is broken but I still have my numbers", `/export` is the quickest path.
For full recovery including merchants and categories, use the encrypted backup or
`finappa.db`.

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
