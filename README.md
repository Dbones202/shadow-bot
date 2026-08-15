# Discord Economy Bot

This is the first foundation milestone for a private, multi-server Discord economy and future
Radarr/Sonarr request bot. Each Discord server has an isolated economy and configuration.

## Included in this milestone

- Python 3.11+ application using `discord.py`
- Asynchronous PostgreSQL access through SQLAlchemy and psycopg
- Versioned database migrations through Alembic
- `/ping` status command for Discord and PostgreSQL
- Server Members and optional Message Content intents
- Immediate database cleanup when a member leaves, is kicked, or is banned
- Per-role collection cooldown reset when a member loses that role
- Initial schema for accounts, balances, ledger entries, role income, activities, interest,
  flavor text, cooldowns, delegated permissions, usage limits, and audit events
- Tested fine behavior that uses cash to its floor and then uses bank
- Tested capability combining for members with multiple administrative roles
- A hardened `systemd` service template

Economy member commands and the interactive setup wizard are the next milestone. The schema is
present now so later commands can be added without restructuring the application.

## 1. Create the Discord application

1. Visit <https://discord.com/developers/applications> and create an application.
2. Open **Bot** and create the bot user.
3. Enable **Server Members Intent**.
4. Enable **Message Content Intent** for the later features that will require it.
5. Copy the bot token into your protected server configuration later. Never put it in this project.
6. In the installation settings, enable the `bot` and `applications.commands` scopes.
7. Grant the bot **View Channels**, **Send Messages**, and **Embed Links** initially.
8. Install it in a private test server.

Turn on Developer Mode in your Discord client under **User Settings → Advanced**. You can then
right-click yourself and the test server to copy their numeric IDs.

## 2. Prepare PostgreSQL

Run the following from the PostgreSQL container as a PostgreSQL administrator:

```sql
CREATE ROLE discord_bot LOGIN;
\password discord_bot
CREATE DATABASE discord_bot OWNER discord_bot;
```

Choose a unique password when prompted. This avoids putting it into shell history.

Because PostgreSQL is in a separate container:

- Allow PostgreSQL to listen on its private LAN address.
- Add a `pg_hba.conf` entry permitting only the bot container's IP address and `discord_bot` user.
- Allow TCP port 5432 from the bot container only.
- Do not expose PostgreSQL to the public internet.

The exact PostgreSQL configuration file locations depend on its Ubuntu and PostgreSQL versions.

## 3. Transfer the project

The whole `discord-economy-bot` folder is the deployable project. Transfer it with WinSCP to a
temporary location on the Debian bot container, such as `/tmp/discord-economy-bot`.

Then connect over SSH and run:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
sudo adduser --system --group --home /opt/discord-economy-bot discordbot
sudo cp -a /tmp/discord-economy-bot/. /opt/discord-economy-bot/
sudo chown -R discordbot:discordbot /opt/discord-economy-bot
sudo -u discordbot python3 -m venv /opt/discord-economy-bot/.venv
sudo -u discordbot /opt/discord-economy-bot/.venv/bin/pip install /opt/discord-economy-bot
```

## 4. Create the protected configuration

```bash
sudo install -d -m 750 -o root -g discordbot /etc/discord-economy-bot
sudo cp /opt/discord-economy-bot/.env.example /etc/discord-economy-bot/bot.env
sudo chown root:discordbot /etc/discord-economy-bot/bot.env
sudo chmod 640 /etc/discord-economy-bot/bot.env
sudo nano /etc/discord-economy-bot/bot.env
```

Set these values:

- `DISCORD_TOKEN`: token from the Discord Developer Portal
- `DATABASE_URL`: private PostgreSQL connection URL
- `BOT_OWNER_IDS`: your Discord user ID
- `TEST_GUILD_ID`: your private test server ID

Keep the database password URL-encoded if it includes URL-reserved characters. For example, `@`
inside the password becomes `%40`.

## 5. Create the database tables

```bash
cd /opt/discord-economy-bot
sudo -u discordbot bash -c \
  'set -a; source /etc/discord-economy-bot/bot.env; set +a; .venv/bin/alembic upgrade head'
```

Alembic records the installed schema version. Future updates will add migrations rather than asking
you to recreate the database.

## 6. Install and start the service

```bash
sudo cp /opt/discord-economy-bot/deploy/discord-economy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-economy-bot
sudo systemctl status discord-economy-bot
```

To follow its logs:

```bash
sudo journalctl -u discord-economy-bot -f
```

Run `/ping` in the test server. It should show Discord and PostgreSQL as connected.

## Updating later

For future milestones:

1. Stop the service.
2. Transfer the updated project files.
3. Reinstall the project into its virtual environment.
4. Run `alembic upgrade head` with the environment loaded.
5. Start the service and check its logs.

Exact commands will be included with each milestone.

## Local checks

From a development virtual environment with the optional development dependencies installed:

```bash
python -m pytest
ruff check .
ruff format --check .
```

## Security notes

- Never commit `.env` or the production `bot.env` file.
- Never paste the Discord token, PostgreSQL password, or future ARR API keys into Discord.
- Restrict PostgreSQL, Radarr, Sonarr, and Plex to your private network.
- Give the bot only the Discord permissions needed by implemented features.

