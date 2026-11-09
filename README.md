# Let’s Play — Chat Games

Let’s Play is a Discord chat-game bot. Its first game, **New Rules**, is a party
conversation game where unusual rules accumulate as people chat.

**Current status: v0.3.14 — development-only ordinary-round preview (M2 in progress).**
M1 is accepted and closed at v0.2.9/schema 8. The Discord adapter connects a
persisted three-human opening, message/raw-reaction scoring, markers, timed
warnings, picker succession, three-card choices, replacements, and announced
round changes. Opening hands use five proven templates; later hands use sixteen
existing ordinary validators without a private phrase dependency. Held play and
three-person revival preserve the open round. Manager ending posts final results
ordered by exact adjusted score, highest first, with unrated players last;
inactivity ending only edits an existing panel.

A private Summary button on the rules disclosure panel and `/rules` shows paged
round/game scores, with scope/page buttons updating the same private menu.
Post summary to channel publishes a brief report; Check current scores opens
the live private menu. Repeat reports require 30 seconds and new non-whisper
chat. Delivered score copies are discarded; Discord history keeps the report.
Busy-round progress reports and compact round-end leaderboards are now wired.
Settled ordinary rounds recover automatically after reboot: a channel notice,
30-second read-in and scheduled continuation preserve the game and scores.
Interrupted warnings/choices, unknown-send reconciliation, modes, settings,
complete retry/retention cleanup and public-release checks remain unfinished.
Restart and uncertain publication enter a durable pause; a manager can close the
saved game. See [the preview runbook](docs/DISCORD_PREVIEW.md) for setup and live checks.
Offline checks do not certify a human Discord session or Windows execution.
Public setup and operational documentation is under `docs/`. Private design
notes are excluded from Git and package distributions.

## Install on Windows

Use **CPython 3.13** and run these commands from the repository root in VS Code’s
terminal. An existing clone/folder named `Chat_Rules` is fine.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pip install -e . --no-deps
```

For a runtime-only installation, use `requirements.txt` instead of
`requirements-dev.txt`. The editable install adds the `lets-play` console entry
point. `python -m lets_play` is the standard package entry point.

On Linux:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e . --no-deps
```

## Configure and run

Copy `.env.example` to `.env`, or transfer your existing values into its
`CHATGAMES_*` settings. All environment settings use that prefix, including
`CHATGAMES_ADMIN_IDS`. Set `CHATGAMES_DISCORD_TOKEN` only for live startup.
See the full [configuration contract](docs/CONFIGURATION.md).
Edit sent copy/colors in `lets_play/resources/en-US.json`, or configure private
overrides; see [presentation and translation groundwork](docs/PRESENTATION.md).
Real emoji assets/IDs are private configuration, with no bundled marker.

With the virtual environment active, or substituting its Python executable:

```bash
python -m lets_play --check-config
python -m lets_play --init-db
python -m lets_play
```

The first command reads configuration without connecting to Discord or creating
a database. The second creates/migrates the local database without needing a
token. The last logs in and also initializes the database during startup if
needed. The installed `lets-play` command accepts the same options.
Run from the repository root: both entry points select `.env` and relative paths
from the current working directory. `--version` needs no configuration.

Enable **Message Content Intent** in the Discord Developer Portal for live game
development. The adapter requests guild, guild-message, guild-reaction, and
message-content intents; it does not request member, presence, or voice-state
intents. Message caching and SDK member caching are disabled. Raw reactions use the uncached Gateway payload; no target history is fetched.

The parser API and its tested boundaries are documented in [docs/PARSER.md](docs/PARSER.md).
See [docs/EVALUATION.md](docs/EVALUATION.md) for candidate decisions and score values.
See [docs/HANDS.md](docs/HANDS.md) for the implemented ordinary subset and offer API.
See [docs/ORDINARY_FLOW.md](docs/ORDINARY_FLOW.md) for durable publication/cutover boundaries.
See [docs/PROGRESSION.md](docs/PROGRESSION.md) for New Rules credit and general resolution APIs.
See [docs/PACING.md](docs/PACING.md) for ordinary warning and succession APIs.
See [docs/OPENING.md](docs/OPENING.md) for admission, lobby and first-rule activation APIs.
The development ordinary-round preview invokes the existing transactional evaluators.

