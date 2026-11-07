"""Check Git exclusions without creating real secrets or production data."""

import subprocess
from pathlib import Path


def test_git_ignores_runtime_secrets_and_lists_but_keeps_public_examples():
    root = Path(__file__).resolve().parents[1]
    private_paths = [
        ".env",
        ".env.production",
        "design/NEW_RULES_PROJECT_BIBLE.md",
        "private/words.txt",
        "private_lists/words.txt",
        "wordlists/words.txt",
        "data/game.sqlite3",
        "game.sqlite3-wal",
        "game.sqlite3-shm",
        "game.sqlite3-journal",
        "backups/snapshot.bak",
        "run.log",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(private_paths) + "\n",
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == set(private_paths)
    public = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input=".env.example\nlets_play/config.py\ntests/test_config.py\n",
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert public.returncode == 1
    assert public.stdout == ""
