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
- `/income add|remove|list` — attach a payout and cooldown to a Discord role
- `/collect` — members collect every income role they hold that is ready
- `/work`, `/crime`, `/steal`, `/slut` — active income, with per-guild odds, rewards and fines
- `/activity set|enable|disable|list` — configure every number an activity uses
- `/hungrygames start|join|status|cancel` — a paced elimination game with a shared pot
- `/gameconfig show|set` — rename the game per server, set pacing, style, spoilers and card mode
- A join button on the signup message, and a generated battle card each round
- `/leaderboard` and `/gamestats` — nine statistics over every completed game
- Editable narration — every activity and game line comes from a plain text file
- `/ping` — Discord gateway latency and PostgreSQL reachability
- Immediate economy-data deletion when a member leaves; game history anonymised, not erased
- Per-role collection cooldown reset when a member loses that role
- Balance changes are row-locked and every movement writes a ledger entry
- Tested fine behavior: cash to its floor first, then bank to its floor
- Tested capability combining for members holding several administrative roles
- Hardened `systemd` unit

Transaction history (`/history`), per-guild narration editing (`/flavor`), interest, and delegated
permissions are the next milestone. The schema already supports them — see `ECONOMY_SPEC.md`.

### Command reference

| Command | Who | What it does |
|---|---|---|
| `/setup` | Administrator | Opens the configuration wizard. Must be run before anything else — accounts have a foreign key to guild settings. |
| `/settings` | Anyone | Shows currency, timezone, balance floors, and whether the economy is enabled. |
| `/balance [member]` | Anyone | Cash, bank, and total. Defaults to you. |
| `/deposit <amount>` | Anyone | Cash into bank. Accepts `all`, `half`, `2.5k`, `1,000`. |
| `/withdraw <amount>` | Anyone | Bank into cash. Same amount formats. |
| `/pay <member> <amount>` | Anyone | Sends cash. Cannot overdraft, cannot target bots or yourself. |
| `/economy add <member> <amount> [cash\|bank]` | Administrator | Creates currency. The only way money enters an economy. Writes a ledger entry and an audit event. |
| `/economy remove <member> <amount> [cash\|bank]` | Administrator | Removes currency, stopping at the configured floor and reporting any shortfall. |
| `/income add <role> <payout> <cooldown>` | Administrator | Attaches income to a role. Cooldowns accept `30m`, `12h`, `1d`, `1d12h`. Re-running updates the rule. |
| `/income remove <role>` | Administrator | Stops a role paying and clears its cooldowns. |
| `/income list` | Administrator | Every earning role, its payout and cadence. |
| `/collect` | Anyone | Collects every ready income role at once, and shows when the rest return. |
| `/activity set <activity> <cooldown> <chance> <reward_min> <reward_max> <fine_min> <fine_max>` | Administrator | Configures an activity. Nothing is hardcoded. |
| `/activity enable\|disable <activity>` | Administrator | Turns one on or off. |
| `/activity list` | Administrator | Every activity and how it is configured. |
| `/work`, `/crime`, `/slut` | Anyone | Attempt active income. Success pays from the reward range; failure is fined from the fine range. |
| `/steal <member>` | Anyone | Takes cash from another member, capped at what they actually hold. |
| `/hungrygames start [entry_fee] [signup] [seed] [min_players] [style] [round_time]` | Economy admin | Opens signups in the current channel. Every option defaults to the server setting, so bare `/hungrygames start` works. |
| `/hungrygames join` | Anyone | Enters as a tribute, paying the entry fee from cash. The signup message also carries an **Enter the arena** button that does the same thing. |
| `/hungrygames status` | Anyone | Current game state, tributes, and pot. |
| `/hungrygames cancel` | Economy admin | Cancels the game and refunds every entry fee. |
| `/gameconfig show` | Administrator | The server's game name, pacing, default style and spoiler chance. |
| `/gameconfig set [name] [round_time] [signup] [default_style] [spoiler_percent] [battle_cards]` | Administrator | Changes any of the above. Games already running keep their own pacing. |
| `/leaderboard [stat]` | Anyone | Wins, kills, times killed, arena deaths, games played, win rate, winnings, average finish, best kill streak. |
| `/gamestats [member]` | Anyone | One member's full record. Defaults to you. |
| `/ping` | Anyone | Connectivity check. |

Members cannot voluntarily go negative. Balance floors exist so **fines** and administrative
removals can collect into debt; they are not an overdraft members may draw on themselves.

### Activities

Activities do nothing until an administrator configures them — a fresh server has no accidental
economy. Every number is per-guild and adjustable at any time: cooldown, success chance, reward
range, and fine range.

```
/activity set activity:work cooldown:1h success_chance:75% reward_min:100 reward_max:400 fine_min:50 fine_max:150
```

