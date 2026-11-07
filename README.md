# Let’s Play — Chat Games

Let’s Play is a Discord chat-game bot. Its first game, **New Rules**, is a party
conversation game where unusual rules accumulate as people chat.

**Current status: v0.1.2, M0 foundation.** The application can validate its
configuration offline, initialize its SQLite database, and log in to Discord.
Game commands, scoring, cards, and achievements are not implemented yet.
The owner verified the earlier v0.1.1 login in one guild; the M0 revision has
offline verification, not a new live playtest.

The current specification and implementation ledger are in
[docs/NEW_RULES_PROJECT_BIBLE.md](docs/NEW_RULES_PROJECT_BIBLE.md). The accepted
product baseline is v0.32; the development update records M0 without changing
the game’s rules. The next bounded increment is M1.

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
point; running the existing launcher directly also works after installing the
requirements.

On Linux:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e . --no-deps
```

## Configure and run

Copy `.env.example` to `.env`; keep your existing `.env` if you already have one
and add any desired settings from the example. Existing `CHATRULES_*` names and
`ADMIN_IDS` are retained. Set `CHATRULES_DISCORD_TOKEN` only for live startup.
See the full [configuration contract](docs/CONFIGURATION.md).

With the virtual environment active, or substituting its Python executable:

```bash
python discordbot_chatgames.py --check-config
python discordbot_chatgames.py --init-db
python discordbot_chatgames.py
```

The first command reads configuration without connecting to Discord or creating
a database. The second creates/migrates the local database without needing a
token. The last logs in. `python -m lets_play` and `lets-play` accept the same
options when run from the project root. `--version` needs no configuration.
The original script finds `.env` relative to its own location even if launched
from another directory; module/console entry points use the current directory.

Enable **Message Content Intent** in the Discord Developer Portal for live game
development. The adapter requests guild, guild-message, guild-reaction, and
message-content intents; it does not request member, presence, or voice-state
intents. Message caching and SDK member caching are disabled. Raw reaction
handling belongs to M2 and must work for uncached/old targets.

The guild installation uses `bot` and `applications.commands` with permission
integer **274878024768**: View Channels, Send Messages, Send Messages in Threads,
Embed Links, Attach Files, Read Message History, and Add Reactions. The history
permission is required by Discord’s Create Reaction endpoint for ding markers.
The actual custom emoji and target-surface capabilities still need M2 checks;
the final emoji mechanism may require an additional operation-specific permission.

Command synchronization is off by default. Set `CHATRULES_SYNC_COMMANDS=true`
deliberately when integrating commands; the scaffold also refuses to sync an
empty tree. No `help` text command is exposed. Optional vote/support/privacy
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
| `discordbot_chatgames.py` | Existing launcher, retained |
| `config.py` | Compatibility aliases for the starter |
| `lets_play/config.py` | Explicit, immutable runtime configuration |
| `lets_play/cli.py`, `lets_play/bot.py` | Offline tools and Discord shell |
| `lets_play/new_rules/` | Domain boundary reserved for M1, with no Discord dependency |
| `lets_play/storage/` | WAL bootstrap and ordered migration infrastructure |
| `tests/` | Configuration, offline startup, SDK boundary, and real SQLite checks |
| `docs/` | Specification, migration policy, and evidence |

Schema **1** creates migration bookkeeping only. Game tables and transactional
gameplay repositories belong to M1. Read the
[migration and backup policy](docs/STORAGE.md) before changing persistence.

## Source hygiene and license

Keep tokens in `.env`/the process environment and production lists outside the
repository. Ignored runtime/private directories are described in the configuration
contract. `.gitignore` is a guard, not a substitute for reviewing staged files.
No transcript/media persistence or downloading is part of this scaffold.
The copied Sheep WAL test was replaced with project-specific WAL tests; the Sheep
database and fixtures are not dependencies.

The M0 license selection is **GNU AGPL version 3 only** (`AGPL-3.0-only`), following
the project’s stated AGPL direction. See [LICENSE](LICENSE). No additional
“or any later version” grant is made. Public project metadata and release
readiness should be reviewed separately from this foundation milestone.
