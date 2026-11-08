# Configuration contract

`load_settings()` explicitly reads `.env` in the selected project root, then
overlays the process environment. Process values win, including a blank value
used to clear a file setting. It never mutates `os.environ`. Dotenv variable
interpolation is disabled; use literal values. In tests, passing `environ={...}`
supplies the entire configuration and skips `.env` reads.

Imports do not read `.env`, require a token, open a database, or connect to
Discord. The CLI explicitly loads settings from the current working directory;
application code accepts a `Settings` instance. Only live startup calls
`require_token()`. Run commands from the repository root so its `.env` is selected.

Blank optional values mean absent. There is no bundled reaction marker: configure
your own asset privately before starting a game. Relative paths resolve against the selected
root; production list files are not read by configuration checks. M0 does not
enable list-backed validators, phrase management, or badge rendering. Feature
code must validate each dependency before offering it to players.

| Environment variable | Meaning / default |
| --- | --- |
| `CHATGAMES_DISCORD_TOKEN` | Required only for live login; excluded from settings repr and diagnostics |
| `CHATGAMES_APP_ID` | Optional positive Discord ID; omit invite URL when absent |
| `CHATGAMES_ADMIN_IDS` | Comma-separated bot-owner IDs, deduplicated; empty means no configured owner. Invalid nonempty entries fail instead of being silently dropped |
| `CHATGAMES_DATABASE_PATH` | `data/lets_play.sqlite3`; must be on disk for WAL |
| `CHATGAMES_PROFANITY_LIST_PATH` | Optional private NR032 gameplay list |
| `CHATGAMES_PHRASE_SUITABILITY_LIST_PATH` | Separate optional private compulsory-phrase suitability input |
| `CHATGAMES_DING_EMOJI` | Private custom marker markup; no default. Missing/blank values prevent game admission; a real reaction probe must succeed |
| `CHATGAMES_PRESENTATION_PATH` | Optional private JSON copy/palette overrides; omitted uses packaged `lets_play/resources/en-US.json` |
| `CHATGAMES_BADGE_EMOJI_MAP_PATH` | Optional mapping file path; format/validation implemented with profiles |
| `CHATGAMES_DEVELOPMENT_MODE` | `false`; `true` enables the restricted ordinary-round preview and guild-name startup diagnostics |
| `CHATGAMES_SYNC_COMMANDS` | `false`; explicit global command-sync opt-in, with an empty-tree guard |
| `CHATGAMES_LINK_OTHER_BOTS` | Owner-supplied default: `https://pivot.quest/dbots/` |
| `CHATGAMES_LINK_THIS_BOT` | Owner-supplied default: `https://pivot.quest/dbots/#lets-play`; availability not certified here |
| `CHATGAMES_LINK_GITHUB` | `https://github.com/covesoft/Lets-Play-Chat-Games` |
| `CHATGAMES_LINK_DONATE` | Owner-supplied default: `https://ko-fi.com/lepivo/goal?g=8` |
| `CHATGAMES_LINK_SUPPORT` | Optional full HTTPS support URL |
| `CHATGAMES_SUPPORT_INVITE_CODE` | Invite code; used only when full support URL is absent |
| `CHATGAMES_LINK_VOTE` | Optional approved/available listing URL; no automatic URL generation |
| `CHATGAMES_PRIVACY_CONTACT_URL` | Optional actual privacy/contact page; required before public gameplay release |

Boolean settings accept `true`, `false`, `1`, or `0`, case-insensitively. IDs must
be positive ASCII integers below 2^64. Links must be HTTPS and cannot contain
embedded credentials or whitespace. Error messages identify the setting, not its
supplied value. Syntax validation does not prove a URL is live or an emoji usable.

Only the seven current install permissions are requested. No Administrator,
Manage Messages, member-list access, voice connection, or moderation permission
is required by M0. Missing Message Content access must be addressed before
enabling gameplay; no mention-only fallback can enforce the full channel game.

Use private external directories for production lists and backup files. Inside
the repository, `design/`, `private/`, `private_lists/`, `wordlists/`, `data/`, `backups/`,
`.env` variants, SQLite files/sidecars, and logs are ignored. Only `.env.example`
is tracked. Public tests use `turnip`, `rutabaga`, and other harmless fixtures;
never put production lists in tests, docs, exception messages, or build inputs.
The installed `better-profanity` distribution has its own default word list;
that list is not the approved gameplay policy. M1/M3 code must explicitly supply
the configured list rather than enable the library’s default singleton as policy.

All environment settings use the `CHATGAMES_` prefix, including
`CHATGAMES_ADMIN_IDS`. Module-level settings defaults in `lets_play/config.py`
use the same prefix. `Settings` fields remain normal lower-case Python attributes
such as `settings.token` and `settings.admin_ids`. No old environment aliases
are accepted. Keep private design/planning files out of Git and distributions;
they are not public documentation.

Player copy and colors are documented in [PRESENTATION.md](PRESENTATION.md).
The optional override is validated by `--check-config` and at bot construction;
relative paths are resolved against the selected configuration root. Actual owner
emoji assets/IDs are private and never supplied by a public default.
