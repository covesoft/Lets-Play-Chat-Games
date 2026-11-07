# Let’s Play development

This repository implements the New Rules chat game in the Let’s Play application.
Read `docs/NEW_RULES_PROJECT_BIBLE.md` before changing game behavior; it contains
the current contracts, bounded milestone plan, and implementation evidence.
The accepted product baseline is v0.32. Follow its documented change protocol.

Use Python 3.13. `pyproject.toml` owns dependency pins; update `uv.lock` and both
requirements exports together. Keep the existing launcher compatible. No token
or external private list is required for offline tests.

Keep the New Rules domain independent of Discord and storage. Do not treat M0’s
empty domain package or migration bookkeeping as implemented gameplay. Finish M1
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
python discordbot_chatgames.py --check-config
```

For packaging/dependency changes, also run `python -m build` and `python -m pip
check` (or `uv pip check`). Record actual results and remaining limits in the
bible’s implementation ledger. Never report a mock/offline check as live Discord
or Windows evidence.