Success chance accepts `75`, `75%`, or `0.75`. A failed attempt is fined using the tested
cash-then-bank floor logic in `domain/fines.py`, and the response reports what was actually
collected versus what could not be — a member already at their floor pays less than the fine
demanded, and the message says so rather than quietly differing.

`/steal` is the only activity that moves existing currency rather than creating it. It is capped at
the target's cash, and a target already in debt has nothing takeable — stealing cannot push someone
further below a floor they did not choose.

Randomness is injected rather than generated inside the resolution logic, so every branch, boundary
and rounding rule is tested deterministically. Rolls use `random.SystemRandom`.

Missed income windows do not accumulate — a member three days late on a 12-hour income
collects one payout, not six, and the next window starts from the moment they collect.
Losing a role clears its cooldown, so regaining it grants immediate eligibility.

### The Hungry Games

An elimination game paced over real time. An administrator opens signups, members join as
tributes, and once signups close the bot posts a round every few seconds until one tribute is
left standing and takes the pot.

```
/hungrygames start entry_fee:250 signup:5m seed:5000 min_players:4
```

The pot is the admin seed plus every entry fee. **A seed creates currency** and is audited as
`currency_created`, exactly like `/economy add`; entry fees only redistribute. Cancelling — or
failing to reach `min_players` — refunds every fee.

Game state lives in the database rather than in memory, so restarting the bot mid-game resumes
it instead of stranding everyone's entry fees. A partial unique index enforces one active game
per guild, so two people starting at the same instant cannot both succeed.

Two rules keep a game finite and non-degenerate: at least one elimination whenever two or more
tributes remain, so an unlucky run cannot continue forever; and never more than `alive - 1`, so
the arena always ends with a winner rather than empty.

**A game needs at least three tributes.** Two is a coin flip with narration attached. Player
count fixes the length exactly — five tributes is always three rounds, twenty is always seven —
so the round gap is what controls how long a game actually takes.

#### Styles

The organizer picks one at start; the server sets the default with `/gameconfig set`.

| Style | What eliminations mean |
|---|---|
| `standard` | Nothing. Narration and the pot, no obligations afterwards. **Shipped default.** |
| `random_tasks` | A forfeit drawn from the event files, different every time. |
| `organizer_defined` | Three outcomes the organizer types into a modal at start. Shown in the signup embed, so nobody enters without reading the terms. |

#### Numbering and statistics

Games are numbered per server, sequentially, **and only when they finish**. A cancelled game
keeps its ledger trail — the entry fees really did move and really were refunded — but never
takes a number and never reaches a leaderboard. Numbering at completion rather than at start is
what keeps the sequence contiguous.

Every kill, arena death and survival is written to `game_events`. Statistics are queries over
that table rather than running totals, which means a new statistic later needs no migration and
is computed over games that already happened instead of starting from zero.

`/leaderboard` covers wins, kills, times killed, arena deaths, games played, win rate, total
winnings, average finish and best kill streak in a single game. Win rate requires at least three
games played, or somebody who played once and won would sit permanently at 100%.

#### Battle cards

Each round with an elimination posts a generated image: the killer and their victim side by side
with crossed swords, or a lone portrait for whoever the arena took. The eliminated face is
greyscaled and dimmed, so the outcome reads without reading.

The card is **drawn in code rather than composited onto a template**, because servers can rename
the game — a name baked into an image asset would have every server's card announcing the same
thing. Drawing it means the card says theirs.

Nothing touches disk. Cards render to memory and upload directly, which is what lets the systemd
unit keep `ProtectSystem=strict` with no `ReadWritePaths=` exception.

`/gameconfig set battle_cards:` switches between `off`, one card per `round`, and one per `kill`.
**Turning them off is a slash command, never a deploy** — the point of shipping this separately
was being able to back out of it in seconds.

Cost is recorded per game: cards drawn, milliseconds spent, bytes uploaded. Logs answer "was that
round slow"; the counters on the game row answer "what has this cost me over a month", which is
the question that decides whether it stays. Rendering is capped at three pairs per card and runs
in a worker thread so a slow draw cannot stall the game loop, and any failure posts the round
without a picture rather than dropping it.

#### Spoilers

Roughly one round in ten posts with its names hidden behind Discord spoiler bars, so it has to be
clicked to be read. The chance is per-guild (`/gameconfig set spoiler_percent:`). It is
deliberately occasional: an always-spoilered game is a chore, a rare one is a surprise.

A hidden round's card is sent as a standalone spoilered attachment rather than inside the embed —
an image pulled into an embed renders immediately and the spoiler would be lost.

#### Renaming

`/gameconfig set name:"Crimson Fields"` changes every displayed name — embed titles, recaps,
leaderboard headers. **It does not change the command.** Discord command names are global to the
bot, so `/hungrygames` is what everyone types on every server regardless of what their game is
called.

`/hungrygames join` and `status` are open to everyone; `start` and `cancel` check authority in
code. The group deliberately does **not** declare `default_member_permissions` — Discord honours
that only on top-level commands and subcommands inherit it, so gating the group would have
hidden `join` from the members who need it.

