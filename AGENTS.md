# Let’s Play development

This repository implements the New Rules chat game in the Let’s Play application.
Owner-provided design notes may be available privately under `design/`. Consult
them before changing game behavior. Never commit, package, or copy their contents
into public documentation. The public `docs/` directory contains operational
documentation only.

Use Python 3.13. `pyproject.toml` owns dependency pins; update `uv.lock` and both
requirements exports together. Run the application with `python -m lets_play`
or the installed `lets-play` command. Use `CHATGAMES_*` configuration names; there
are no legacy launcher/configuration aliases. No token or external private list
is required for offline tests.

Keep the New Rules domain independent of Discord and storage. Do not treat the
offline parser/contracts or migration bookkeeping as implemented gameplay. Finish M1
interfaces, parser examples, and persistent transaction boundaries before wiring
playable commands. Resolve the relevant open contracts at their designated gates.

Never commit `.env`, production word lists, databases/sidecars, transcripts, or
media. Use harmless fixtures. Do not contact/invite collaborators or deploy the
bot without instructions. Do not run the live bot just to verify an offline edit.

Run meaningful checks for each change:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m lets_play --check-config
```

For packaging/dependency changes, also run `python -m build` and `python -m pip
check` (or `uv pip check`). Record actual results and remaining limits in the
private implementation notes when available. Never report a mock/offline check
as live Discord or Windows evidence. M1 is authorized in bounded increments.
The owner verified the earlier v0.1.3 shell live, schema-2 bootstrap, and v0.2.3
tests on Windows (655 passed). Source hygiene uses NUL-framed Git I/O.
Later increments still need their own Windows/CI evidence.

Schema 2 is an offline evaluation unit of work, not a transition/admission or
award service. Keep every guard, aggregate, and marker intent in one transaction;
ownership updates require monotonic revisions. Never interpret a candidate or
an uncommitted unit return value as an applied effect. Use bounded store/page
limits and surface overload/contention explicitly. No network waits inside a
write unit. Back up an existing database before schema upgrades.

The ordinary-subset hand API prepares immutable proposals with actual-validator
witnesses; READY does not activate, consume, publish, resolve a round, or commit.
Keep a single template ticket regardless of phrase/target variants. Slice the
authorized phrase window before filtering. Never repair a conflict by removing an
unrelated rule or use empty/whisper/media bypass as a witness. Bounds may withhold
unproven states. Unsupported cards/modes remain outside the subset until verified.

Schema 3 OrdinaryFlow is a later-round offline coordinator. Use one trusted,
contiguous server receipt sequence for every flow action, chat/reaction, and timer.
Never skip a sequence gap or backdate a timer over queued chat. Publication ACKs
are external facts, not output preparation. Old rules apply until the announced
whole-second cutover; missing/late ACK requests explicit recovery. Closures retain
pending award evidence; they are not resolved awards. Actual delivery, full startup
recovery, opening/admission, pacing, succession, and refresh/held remain unfinished.
