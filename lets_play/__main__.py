"""Allow ``python -m lets_play`` alongside the original launcher."""

from lets_play.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
