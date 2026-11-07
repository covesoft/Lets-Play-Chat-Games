# Dependency verification — foundation and first M1 increment

Verified November 6, 2026 against publisher metadata from PyPI and an actual
installation using **CPython 3.13.17 on Linux**. These are tested pins, not a claim
of universal compatibility or a dependency security audit. Python 3.13 is the
supported minor version; the patch release can differ on the owner’s machine.

| Runtime distribution | Pin | Purpose |
| --- | --- | --- |
| `discord.py` | 2.7.1 | Discord Gateway, interactions, and message metadata |
| `python-dotenv` | 1.2.4 | Explicit `.env` parsing; import is `dotenv` |
| `emoji` | 2.16.0 | Unicode emoji sequences; custom Discord emoji parsing remains project code |
| `aiosqlite` | 0.22.1 | Async access to the standard SQLite library |
| `better-profanity` | 0.7.0 | Configured-list matching helper; import is `better_profanity` |
| `markdown-it-py` | 4.2.0 | Token-based structure for the offline Discord-oriented parser |
| `regex` | 2026.11.7 | Unicode word sets, quotation properties, and grapheme clusters |

Python supplies `asyncio`, `sqlite3`, `logging`, `pathlib`, `dataclasses`, and the
other standard-library modules used here. Do not add those to requirements.
`discord.py` supplies `aiohttp` and the Python-3.13 `audioop-lts` compatibility
dependency transitively; the latter is not a request for voice-channel support.
Pillow, FFmpeg, PyNaCl, davey, a database ORM, and a migration framework are not
needed for M0. The first M1 parser increment adds `regex` for Unicode word/grapheme properties
and `markdown-it-py` for structured tokenization. Their pins were checked against
PyPI metadata and installed/tested on CPython 3.13.17. `mdurl` is a transitive
dependency; no renderer, HTTP parser service, or media downloader is introduced.

| Development/build tool | Pin | Purpose |
| --- | --- | --- |
| `pytest` | 9.2.0 | Offline test runner |
| `pytest-asyncio` | 1.4.1.post1 | Strict async tests |
| `ruff` | 0.17.4 | Lint and formatting |
| `build` | 1.6.1 | Isolated source/wheel build checks |
| `setuptools` | 84.2.1 | Pinned PEP 517 build backend |

The old `better-profanity` release has been smoke-tested on Python 3.13 with an
explicit harmless list. Its age and built-in matching/obfuscation behavior are
not authority for NR032. Before enabling that rule, verify the actual private
list, whole-word examples, and accepted parser policy. No library default is
silently treated as the project’s production list. Unicode sequence smoke tests
likewise do not complete every emoji validator or approve a full parser policy.

## Lock and update procedure

`pyproject.toml` owns direct pins and the dev group. `uv.lock` captures the full
resolution, with artifact hashes and supported-platform dependencies. Requirements
exports pin transitive packages with relevant platform markers; they deliberately
omit hashes for straightforward pip installation on Windows/Linux. Lock generation
used uv 0.13.5. uv is an optional developer tool, not a bot runtime dependency.

When deliberately upgrading:

```bash
# Edit the selected direct pins in pyproject.toml first.
uv lock
uv sync --locked
uv export --locked --no-dev --no-emit-project --no-hashes --output-file requirements.txt
uv export --locked --no-emit-project --no-hashes --output-file requirements-dev.txt
uv run --locked python -m pytest
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
uv run --locked python -m build
uv pip check
```

Commit the pyproject, lockfile, both exports, and updated evidence together.
Do not regenerate dependencies automatically at bot startup. The GitHub Actions
matrix checks pip-based installation on Windows and Linux after publication;
those results are distinct from the local Linux checks.

Publisher references:

* [discord.py](https://pypi.org/project/discord.py/2.7.1/)
* [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/)
* [emoji](https://pypi.org/project/emoji/2.16.0/)
* [aiosqlite](https://pypi.org/project/aiosqlite/0.22.1/)
* [better-profanity](https://pypi.org/project/better-profanity/0.7.0/)
* [discord.py Client API](https://discordpy.readthedocs.io/en/stable/api.html#discord.Client)
* [Discord Create Reaction](https://docs.discord.com/developers/resources/message#create-reaction)
* [SQLite WAL](https://www.sqlite.org/wal.html)
* [SQLite pragmas](https://www.sqlite.org/pragma.html)
