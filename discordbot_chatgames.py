"""Original Windows-friendly entry point, retained for existing installations."""

from pathlib import Path

from lets_play.cli import main

if __name__ == "__main__":
    raise SystemExit(main(base_dir=Path(__file__).resolve().parent))
