# Shadow Bot

A private, multi-server Discord bot. Each guild gets a fully isolated economy,
configuration, and currency identity. Radarr/Sonarr request features come later.

Deployment target: a Debian LXC container on Proxmox, talking to PostgreSQL in a
separate container over the private LAN.

---

## What works today

- Python 3.11+ on `discord.py` 2.5+, async PostgreSQL via SQLAlchemy 2.0 + psycopg 3
- Versioned schema through Alembic
- `/setup` — guild-owner modal wizard for currency name, plural, symbol, and timezone
- `/settings` — show the current configuration
- `/balance` — cash, bank, and total for yourself or another member
- `/deposit` and `/withdraw` — move money between cash and bank
- `/pay` — send cash to another member
- `/economy add` and `/economy remove` — owner-only currency creation and removal, audited
- `/ping` — Discord gateway latency and PostgreSQL reachability
- Immediate economy-data deletion when a member leaves, is kicked, or is banned
- Per-role collection cooldown reset when a member loses that role
- Balance changes are row-locked and every movement writes a ledger entry
- Tested fine behavior: cash to its floor first, then bank to its floor
- Tested capability combining for members holding several administrative roles
- Hardened `systemd` unit

Role income, activities (work/steal/crime), interest, and delegated permissions are the next
milestone. The schema already supports them — see `ECONOMY_SPEC.md`.

### Command reference

| Command | Who | What it does |
|---|---|---|
| `/setup` | Server owner | Opens the configuration wizard. Must be run before anything else — accounts have a foreign key to guild settings. |
| `/settings` | Anyone | Shows currency, timezone, balance floors, and whether the economy is enabled. |
| `/balance [member]` | Anyone | Cash, bank, and total. Defaults to you. |
| `/deposit <amount>` | Anyone | Cash into bank. Accepts `all`, `half`, `2.5k`, `1,000`. |
| `/withdraw <amount>` | Anyone | Bank into cash. Same amount formats. |
| `/pay <member> <amount>` | Anyone | Sends cash. Cannot overdraft, cannot target bots or yourself. |
| `/economy add <member> <amount> [cash\|bank]` | Server owner | Creates currency. The only way money enters an economy. Writes a ledger entry and an audit event. |
| `/economy remove <member> <amount> [cash\|bank]` | Server owner | Removes currency, stopping at the configured floor and reporting any shortfall. |
| `/ping` | Anyone | Connectivity check. |

Members cannot voluntarily go negative. Balance floors exist so **fines** and administrative
removals can collect into debt; they are not an overdraft members may draw on themselves.

Currency creation and removal are limited to the guild owner and the application owner.
Discord's Administrator permission grants no economy authority — per `ECONOMY_SPEC.md`,
delegation happens through capability grants, which are a later milestone.

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

`tests/test_extensions.py` imports every cog listed in `EXTENSIONS` in `src/shadow_bot/bot.py`.
Add a cog, add it to that tuple — otherwise the suite fails, which is deliberate. A broken cog
import is invisible to unit tests but fatal at startup, and that has already happened once here.

### Database integration tests

`tests/test_economy_db.py` runs against a real PostgreSQL instance and is **skipped unless
`TEST_DATABASE_URL` is set**. The variable is deliberately not `DATABASE_URL`, so running the
suite on the bot host can never point these at live data — they truncate tables.

```bash
TEST_DATABASE_URL=postgresql+psycopg://shadow_bot:pw@127.0.0.1:5432/shadow_bot \
    .venv/bin/python -m pytest tests/test_economy_db.py
```

These exist because row locking cannot be verified with mocks. The bug they guard against —
two concurrent transfers reading the same balance, the second overwriting the first, currency
appearing from nowhere — only reproduces when real transactions contend for real rows. Removing
`with_for_update()` makes `test_concurrent_payments_cannot_create_money` fail immediately, which
is how you know it is doing its job.

## Security notes

- `.env` and the production `bot.env` are never committed.
- The bot token, PostgreSQL password, and future ARR API keys never get pasted
  into Discord — a leaked token is full control of the bot account.
- PostgreSQL, Radarr, Sonarr, and Plex stay on the private network.
- If the token is ever exposed, **Reset Token** in the portal immediately; the
  old one dies at once.
