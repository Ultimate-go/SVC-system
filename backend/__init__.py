"""后端：FastAPI + SQLite 的可验证分布式存储服务。

层次：

* :mod:`backend.manager` —— 把内存里的 ``VectorStore`` 与数据库缝起来
* :mod:`backend.models` —— 表定义（**只存公开量，绝不存 p/q/φ(N)**）
* :mod:`backend.routers` —— 路由；**验证不受限**（verify.py）、**解密受限**（files.py）
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.0"
