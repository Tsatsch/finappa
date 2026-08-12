---
name: deploy
description: Deploy the latest committed finappa code to the production host. Use when the user asks to deploy, ship, or push finappa changes live.
---

# Deploy finappa

Manual deploy, by design (spec §10) — this is a one-user bot, deployed by hand a
couple of times a month. There is no CI/CD; do not propose adding it here.

1. Confirm the working tree is clean and the change is committed and pushed:
   `git status` and `git log -1`.
2. SSH into the host:
   ```bash
   ssh <user>@<host>
   ```
3. Pull and rebuild:
   ```bash
   cd finappa
   git pull
   docker compose up -d --build
   ```
4. Confirm it started cleanly:
   ```bash
   docker compose logs -f --tail=50
   ```
   Look for the polling loop starting with no traceback. Ctrl+C to stop
   following.
5. Verify from Telegram: send `/summary` to the bot and confirm a reply arrives.
   If nothing replies within ~10 seconds, re-check the logs — a common cause is
   a typo'd `.env` value causing `Settings()` validation to fail at startup, in
   which case the container will be in a restart loop (`docker compose ps` shows
   `Restarting`).
6. If the stats service is enabled (`STATS_USERNAME` / `STATS_PASSWORD` set),
   confirm it is up:
   ```bash
   docker compose ps
   docker compose logs stats --tail=50
   curl -I -u "$STATS_USERNAME:$STATS_PASSWORD" "http://127.0.0.1:${STATS_PORT:-8080}/"
   ```
   Expect HTTP 200 on localhost. Public access should go through barcheck Caddy
   over HTTPS — see step 7.

7. **HTTPS via barcheck Caddy** (recommended on a shared host):
   - Confirm the shared Docker network name:
     ```bash
     docker inspect barcheck-caddy-1 --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}'
     ```
     Set `STATS_CADDY_NETWORK` in `.env` if it is not `barcheck_default`.
   - Add the block from `deploy/caddy-finappa.caddy` to the barcheck Caddyfile
     (replace `YOURDOMAIN`), create DNS `finappa.YOURDOMAIN` → server IP, reload
     Caddy.
   - Verify:
     ```bash
     curl -I -u "$STATS_USERNAME:$STATS_PASSWORD" "https://finappa.YOURDOMAIN/"
     ```
     Expect HTTP 200. Stats binds `127.0.0.1:${STATS_PORT}` only — Caddy
     reaches `finappa-stats-1:8080` on the Docker network, not via the host port.
