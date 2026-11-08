# Owner-editable copy and colors

Player-facing messages, embed titles/descriptions/fields/footers, button labels,
card display names/instructions and command descriptions live in
`lets_play/resources/en-US.json`. Edit that file for shared default changes. All
embed colors are in its `colors` object as `#RRGGBB` values, with entries for each
currently rendered phase. Internal card IDs, state names and control IDs remain
stable when labels are edited or translated.

For installation-specific copy, keep a JSON file outside public source (for
example, under ignored `private/`) and set this in your private `.env`:

```dotenv
CHATGAMES_PRESENTATION_PATH=private/presentation.json
```

An override can contain just the keys you want to change:

```json
{
  "locale": "en-US",
  "strings": {
    "lobby.description": "Come play New Rules! Three players are needed. Use /rules for the rules.",
    "buttons.play": "Join game",
    "offer.description": "<@{picker}>, choose our next rule."
  },
  "colors": {
    "lobby": "#4A698C",
    "results": "#647A51"
  }
}
```

Missing override keys fall back to the packaged English catalog. Unknown keys,
invalid JSON, duplicate keys, invalid colors, and changed/missing placeholders
are rejected before startup. Command descriptions and button labels have their
Discord length limits checked too. Use `python -m lets_play --check-config` to
validate the configured file without logging in or writing a database. Diagnostics
do not print private override contents or paths.

Copy is loaded once for the bot process. Restart to apply edits. Changing words
or colors does not change timing, quorum, scoring or other gameplay rules. Keep
instructions accurate. Existing messages are updated only through their normal
panel lifecycle; this feature does not rewrite channel history.

## Translation foundation and placeholders

Each string has a stable semantic key. A translator can reorder the placeholders
and surrounding text without changing application code. Preserve these names:

| Template | Required fields |
| --- | --- |
| `offer.description` | `{picker}` |
| `scheduled.description` | `{timestamp}` |
| `results.actual`, `results.ranking` | `{percent}` (numeric format specs such as `:.2f` are supported) |
| `results.player` | `{actor}` |
| `results.detail` | `{actual}`, `{ranking}`, `{faults}`, `{opportunities}`, `{ordinary_posts}`, `{reaction_failures}` |
| `results.footer` | `{page}` |

Other current strings take no fields. Ordinary literal braces must be doubled as
`{{` and `}}`. Attribute/index expressions, conversions and nested format fields
are refused. The application fills IDs, counts and timestamps; no chat transcript
or private asset is a template argument.

The `locale` tag identifies the catalog's language. This increment uses one
catalog for the bot process, with English fallback. It does not automatically
select languages per user/guild, implement plural rules, or register Discord
command-localization metadata. Command identifiers stay in the constants near
the top of `discord_adapter/discord_io.py`; sent copy can be translated separately.
Changing command descriptions requires another explicit command sync. Do not
translate control IDs, card IDs or stored state values.

## Private assets

Real owner emoji artwork, emoji names/markup and numeric asset IDs belong in
private configuration. There is no built-in marker. Supply your own custom emoji
via `CHATGAMES_DING_EMOJI`; admission still requires a successful reaction probe.
Never put real owner assets/IDs into public source, tests, examples or docs. Tests
use synthetic marker fixtures. Public language catalogs should contain readable
copy, not installation-specific assets. Badge mappings remain private too.

Operator CLI/log formatting is separate from player copy. Its version/banner
constants are near the top of `cli.py`, with the ready separator near the top of
`bot.py`. Tokens, emoji values and private file contents must never appear in
startup diagnostics.
