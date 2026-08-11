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
   Expect HTTP 200. Then open `http://<host-public-ip>.nip.io:<STATS_PORT>/`
   in a browser and sign in with Basic Auth.
