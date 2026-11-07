# Let’s Play — Chat Games

Let’s Play is a Discord chat-game bot. Its first game, **New Rules**, is a party
conversation game where unusual rules accumulate as people chat.

**Current status: v0.2.7, M1 opening and admission increment.** The application can validate its
configuration offline, initialize its SQLite database, and log in to Discord.
Offline domain contracts, text views, seventeen ordinary rules, and exact score
arithmetic are implemented. An offline SQLite unit of work now atomically applies
those candidates with event/reaction guards, score aggregates, and marker intents.
The ordinary subset also has bounded witness checks, legal replacements, frozen
three-template offers, and pure choice revalidation. A durable later-round flow
now records publication, fixed deadlines, selection and announced activation,
with ordered ingress and once-only closure boundaries. Schema 4 adds atomic
New Rules game credit, profiles, five general badge predicates, ordinary round
resolution and streak-notice intents. Schema 5 adds ordinary warning publication
clocks, immutable exact-score picker snapshots and persistent shuffled rotation.
Schema 6 adds atomic guild/channel lobby reservations, three-human quorum,
publication-started lobby/choice clocks, first activation and ordered handover.
Full held/revival/recovery, theme/mode achievements,
live commands and delivery workers remain unfinished.
The owner verified the earlier live shell, schema-5/WAL/FK bootstrap,
and v0.2.6 tests on Windows 11 (813 passed). This increment has local offline checks;
M1 remains in progress.
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
intents. Message caching and SDK member caching are disabled. Raw reaction
handling belongs to M2 and must work for uncached/old targets.

The parser API and its tested boundaries are documented in [docs/PARSER.md](docs/PARSER.md).
See [docs/EVALUATION.md](docs/EVALUATION.md) for candidate decisions and score values.
See [docs/HANDS.md](docs/HANDS.md) for the implemented ordinary subset and offer API.
See [docs/ORDINARY_FLOW.md](docs/ORDINARY_FLOW.md) for durable publication/cutover boundaries.
See [docs/PROGRESSION.md](docs/PROGRESSION.md) for New Rules credit and general resolution APIs.
See [docs/PACING.md](docs/PACING.md) for ordinary warning and succession APIs.
See [docs/OPENING.md](docs/OPENING.md) for admission, lobby and first-rule activation APIs.
The live shell does not yet invoke these offline evaluators.

The guild installation uses `bot` and `applications.commands` with permission
integer **274878024768**: View Channels, Send Messages, Send Messages in Threads,
Embed Links, Attach Files, Read Message History, and Add Reactions. The history
permission is required by Discord’s Create Reaction endpoint for ding markers.
The actual custom emoji and target-surface capabilities still need M2 checks;
the final emoji mechanism may require an additional operation-specific permission.

Command synchronization is off by default. Set `CHATGAMES_SYNC_COMMANDS=true`
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
| `lets_play/__main__.py` | Standard `python -m lets_play` entry point |
| `lets_play/config.py` | Explicit, immutable runtime configuration |
| `lets_play/cli.py`, `lets_play/bot.py` | Offline tools and Discord shell |
| `lets_play/new_rules/` | Contracts, parsing, ordinary subset, witness/offer preparation, and exact scoring |
| `lets_play/storage/` | WAL migrations, live-state codecs, and atomic event unit of work |
| `tests/` | Configuration, offline startup, SDK boundary, and real SQLite checks |
| `docs/` | Public configuration, dependency, and storage documentation |

Schema **3** adds durable ordinary offers, outputs, ordered receipt watermarks,
phrase usage and round-closure records to the schema-2 score foundation.
Existing schema-1/2 databases upgrade in place
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
