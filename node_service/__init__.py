"""存储节点服务。

一台服务器 = 一个独立进程 = 一个独立 SQLite 文件。对外只有五个接口，
而且**没有一个是"把状态设成这样"** —— 协调者只能告诉它"发生了什么"
（:math:`\\Delta` 与 :math:`\\Upsilon_\\Delta`）和"你负责哪些新位置"，
新状态由节点自己用 :func:`vds.updates.apply_update` 算出来。

跑法::

    python -m node_service --node-id node-1 --port 9101 --data-dir nodes/node-1

起步可以直接用 ``scripts/run_nodes.ps1`` 一次起四台。
"""

from __future__ import annotations

from .client import HttpTransport
from .persist import NodeDB
from .server import NODE_TOKEN_HEADER, NodeRuntime, create_node_app

__all__ = [
    "NODE_TOKEN_HEADER",
    "HttpTransport",
    "NodeDB",
    "NodeRuntime",
    "create_node_app",
    "__version__",
]

__version__ = "0.2.0"
