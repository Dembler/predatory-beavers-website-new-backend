"""Refresh ASB and configured FIBA feeds independently, preserving last-good data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sports_refresh_runtime import main  # noqa: E402

if __name__ == "__main__":
    main()
