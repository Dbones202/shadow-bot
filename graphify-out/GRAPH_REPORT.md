# Graph Report - Discord-Bot  (2026-08-15)

## Corpus Check
- 23 files · ~7,760 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 133 nodes · 173 edges · 16 communities (10 shown, 6 thin omitted)
- Extraction: 82% EXTRACTED · 18% INFERRED · 0% AMBIGUOUS · INFERRED: 31 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]

## God Nodes (most connected - your core abstractions)
1. `Base` - 16 edges
2. `Discord Economy Bot` - 11 edges
3. `EconomyBot` - 8 edges
4. `ConfigurationError` - 8 edges
5. `TimestampMixin` - 8 edges
6. `Economy specification — approved foundation` - 8 edges
7. `Economy specification — approved foundation` - 8 edges
8. `from_environment()` - 6 edges
9. `Database` - 6 edges
10. `apply_fine()` - 6 edges

## Surprising Connections (you probably didn't know these)
- `test_fine_uses_cash_before_bank()` --calls--> `apply_fine()`  [INFERRED]
  tests/test_fines.py → src/discord_economy_bot/domain/fines.py
- `test_fine_stops_at_both_floors()` --calls--> `apply_fine()`  [INFERRED]
  tests/test_fines.py → src/discord_economy_bot/domain/fines.py
- `test_negative_fine_is_rejected()` --calls--> `apply_fine()`  [INFERRED]
  tests/test_fines.py → src/discord_economy_bot/domain/fines.py
- `test_no_roles_means_not_allowed()` --calls--> `combine_grants()`  [INFERRED]
  tests/test_permissions.py → src/discord_economy_bot/domain/permissions.py
- `EconomyBot` --uses--> `Settings`  [INFERRED]
  src/discord_economy_bot/bot.py → src/discord_economy_bot/config.py

## Communities (16 total, 6 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.29
Nodes (17): Base, Base, ActivityCooldown, ActivityRule, AuditEvent, CapabilityUsage, EconomyAccount, EconomyRolePermission (+9 more)

### Community 1 - "Community 1"
Cohesion: 0.11
Nodes (18): 1. Create the Discord application, 2. Prepare PostgreSQL, 3. Transfer the project, 4. Create the protected configuration, 5. Create the database tables, 6. Install and start the service, code:sql (CREATE ROLE discord_bot LOGIN;), code:bash (sudo apt update) (+10 more)

### Community 2 - "Community 2"
Cohesion: 0.13
Nodes (5): Database, EconomyBot, run_bot(), configure_logging(), main()

### Community 3 - "Community 3"
Cohesion: 0.27
Nodes (9): Capability, CapabilityGrant, combine_grants(), EffectiveGrant, Combine role grants, choosing the most permissive limit in each category., StrEnum, test_most_generous_limits_win(), test_no_roles_means_not_allowed() (+1 more)

### Community 4 - "Community 4"
Cohesion: 0.28
Nodes (6): MemberLifecycleCog, on_member_update(), on_raw_member_remove(), setup(), delete_member_economy(), reset_lost_role_cooldowns()

### Community 5 - "Community 5"
Cohesion: 0.22
Nodes (8): Cooldowns and collection, Currency and balances, Delegated capabilities, Economy specification — approved foundation, Fines, Lifecycle and auditing, Scope and ownership, Time and interest

### Community 6 - "Community 6"
Cohesion: 0.47
Nodes (8): _boolean(), ConfigurationError, from_environment(), _optional_snowflake(), Raised when required application configuration is missing or invalid., _required(), Settings, _snowflake_set()

### Community 7 - "Community 7"
Cohesion: 0.22
Nodes (8): Cooldowns and collection, Currency and balances, Delegated capabilities, Economy specification — approved foundation, Fines, Lifecycle and auditing, Scope and ownership, Time and interest

### Community 8 - "Community 8"
Cohesion: 0.31
Nodes (6): apply_fine(), FineResult, Apply a fine to cash first, then bank, without crossing either floor., test_fine_stops_at_both_floors(), test_fine_uses_cash_before_bank(), test_negative_fine_is_rejected()

### Community 10 - "Community 10"
Cohesion: 0.5
Nodes (3): run_async_migrations(), run_migrations_offline(), RuntimeError

## Knowledge Gaps
- **33 isolated node(s):** `Create the initial guild-scoped economy schema.  Revision ID: 0001_initial_econo`, `Raised when required application configuration is missing or invalid.`, `Discord economy bot package.`, `Discord command groups.`, `Database models and session management.` (+28 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `EconomyBot` connect `Community 2` to `Community 6`?**
  _High betweenness centrality (0.033) - this node is a cross-community bridge._
- **Why does `Settings` connect `Community 6` to `Community 2`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Why does `ConfigurationError` connect `Community 6` to `Community 10`?**
  _High betweenness centrality (0.017) - this node is a cross-community bridge._
- **Are the 14 inferred relationships involving `Base` (e.g. with `TimestampMixin` and `GuildSettings`) actually correct?**
  _`Base` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `EconomyBot` (e.g. with `Settings` and `Database`) actually correct?**
  _`EconomyBot` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Create the initial guild-scoped economy schema.  Revision ID: 0001_initial_econo`, `Raised when required application configuration is missing or invalid.`, `Discord economy bot package.` to the rest of the system?**
  _33 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.11 - nodes in this community are weakly interconnected._