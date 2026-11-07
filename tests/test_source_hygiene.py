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
        "private/quoted name.txt",
        "private/naïve.txt",
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
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        # Binary NUL framing avoids Windows text-mode CRLF translation and Git's
        # C-style quoting of paths. No secrets/files need to exist for this test.
        input=b"\0".join(path.encode("utf-8") for path in private_paths) + b"\0",
        cwd=root,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert {path.decode("utf-8") for path in result.stdout.split(b"\0") if path} == set(
        private_paths
    )
    public = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        input=b".env.example\0lets_play/config.py\0tests/test_config.py\0",
        cwd=root,
        capture_output=True,
        timeout=10,
    )
    assert public.returncode == 1
    assert public.stdout == b""
