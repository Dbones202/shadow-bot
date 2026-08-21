# Economy specification — approved foundation

## Scope and ownership

- Every Discord guild has an independent economy, configuration, and currency identity.
- The guild owner is the root economy administrator and cannot be locked out.
- The application owner has emergency cross-guild access for support.
- The guild owner may grant capabilities to Discord roles.
- Discord's general Administrator permission does not automatically grant economy access.

> **Interim deviation (2026-08-19).** The delegated capability system is not built yet, which
> would leave the guild owner as the only person able to run anything. Until it exists, members
> holding Discord's **Administrator** permission are treated as economy administrators, and the
> administrative commands are made visible to them by default. Audit records distinguish
> `guild_owner`, `app_owner`, and `guild_admin`, so actions taken under this interim rule remain
> identifiable afterwards. Revisit when capability grants land — `Authority.GUILD_ADMIN` in
> `domain/authority.py` is the seam to remove.

## Delegated capabilities

Initial capabilities are:

- Manage settings
- Manage permissions
- Manage role income
- Manage activities
- Create currency
- Remove currency
- Reset accounts
- View audit information

A role grant can have an optional maximum per action and maximum per local calendar day. A missing
limit means unlimited. When several member roles grant the same capability, the most permissive
per-action and daily limits apply. Attempts that exceed a limit are rejected and audited.

## Currency and balances

- Each guild configures singular name, plural name, and a symbol or custom Discord emoji.
- New accounts begin with zero cash and zero bank.
- Default floors are -1,000 cash and -10,000 bank; the guild can change both.
- Money uses whole integers.
- Percentage calculations use half-up rounding.
- Deposits and withdrawals have no fees.
- Member payments use cash and cannot overdraft the sender.

## Fines

- Fines draw cash down to the configured cash floor.
- Any remainder draws bank down to its configured bank floor.
- Any remainder after both floors is uncollected.
- The response and audit record contain attempted, collected, and uncollected amounts.

## Cooldowns and collection

- Activity cooldowns are independent for work, steal, crime, and slut.
- Each configured income role has an independent payout and cooldown.
- `/collect` collects every currently eligible role at once and lists the next availability of each
  eligible or cooling-down role.
- Missed role-income windows do not accumulate.
- Losing a role deletes that role's cooldown; regaining it grants immediate eligibility.
- Version one has no activity cooldown reductions based on roles.

## Time and interest

- Persistence uses timezone-aware UTC timestamps.
- Every guild selects an IANA timezone such as `America/Denver` for schedules and displays.
- Discord timestamps may be used so viewers see dates in their own local settings.
- Interest applies automatically only to positive bank balances.
- Each interest period has a unique database record, preventing duplicate payment after restarts.

## Lifecycle and auditing

- Leaving, being kicked, or being banned deletes the member's account, cooldowns, capability usage,
  and owned ledger entries through database cascades.
- References to a departed member in other audit records are anonymized.
- Existing messages in a Discord audit channel remain in Discord.
- Rejoining creates a new zero-balance account.

