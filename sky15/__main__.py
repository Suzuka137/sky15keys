"""让 `python -m sky15 ...` 能直接跑（等价于 `python -m sky15.cli ...`）。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
