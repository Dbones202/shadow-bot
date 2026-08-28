# Changelog

All notable changes to Shadow Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-08-28

### Added
- **Event file library (M9)**: narration text moved off the single bundled `default.txt` into
  five per-category files (`hungrygames.md`, `work.md`, `crime.md`, `steal.md`, `slut.md`), plus
  an optional `EVENTS_DIR` env var pointing at an external copy outside the installed package —
  edits there survive `pip install --upgrade`, with a fallback to the packaged copy if it's
  unset, missing, empty, or has an invalid file. No live reload; a restart picks up an edit.
- **`/work`, `/crime`, `/steal`, `/slut` now narrate**: these previously rendered no flavor text
  at all. They now pull the guild's narration library per outcome, falling back to the original
  hardcoded sentence until the corresponding `.md` file has lines.

### Changed
- `GamesCog` and `ActivitiesCog` now share one narration-assembly path (`cogs._narration`,
  `db.narration`) instead of `GamesCog` alone owning the bundled defaults.

## [0.4.0] - 2026-08-21

### Added
- **Active Income System (Activities)**:
  - `/activity set <activity> <cooldown> <chance> <reward_min> <reward_max> <fine_min> <fine_max>`: Guild-scoped configuration for active commands with support for single and compound cooldowns, flexible percentages (`75`, `75%`, `0.75`), and min/max reward and fine ranges.
  - `/activity enable <activity>` & `/activity disable <activity>`: Administrative toggles to activate or deactivate individual activities without deleting their configurations.
  - `/activity list`: Overview of all configured server activities, displaying odds, reward and fine ranges, cooldown intervals, and statuses.
  - `/work`, `/crime`, `/slut`: Active earning commands resolving outcomes deterministically with half-up scaling and cryptographic randomness (`random.SystemRandom`).
  - `/steal <member>`: Peer-to-peer cash theft capped at the target's positive cash balance, preserving total server currency and preventing unconsented debt accumulation.
- **Administrator Permission Integration**:
  - Discord command group permissions (`default_permissions=discord.Permissions(administrator=True)`) applied to `/setup`, `/economy`, `/income`, and `/activity`.
  - Interim `Authority.GUILD_ADMIN` classification in `domain.authority` enabling members with Discord's Administrator permission to manage economy settings and income.
  - Safe `has_admin_permission(user)` helper ensuring strict type-safe guild permission checks.
- **Concurrency & Database Protection**:
  - Deadlock-free account and cooldown row-level locking in `db.activities`.
  - Paired ledger entries with shared correlation IDs for steal transactions.
  - Dual floor fine enforcement (cash down to cash floor, then bank down to bank floor) reporting exact collected amounts vs uncollected shortfalls.
- **Test Suites**:
  - New test suite in `tests/test_activities.py` covering activity resolution, range scaling, probability edge cases, and half-up rounding.
  - Database integration tests in `tests/test_economy_db.py` for activity CRUD, concurrent attempts, and steal currency conservation.
  - Authority resolution tests in `tests/test_authority.py`.

## [0.3.0] - 2026-08-19

### Added
- **Role Income System**:
  - `/income add <role> <payout> <cooldown>`: Attaches recurring payout with custom cooldowns to Discord roles. Re-running updates the existing rule.
  - `/income remove <role>`: Removes income from a role and purges associated cooldowns.
  - `/income list`: Displays all configured role income rules, payouts, intervals, and statuses.
  - `/collect`: Allows members to claim all currently eligible role payouts in a single transaction and reports cooldown countdowns for pending roles.
- **Duration Parsing and Formatting**:
  - Human-readable duration parser supporting single and compound time spans (e.g., `30m`, `12h`, `1d`, `1d12h`, `1w`).
  - Cooldown formatter and Discord relative timestamp generator (`<t:TIMESTAMP:R>`) for localized time representations.
- **Collection Engine & Concurrency Control**:
  - Pure domain collection planner (`domain.income`) enforcing non-accumulating missed windows and deterministic output sorting.
  - Row-locked database execution in `db.income` preventing race conditions during concurrent collection attempts.
  - Correlation-ID tagged ledger entries for every paid role within a collection run.
- **Continuous Integration**:
  - GitHub Actions CI workflow running across Python 3.11, 3.12, and 3.13.
  - PostgreSQL 16 container service with UTF-8 encoding and C locale verification.
  - Bidirectional Alembic migration validation (`upgrade head` -> `downgrade base` -> `upgrade head`).
  - Automated linting and formatting enforcement via `ruff`.
- **Test Suites**:
  - New test suites in `tests/test_durations.py`, `tests/test_income.py`, and `tests/test_version.py`.
  - Expanded `tests/test_economy_db.py` covering role income CRUD, collection cooldown expiration, and concurrent collection stress tests.

## [0.2.0] - 2026-08-19

### Added
- **Member Banking & Commands**:
  - `/balance [member]`: View cash, bank, and total net worth.
  - `/deposit <amount>` & `/withdraw <amount>`: Move funds between cash and bank balances with flexible amount strings (`all`, `half`, `2.5k`, `1,000`).
  - `/pay <member> <amount>`: Peer-to-peer cash transfers with overdraft prevention.
- **Guild Configuration & Administration**:
  - `/setup`: Guild-owner modal configuration wizard for currency naming, symbols, and timezone.
  - `/settings`: Display server economy configuration.
  - `/economy add` and `/economy remove`: Owner currency minting and removal with floor constraints.
- **Data Protection & Lifecycle Events**:
  - Automatic purging of member economy data upon guild leave, kick, or ban.
  - Automatic cooldown reset when a member loses a role.
  - Audited ledger logging with row-level locking for all balance operations.
- **Deployment Hardening**:
  - Systemd service unit for Debian LXC environments.
  - Single source of truth version management via `VERSION` file.

## [0.1.0] - 2026-08-19

### Added
- Initial project structure with `discord.py` 2.5+, SQLAlchemy 2.0 (asyncio), and PostgreSQL (`psycopg`).
- Alembic database migration pipeline.
- Base bot skeleton, environment configuration, privacy policy, and terms of service.
