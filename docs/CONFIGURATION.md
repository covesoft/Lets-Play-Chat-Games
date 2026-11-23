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
root. `--check-config` validates the configured gameplay list and phrase seed
without writing to the database.

| Environment variable | Meaning / default |
| --- | --- |
| `CHATGAMES_DISCORD_TOKEN` | Required only for live login; excluded from settings repr and diagnostics |
| `CHATGAMES_APP_ID` | Optional positive Discord ID; omit invite URL when absent |
| `CHATGAMES_ADMIN_IDS` | Comma-separated bot-owner IDs, deduplicated; empty means no configured owner. Invalid nonempty entries fail instead of being silently dropped |
| `CHATGAMES_DATABASE_PATH` | `data/lets_play.sqlite3`; must be on disk for WAL |
| `CHATGAMES_PROFANITY_LIST_PATH` | Optional private NR032 gameplay list |
| `CHATGAMES_GUILD_PHRASES_FILE_PATH` | Optional ordered Don’t Forget defaults for guilds without a saved bank |
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

With either support setting configured, `/support-server` replies privately with
a join button. The full URL takes precedence over the invite code. Enable global
command sync once after adding or removing the support configuration. The
offline `--check-config` command reports whether the support URL is configured.

Only the seven current install permissions are requested. No Administrator,
Manage Messages, member-list access, voice connection, or moderation permission
is required by M0. Missing Message Content access must be addressed before
enabling gameplay; no mention-only fallback can enforce the full channel game.

Use private external directories for production lists and backup files. Inside
the repository, `design/`, `private/`, `private_lists/`, `wordlists/`, `data/`, `backups/`,
`.env` variants, SQLite files/sidecars, and logs are ignored. Only `.env.example`
is tracked. Public tests use `turnip`, `rutabaga`, and other harmless fixtures;
never put production lists in tests, docs, exception messages, or build inputs.

All environment settings use the `CHATGAMES_` prefix, including
`CHATGAMES_ADMIN_IDS`. Module-level settings defaults in `lets_play/config.py`
use the same prefix. `Settings` fields remain normal lower-case Python attributes
such as `settings.token` and `settings.admin_ids`. No old environment aliases
are accepted. Keep private design/planning files out of Git and distributions;
they are not public documentation.

## Mind Your Language

Set `CHATGAMES_PROFANITY_LIST_PATH` to a private UTF-8 text file outside the
repository. Put one whole word or short phrase on each line. Blank lines and
duplicate normalized entries are ignored. The file must contain at least one
valid entry and fit within 256 KiB. Entries can contain up to 16 words and 256
characters. Use plain text without markup or comment lines. Run
`python -m lets_play --check-config` to confirm that it loads and see the count
of unique normalized entries.

The bot reads the list once at startup. Save edits and restart to use the new
list for future offers. The same normalized entries keep the same revision
when reordered or recased. A missing, unreadable, or invalid list withholds
the card from later hands. If a game has the card active or already announced
for the next round, recovery pauses that game until its matching list is
restored and the bot restarts. The host can still end a paused game. Only the
list revision is stored with cards. The configured path, entries and matched
words stay out of diagnostics and game records. See [PARSER.md](PARSER.md#mind-your-language)
for the matching rules.

## Don't Forget

Point `CHATGAMES_GUILD_PHRASES_FILE_PATH` to a private UTF-8 file with one phrase
per line, in draw order. Paths in `.env` resolve from the configuration root.
Use up to 15 entries, each one to five words and at most 80 characters. The file
size limit is 16 KiB. Blank lines are ignored. The loader rejects normalized
duplicates, markup, mentions, links and symbols outside plain punctuation.
If you configure `CHATGAMES_PHRASE_SUITABILITY_LIST_PATH`, the phrases must pass
that separate compulsory-phrase policy. The operator curates the defaults and
reviews their tone. `--check-config` reports the valid count or an error without
printing the configured path or phrase text.

The app reads the defaults at startup and seeds a guild's durable bank on its
first later-hand eligibility check. Editing the startup file and restarting
updates the seed for guilds that still lack a saved bank. The optional local
command `python -m lets_play --import-guild-phrases GUILD_ID` reads the same
`.env` setting and replaces that guild's ordered bank immediately. A valid
empty file explicitly clears the bank when imported. The import needs local
database access and does not connect to Discord. Invalid imports leave the saved
bank intact. Existing offers and active rules keep their exact phrases. Editing
the bank does not reset which wording has been used in the current game.

## Presentation

Player copy and colors are documented in [PRESENTATION.md](PRESENTATION.md).
The optional override is validated by `--check-config` and at bot construction;
relative paths are resolved against the selected configuration root. Actual owner
emoji assets/IDs are private and never supplied by a public default.
