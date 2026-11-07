# Configuration contract

`load_settings()` explicitly reads `.env` in the selected project root, then
overlays the process environment. Process values win, including a blank value
used to clear a file setting. It never mutates `os.environ`. Dotenv variable
interpolation is disabled; use literal values. In tests, passing `environ={...}`
supplies the entire configuration and skips `.env` reads.

Imports do not require a token. Only live startup calls `require_token()`. The
compatibility `config.py` loads settings at import time but performs no login or
database I/O. New application code accepts a `Settings` instance directly.

Blank optional values mean absent. Relative paths resolve against the selected
root; production list files are not read by configuration checks. M0 does not
enable list-backed validators, phrase management, or badge rendering. Feature
code must validate each dependency before offering it, as required by the bible.

| Environment variable | Meaning / default |
| --- | --- |
| `CHATRULES_DISCORD_TOKEN` | Required only for live login; excluded from settings repr and diagnostics |
| `CHATRULES_APP_ID` | Optional positive Discord ID; omit invite URL when absent |
| `ADMIN_IDS` | Comma-separated bot-owner IDs, deduplicated; empty means no configured owner. Invalid nonempty entries fail instead of being silently dropped |
| `CHATRULES_DATABASE_PATH` | `data/lets_play.sqlite3`; must be on disk for WAL |
| `CHATRULES_PROFANITY_LIST_PATH` | Optional private NR032 gameplay list |
| `CHATRULES_PHRASE_SUITABILITY_LIST_PATH` | Separate optional private compulsory-phrase suitability input |
| `CHATRULES_DING_EMOJI` | Optional custom marker string; actual usability checked at M2 |
| `CHATRULES_BADGE_EMOJI_MAP_PATH` | Optional mapping file path; format/validation implemented with profiles |
| `CHATRULES_DEVELOPMENT_MODE` | `false`; enable guild-name startup diagnostics with `true` |
| `CHATRULES_SYNC_COMMANDS` | `false`; explicit global command-sync opt-in, with an empty-tree guard |
| `CHATRULES_LINK_OTHER_BOTS` | Owner-supplied default: `https://pivot.quest/dbots/` |
| `CHATRULES_LINK_THIS_BOT` | Owner-supplied default: `https://pivot.quest/dbots/#lets-play`; availability not certified here |
| `CHATRULES_LINK_GITHUB` | `https://github.com/covesoft/Lets-Play-Chat-Games` |
| `CHATRULES_LINK_DONATE` | Owner-supplied default: `https://ko-fi.com/lepivo/goal?g=8` |
| `CHATRULES_LINK_SUPPORT` | Optional full HTTPS support URL |
| `CHATRULES_SUPPORT_INVITE_CODE` | Existing invite-code setting; used only when full support URL is absent |
| `CHATRULES_LINK_VOTE` | Optional approved/available listing URL; no automatic URL generation |
| `CHATRULES_PRIVACY_CONTACT_URL` | Optional actual privacy/contact page; required before public gameplay release |

Boolean settings accept `true`, `false`, `1`, or `0`, case-insensitively. IDs must
be positive ASCII integers below 2^64. Links must be HTTPS and cannot contain
embedded credentials or whitespace. Error messages identify the setting, not its
supplied value. Syntax validation does not prove a URL is live or an emoji usable.

Only the seven current install permissions are requested. No Administrator,
Manage Messages, member-list access, voice connection, or moderation permission
is required by M0. Missing Message Content access must be addressed before
enabling gameplay; no mention-only fallback can enforce the full channel game.

Use private external directories for production lists and backup files. Inside
the repository, `private/`, `private_lists/`, `wordlists/`, `data/`, `backups/`,
`.env` variants, SQLite files/sidecars, and logs are ignored. Only `.env.example`
is tracked. Public tests use `turnip`, `rutabaga`, and other harmless fixtures;
never put production lists in tests, docs, exception messages, or build inputs.
The installed `better-profanity` distribution has its own default word list;
that list is not the approved gameplay policy. M1/M3 code must explicitly supply
the configured list rather than enable the library’s default singleton as policy.
