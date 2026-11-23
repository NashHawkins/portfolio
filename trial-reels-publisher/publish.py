#!/usr/bin/env python3
"""Stage 1 entry point:  python publish.py [--dry-run | --check-auth | --status | --reconcile]"""
import sys
import warnings
from pathlib import Path

# macOS system Python 3.9 is built with LibreSSL; urllib3 warns about it on import. TLS to
# graph.facebook.com still works. Install Python 3.11+ from python.org to make this moot.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from trial_reels.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
