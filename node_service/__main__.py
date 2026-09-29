"""``python -m node_service`` 的入口。"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
