"""节点自己的持久化 —— 每个节点一个独立 SQLite 文件。

用**标准库 sqlite3** 而不是 SQLAlchemy：节点是一个独立进程，它需要的东西
只有三张表、几条 SQL，把 ORM 拉进来反而多一层。协调者那边用 ORM 是因为它
有八张表和多对多关系，两边需求不一样。

存什么
------
* ``meta``  —— 公开参数 ``(N, g, l, n_max)`` 与自己的 ``node_id``
* ``state`` —— 一行：``(δ, st, I, F_I)``
* ``blobs`` —— 自己那几段密文

**不存什么**：``p``、``q``、``φ(N)`` —— 连它们存在过这件事都不该写进来。
节点只拿公开参数，它凭公开参数与更新密钥就能算出自己该有的状态。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

__all__ = ["NodeDB"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    delta_U   TEXT NOT NULL,
    delta_C   TEXT NOT NULL,
    delta_n   INTEGER NOT NULL,
    S_I       TEXT NOT NULL,
    Lambda_I  TEXT NOT NULL,
    I_json    TEXT NOT NULL,
    FI_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
    global_index INTEGER PRIMARY KEY,
    ciphertext   BLOB NOT NULL
);
"""


class NodeDB:
    """一个节点的本地库。所有方法都上锁 —— uvicorn 的同步路由跑在线程池里。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_meta(self, **kv: object) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, str(v)) for k, v in kv.items()],
            )

    def load_crs(self) -> dict | None:
        """读回公开参数；没有就返回 ``None``（还没被协调者告知）。"""
        keys = ("N", "g", "l", "n_max")
        vals = {k: self.get_meta(k) for k in keys}
        if any(v is None for v in vals.values()):
            return None
        return {
            "N": vals["N"],
            "g": vals["g"],
            "l": int(vals["l"]),
            "n_max": int(vals["n_max"]),
        }

    # -- 状态 ---------------------------------------------------------------

    def load_state(self) -> dict | None:
        """读回 ``(δ, st, I, F_I)``，没有则 ``None``。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT delta_U, delta_C, delta_n, S_I, Lambda_I, I_json, FI_json "
                "FROM state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        U, C, n, S_I, Lam, I_json, FI_json = row
        return {
            "delta": (U, C, int(n)),
            "st": (S_I, Lam),
            "I": json.loads(I_json),
            "FI": json.loads(FI_json),
        }

    def load_blobs(self) -> dict[int, bytes]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT global_index, ciphertext FROM blobs"
            ).fetchall()
        return {int(i): bytes(ct) for i, ct in rows}

    def save_state(self, *, delta, st, I, FI) -> None:
        """整行覆写 —— 状态只有一行，一次追加只写一次。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO state(id, delta_U, delta_C, delta_n, S_I, Lambda_I, "
                "                  I_json, FI_json) "
                "VALUES(1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  delta_U = excluded.delta_U, delta_C = excluded.delta_C, "
                "  delta_n = excluded.delta_n, S_I = excluded.S_I, "
                "  Lambda_I = excluded.Lambda_I, I_json = excluded.I_json, "
                "  FI_json = excluded.FI_json",
                (
                    str(delta.U),
                    str(delta.C),
                    int(delta.n),
                    str(st.S_I),
                    str(st.Lambda_I),
                    json.dumps(list(I)),
                    json.dumps(list(FI)),
                ),
            )

    def save_blobs(self, blobs: dict[int, bytes]) -> None:
        """**只写新增的** —— 每次上传都重写全部的话，n=1024 时就是 1024 行。"""
        if not blobs:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO blobs(global_index, ciphertext) VALUES(?, ?) "
                "ON CONFLICT(global_index) DO UPDATE SET "
                "  ciphertext = excluded.ciphertext",
                [(int(i), sqlite3.Binary(ct)) for i, ct in blobs.items()],
            )

    def delete_blobs(self, indices: Iterable[int]) -> int:
        """删掉若干下标的密文（交回 ``RmvStorage`` / 应用 ``del`` 之后必须真删）。

        :returns: 实际删掉的行数

        .. important::

           **必须真删**，不能只是"从 ``I`` 里去掉名字"。节点自称持有的下标集合
           与它实际存着的密文必须一致 —— :meth:`~core.node_state.NodeState.check`
           就查这一条，留一行孤儿密文会被它当场抓住（然后拒落库，
           表现为 500「删除后本地视图不合法」）。而且"删掉的数据还躺在磁盘上"
           这件事本身就是删除语义的反面。
        """
        want = [int(i) for i in indices]
        if not want:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "DELETE FROM blobs WHERE global_index = ?", [(i,) for i in want]
            )
        return int(cur.rowcount or 0)

    # -- 演示辅助 -----------------------------------------------------------

    def wipe(self) -> None:
        """清空（演示重置用）。"""
        with self._lock:
            self._conn.executescript(
                "DELETE FROM state; DELETE FROM blobs; DELETE FROM meta;"
            )

    def stats(self) -> dict:
        with self._lock:
            n_blobs = self._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            has_state = (
                self._conn.execute("SELECT COUNT(*) FROM state").fetchone()[0] > 0
            )
        return {"file": str(self.path), "blobs": int(n_blobs), "has_state": has_state}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
