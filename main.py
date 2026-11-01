#!/usr/bin/env python3
"""Main executable entrypoint for kalshi-polymarket-hedger."""

import sys
from hedger.cli import main

if __name__ == "__main__":
    sys.exit(main())
