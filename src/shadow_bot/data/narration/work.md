# Narration for /work — shipped default, one line per entry.
#
# Format: one line per entry, grouped under [category.outcome] headers.
# Blank lines and lines starting with # are ignored. No quoting or escaping —
# apostrophes and quotes can be written normally.
#
# Placeholders are replaced where known and left visible where not, so a typo
# like {amout} shows up in the message instead of vanishing.
#
#   {user}      the member acting          {amount}  formatted money
#   {currency}  plural currency name
#
# A guild can override any section with /flavor add; its own lines then replace
# the defaults for that section only.

[work.success]
{user} pulled a double at the diner and made {amount}.
{user} sold hand-drawn portraits outside the station and earned {amount}.
{user} spent the afternoon fixing someone's fence for {amount}.
{user} walked eleven dogs at once and somehow came back with {amount}.
{user} covered a shift nobody wanted and took home {amount}.

[work.failure]
{user} slept through the alarm and was docked {amount}.
{user} put the decimal point in the wrong place. It cost {amount}.
{user} reversed the delivery van into the manager's car. {amount} gone.
