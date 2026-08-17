# Shadow Bot

A private, multi-server Discord bot. Each guild gets a fully isolated economy,
configuration, and currency identity. Radarr/Sonarr request features come later.

Deployment target: a Debian LXC container on Proxmox, talking to PostgreSQL in a
separate container over the private LAN.

---

## What works today

- Python 3.11+ on `discord.py` 2.5+
- Async PostgreSQL via SQLAlchemy 2.0 + psycopg 3
- Versioned schema through Alembic
- `/ping` — reports Discord gateway latency and PostgreSQL reachability
- Immediate economy-data deletion when a member leaves, is kicked, or is banned
- Per-role collection cooldown reset when a member loses that role
- Schema for accounts, balances, ledger, role income, activities, interest,
  flavor text, cooldowns, delegated permissions, usage limits, and audit events
- Tested fine behavior: cash to its floor first, then bank to its floor
- Tested capability combining for members holding several administrative roles
- Hardened `systemd` unit

Member-facing economy commands and the setup wizard are the next milestone. The
schema already exists so those commands drop in without restructuring anything.

---

## 1. Create the Discord application

At <https://discord.com/developers/applications>:

1. **New Application** → name it `Shadow Bot` → accept the developer terms → **Create**.
2. **General Information** → copy the **Application ID**. This is not a secret;
   it goes in your install link.
3. **Bot** → **Reset Token** → copy the token somewhere safe.
   This *is* a secret. It never goes in this repository, in Discord, or in a chat.
4. **Bot** → **Privileged Gateway Intents** → enable **Server Members Intent**.
   Leave **Message Content Intent** off — nothing reads message text yet, and
   unused privileged intents complicate verification at 100 servers.
5. **Bot** → turn **Public Bot** off while this is private.

### Install link

This is the live link for application `1505387163376947340` (Shadow):

```
https://discord.com/oauth2/authorize?client_id=1505387163376947340&permissions=8&integration_type=0&scope=bot+applications.commands
```

Before using it, confirm on the **Bot** tab that **Requires OAuth2 Code Grant is
OFF**. When it is on, authorizing returns a temporary code to a redirect URI
instead of adding the bot, and since this project runs no web server the bot
silently never joins.

- `permissions=8` is Administrator. Acceptable for private servers you control.
  To tighten later, `permissions=277025508416` grants exactly what the economy
  spec needs: View Channels, Send Messages, Send in Threads, Embed Links,
  Attach Files, Read History, Add Reactions, Use External Emojis.
- `integration_type=0` installs to a server rather than to a user account.
- Both `bot` and `applications.commands` scopes are required. Without the
  second, slash commands never register and `/ping` will not appear.

Open the link, pick your private test server, authorize.

### Collect your IDs

In Discord: **User Settings → Advanced → Developer Mode** on. Then right-click
yourself → **Copy User ID**, and right-click the server → **Copy Server ID**.

---

## 2. Prepare PostgreSQL

From the PostgreSQL container, as a database administrator:

```sql
CREATE ROLE shadow_bot LOGIN;
\password shadow_bot
CREATE DATABASE shadow_bot OWNER shadow_bot;
```

`\password` prompts interactively, which keeps the password out of shell history.

Then, because PostgreSQL lives in a different container:

- Bind PostgreSQL to its private LAN address (`listen_addresses` in `postgresql.conf`).
- Add a `pg_hba.conf` line permitting only the bot container's IP for the
  `shadow_bot` user, using `scram-sha-256`.
- Allow TCP 5432 from the bot container only.
- Never expose 5432 to the internet.

Config file paths vary by Ubuntu and PostgreSQL version; `SHOW config_file;`
inside `psql` will tell you where they are.

Verify from the *bot* container before going further:

```bash
psql "postgresql://shadow_bot@192.168.1.20:5432/shadow_bot" -c "SELECT 1;"
```

If that fails, fix it now — every later step depends on it.

---

## 3. Deploy the project

Copy the repository to a staging path on the Debian container (WinSCP, `scp`,
or `git clone`), then:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
sudo adduser --system --group --home /opt/shadow-bot shadowbot
sudo cp -a /tmp/shadow-bot/. /opt/shadow-bot/
sudo chown -R shadowbot:shadowbot /opt/shadow-bot
sudo -u shadowbot python3 -m venv /opt/shadow-bot/.venv
sudo -u shadowbot /opt/shadow-bot/.venv/bin/pip install /opt/shadow-bot
```

---

## 4. Create the protected configuration

```bash
sudo install -d -m 750 -o root -g shadowbot /etc/shadow-bot
sudo cp /opt/shadow-bot/.env.example /etc/shadow-bot/bot.env
sudo chown root:shadowbot /etc/shadow-bot/bot.env
sudo chmod 640 /etc/shadow-bot/bot.env
sudo nano /etc/shadow-bot/bot.env
```

Root owns it, the service account can only read it. Fill in `DISCORD_TOKEN`,
`DATABASE_URL`, `BOT_OWNER_IDS`, and `TEST_GUILD_ID`; `.env.example` documents
each one.

URL-encode reserved characters in the database password: `@` → `%40`, `#` → `%23`,
`/` → `%2F`.

---

## 5. Create the database tables

```bash
cd /opt/shadow-bot
sudo -u shadowbot bash -c \
  'set -a; source /etc/shadow-bot/bot.env; set +a; .venv/bin/alembic upgrade head'
```

Alembic records the schema version, so future milestones ship migrations rather
than asking you to rebuild the database.

---

## 6. Install and start the service

```bash
sudo cp /opt/shadow-bot/deploy/shadow-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shadow-bot
sudo systemctl status shadow-bot
sudo journalctl -u shadow-bot -f
```

A healthy startup logs `Loaded extension ...` twice, then `Synced N command(s)`,
then `Ready as Shadow Bot`. Run `/ping` in the test server — it should report
Discord and PostgreSQL both connected.

### If it does not start

| Log line | Cause |
|---|---|
| `PrivilegedIntentsRequired` | An intent is `true` in `bot.env` but off in the Developer Portal. |
| `ConfigurationError: Required environment variable ...` | `bot.env` is missing a value, or systemd cannot read it. |
| `DATABASE_URL must start with postgresql+psycopg://` | The URL is using a sync driver prefix. |
| Connection refused / timeout on 5432 | `pg_hba.conf`, `listen_addresses`, or the firewall. Retest with `psql`. |
| Slash commands never appear | `applications.commands` was missing from the install link — re-invite. |

---

## Updating

1. `sudo systemctl stop shadow-bot`
2. Copy the new files over `/opt/shadow-bot`
3. `sudo -u shadowbot /opt/shadow-bot/.venv/bin/pip install --upgrade /opt/shadow-bot`
4. `alembic upgrade head` with the environment loaded (step 5)
5. `sudo systemctl start shadow-bot` and check the logs

---

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

`tests/test_extensions.py` imports every cog listed in `EXTENSIONS` in
`src/shadow_bot/bot.py`. Add a cog, add it to that tuple — otherwise the test
suite fails, which is deliberate. A broken cog import is invisible to unit
tests but fatal at startup, and that has already happened once here.

---

## Security notes

- `.env` and the production `bot.env` are never committed.
- The bot token, PostgreSQL password, and future ARR API keys never get pasted
  into Discord — a leaked token is full control of the bot account.
- PostgreSQL, Radarr, Sonarr, and Plex stay on the private network.
- If the token is ever exposed, **Reset Token** in the portal immediately; the
  old one dies at once.