### Narration

Every activity and game line is read from `src/shadow_bot/data/narration/default.txt` rather than
hardcoded:

```
[work.success]
{user} pulled a double at the diner and made {amount}.
{user} sold hand-drawn portraits outside the station and earned {amount}.
```

One line per entry under a `[category.outcome]` header. Blank lines and `#` comments are ignored.
**Nothing needs quoting or escaping** — apostrophes and quotation marks are written normally,
which is the entire reason this is not TOML or JSON.

| Placeholder | Meaning |
|---|---|
| `{user}` | the member acting |
| `{amount}` | formatted money |
| `{target}` | the other member |
| `{currency}` | plural currency name |
| `{tribute}` | a games participant |
| `{killer}` | who made the kill (same as `{tribute}` in a kill line) |
| `{game}` | this server's name for the game |
| `{victim}` | who they eliminated |
| `{winner}` | the surviving tribute |
| `{pot}` | the prize pot |

Within a single game, lines are drawn **without replacement** — a line will not appear again
until its section is exhausted, and when the pool wraps it never lands on the line that just
fired. Independent random draws repeat far more than people expect: a five-player game runs three
rounds against five kill lines, and plain random selection produced two identical lines in
adjacent events during testing. It reads as a bug even though each draw was fair.

Substitution is a plain regex, **not** `str.format`. A line containing `{user.__class__.__mro__}`
renders literally instead of reaching into the object — which matters because narration is meant
to become editable from Discord. Unknown placeholders are left visible, so a typo like `{amout}`
appears in the message rather than silently vanishing.

The file ships as package data (`[tool.setuptools.package-data]`). Without that entry it would be
missing from the installed package and every line would fall back to plain wording.

#### Forfeits

Three sections attach a **social consequence** to how a tribute left the arena, posted under the
round line as `↳ *forfeit*`:

| Section | When |
|---|---|
| `hungrygames.forfeit_death` | eliminated by the arena |
| `hungrygames.forfeit_kill` | eliminated by another tribute — `{victim}` owes `{tribute}` |
| `hungrygames.reward_winner` | what the winner gets to impose |

The bot posts these and does not enforce them. That keeps the whole feature editable per guild
with no extra schema, and it composes with the currency payout rather than replacing it.

### Who sees which commands

Administrative commands declare `default_member_permissions = Administrator`, so Discord only
shows them to administrators. Everything else is visible to every member.

| Visible to administrators | Visible to everyone |
|---|---|
| `/setup`, `/economy add`, `/economy remove`, `/income add`, `/income remove`, `/income list`, `/activity set`, `/activity enable`, `/activity disable`, `/activity list` | `/settings`, `/balance`, `/deposit`, `/withdraw`, `/pay`, `/collect`, `/work`, `/crime`, `/steal`, `/slut`, `/hungrygames`, `/ping` |

Two things to know about this:

* **Discord applies the permission to a whole command group**, never per subcommand. `/income list`
  is therefore administrator-only because it shares a group with `add` and `remove`. Members see
  their own earning roles and cooldowns through `/collect`.
* **Visibility is not authorisation.** `default_member_permissions` only decides what Discord
  displays; server administrators can override it per role under Server Settings → Integrations.
  Every administrative command independently re-checks authority in code, so overriding visibility
  does not grant the ability to run anything.

Currency creation, removal, and role income configuration accept the guild owner, the application
owner, and — as an interim measure until capability grants exist — anyone with Discord's
Administrator permission. See the deviation note in `ECONOMY_SPEC.md`.

**If members cannot see commands they should**, check `@everyone` has **Use Application Commands**
in Server Settings → Roles, and that no channel overwrite denies it. That permission is enforced by
Discord before any of this applies.
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

A healthy startup logs one `Loaded extension ...` line per cog, then
`Synced N command(s)`, then `Ready as Shadow Bot`. Run `/ping` in the test server
— it should report Discord and PostgreSQL both connected.

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
4. `alembic upgrade head` with the environment loaded (section 5)
5. `sudo systemctl start shadow-bot` and check the logs

v0.7.0 adds `0005` (battle cards) and a new dependency, **Pillow** — `pip install --upgrade`
is therefore not optional either. v0.6.0 added **two** migrations: `0003` repairs CHECK constraint names created by `0001` and
`0002` (catalog-only, no table rewrite), and `0004` is the games expansion. Run both with a
single `alembic upgrade head`.

Step 4 is **not optional when a release adds a migration.** Copying files alone leaves the code
expecting tables that do not exist, and the failure surfaces later as a command error rather than
at startup. `alembic current` shows the deployed revision; `alembic heads` shows what the code
expects. If they differ, run the upgrade.

If commands look missing afterwards, **restart the Discord client** (Ctrl+R) before debugging the
bot — Discord caches the command list on the client side.

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