The guild installation uses `bot` and `applications.commands` with permission
integer **274878024768**: View Channels, Send Messages, Send Messages in Threads,
Embed Links, Attach Files, Read Message History, and Add Reactions. The history
permission is required by Discord’s Create Reaction endpoint for ding markers.
The actual custom emoji and target-surface capabilities still need M2 checks;
the final emoji mechanism may require an additional operation-specific permission.

Command synchronization is off by default. Set `CHATGAMES_SYNC_COMMANDS=true`
for the first registration of `/start-game new-rules`, `/rules`, and `/end-game`. No `help` text command is exposed. Optional vote/support/privacy
links are absent until configured; a Top.gg URL is never invented from an ID.

The `davey`/voice-library warning is harmless for this scope. Playing Telephone
uses message flags and duration metadata, not a voice-channel connection or
audio decoding. Do not install `discord.py[voice]`, Pillow, or a separate
`sqlite3` package for this project.

## Verify offline

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
python -m pip check
```

Tests need no Discord token, private word list, external service, or real-time
gameplay delays. They use temporary databases and harmless text fixtures.
GitHub Actions is configured for Python 3.13 on Ubuntu and Windows; its actual
results must be checked after pushing. Local Linux results do not certify Windows.

## Dependencies and layout

`pyproject.toml` is the source for direct pins and test tooling. `uv.lock`
captures resolution, and the generated requirements files include transitive
pins and platform markers for Python 3.13. See
[dependency verification](docs/DEPENDENCIES.md) for versions and reproducible
update commands.

| Path | Responsibility |
| --- | --- |
| `lets_play/__main__.py` | Standard `python -m lets_play` entry point |
| `lets_play/config.py` | Explicit, immutable runtime configuration |
| `lets_play/cli.py`, `lets_play/bot.py` | Offline tools and Discord shell |
| `lets_play/new_rules/` | Contracts, parsing, ordinary subset, witness/offer preparation, and exact scoring |
| `lets_play/storage/` | WAL migrations, live-state codecs, and atomic event unit of work |
| `tests/` | Configuration, offline startup, SDK boundary, and real SQLite checks |
| `docs/` | Public configuration, dependency, and storage documentation |

Schema **12** adds round-report delivery receipts and round-local reporting watermarks.
Existing clocks, scores and delivery claims are preserved. The Discord preview
continues to use the v0.3.3 ordinary loop, with v0.3.9 live private score
pages, quiet requested reports, gated interim updates and compact round leaderboards.
See [round reporting](docs/ROUND_REPORTS.md) for policy and upgrade checks.
See [the summary API](docs/SUMMARY_FOUNDATION.md).
Existing recognized databases upgrade in place
on `--init-db` or startup. Stop the bot and make a verified SQLite backup before
upgrading. Read the [database API and backup policy](docs/STORAGE.md).

## Source hygiene and license

Keep tokens in `.env`/the process environment and production lists outside the
repository. Ignored runtime/private directories are described in the configuration
contract. `.gitignore` is a guard, not a substitute for reviewing staged files.
No transcript/media persistence or downloading is part of this scaffold.
`design/` is private: it is ignored by Git and explicitly pruned from builds.
Public distributions contain operational docs, not private development plans.

The M0 license selection is **GNU AGPL version 3 only** (`AGPL-3.0-only`), following
the project’s stated AGPL direction. See [LICENSE](LICENSE). No additional
“or any later version” grant is made. Public project metadata and release
readiness should be reviewed separately from this foundation milestone.
