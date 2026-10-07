"""节点自己的持久化 —— 每个节点一个独立 SQLite 文件。

用**标准库 sqlite3** 而不是 SQLAlchemy：节点是一个独立进程，它需要的东西
只有三张表、几条 SQL，把 ORM 拉进来反而多一层。协调者那边用 ORM 是因为它
有八张表和多对多关系，两边需求不一样。

存什么
------
* ``meta``  —— 公开参数 ``(N, g, l, n_max)`` 与自己的 ``node_id``
* ``state`` —— **每份参与的文件一行**：``(offset, δ, st, I, F_I)``
  —— 其中 δ 除 ``(U, C, n)`` 外**还存** ``identity`` 与 ``chunks``
  （见 :meth:`NodeDB._migrate`，那是“第 i 块配哪个素数”的前提，
  不存就会变成“重启后下一次更新才炸”）
* ``blobs`` —— 自己那几段密文，键是 ``(offset, 局部块号)``

★ 为什么是“每份文件一行”：新方案里**每份文件是一条独立向量**，一台节点
可以同时参与好几份（各有各的 δ 与位图）。而 ``I`` 里的下标是**文件内局部
块号**（``svc`` 层假定下标密集 ``0..n-1``），不同文件的同名块号指的是完全
不同的位置 —— 所以密文的键必须带上 ``offset``。

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
    offset      INTEGER PRIMARY KEY,
    delta_U     TEXT NOT NULL,
    delta_C     TEXT NOT NULL,
    delta_n     INTEGER NOT NULL,
    S_I         TEXT NOT NULL,
    Lambda_I    TEXT NOT NULL,
    I_json      TEXT NOT NULL,
    FI_json     TEXT NOT NULL,
    identity    TEXT NOT NULL DEFAULT '',
    chunks_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS blobs (
    offset      INTEGER NOT NULL,
    local_index INTEGER NOT NULL,
    ciphertext  BLOB NOT NULL,
    PRIMARY KEY (offset, local_index)
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
            self._migrate()

    def _migrate(self) -> None:
        """把老库补上后加的列 —— ``CREATE TABLE IF NOT EXISTS`` **不会**改已有表。

        ★ 为什么要这两列（而不是让 :meth:`NodeRuntime._reload` 自己凑）：
        ``identity`` 决定「第 ``i`` 块配哪个素数」，``chunks`` 决定
        「第 ``i`` 块是哪一段的哪一块」。两者都不在 ``(U, C, n)`` 里，
        于是从库里重建 δ 时**必须**有它们；否则节点重启一次就退回旧路径，
        之后每一次更新都会抬着旧素数算，以「S_I 校验失败」或
        「算出的摘要与协调者不一致」收场 —— 而那时协调者与库都是好的，
        排查方向会被完全带偏。

        默认值写成“老坐标”（``''`` / ``[]``）而不是硬报错：老库里的文件
        本来都是老坐标，补上空值恰好是对的。
        """
        have = {
            str(r[1]) for r in self._conn.execute("PRAGMA table_info(state)")
        }
        if "identity" not in have:
            self._conn.execute(
                "ALTER TABLE state ADD COLUMN identity TEXT NOT NULL DEFAULT ''"
            )
        if "chunks_json" not in have:
            self._conn.execute(
                "ALTER TABLE state ADD COLUMN chunks_json TEXT NOT NULL DEFAULT '[]'"
            )

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

    def load_states(self) -> dict[int, dict]:
        """读回**每份文件**的 ``(δ, st, I, F_I)``，按 ``offset`` 索引。

        ★ δ 重建需要**四个**字段加上「配哪个素数」的两个前提
        （``identity`` / ``chunks``，见 :meth:`_migrate`）—— 一声不响地
        少读一个，症状是「重启后一切正常，直到下一次更新才炸」。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT offset, delta_U, delta_C, delta_n, S_I, Lambda_I, "
                "       I_json, FI_json, identity, chunks_json FROM state"
            ).fetchall()
        out: dict[int, dict] = {}
        for off, U, C, n, S_I, Lam, I_json, FI_json, ident, chunks_json in rows:
            out[int(off)] = {
                "delta": (U, C, int(n)),
                "st": (S_I, Lam),
                "I": json.loads(I_json),
                "FI": json.loads(FI_json),
                "identity": ident or "",
                "chunks": [
                    (int(a), int(b)) for a, b in (json.loads(chunks_json or "[]"))
                ],
            }
        return out

    def load_blobs(self) -> dict[int, dict[int, bytes]]:
        """读回密文，按 ``offset`` 分组（键是**文件内局部块号**）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT offset, local_index, ciphertext FROM blobs"
            ).fetchall()
        out: dict[int, dict[int, bytes]] = {}
        for off, li, ct in rows:
            out.setdefault(int(off), {})[int(li)] = bytes(ct)
        return out

    def save_state(self, offset: int, *, delta, st, I, FI) -> None:
        """整行覆写 —— 每份文件一行，一次更新只写一份。

        ★ ``identity`` / ``chunks`` 一并写：它们是 δ 的“配哪个素数”那一半，
        与 ``(U, C, n)`` 同等重要（理由见 :meth:`_migrate`）。
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO state(offset, delta_U, delta_C, delta_n, S_I, Lambda_I, "
                "                  I_json, FI_json, identity, chunks_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(offset) DO UPDATE SET "
                "  delta_U = excluded.delta_U, delta_C = excluded.delta_C, "
                "  delta_n = excluded.delta_n, S_I = excluded.S_I, "
                "  Lambda_I = excluded.Lambda_I, I_json = excluded.I_json, "
                "  FI_json = excluded.FI_json, identity = excluded.identity, "
                "  chunks_json = excluded.chunks_json",
                (
                    int(offset),
                    str(delta.U),
                    str(delta.C),
                    int(delta.n),
                    str(st.S_I),
                    str(st.Lambda_I),
                    json.dumps(list(I)),
                    json.dumps(list(FI)),
                    str(getattr(delta, "identity", "") or ""),
                    json.dumps([
                        [int(a), int(b)] for a, b in getattr(delta, "chunks", ()) or ()
                    ]),
                ),
            )

    def delete_state(self, offset: int) -> int:
        """删掉**一段**的状态行 —— 这一份文件在本节点上彻底没了。

        ★ 什么时候会走到这里：协调者把整份文件删掉（``n = 0``）。那时节点侧的
          :meth:`~core.node_state.NodeState.apply_delete` 已经把这一段从内存里
          **整个摘掉**了，库这边必须跟着删行 —— 否则重启时 :meth:`load_states`
          会把一个空段读回来，节点与协调者对"现在有哪些段"的认识就分叉了
          （协调者那边本来就是**直接删行**的），启动自检会报
          「node-x 停在 [... (10, 0)]，协调者是 [...]」并**拒绝启动**。

        :returns: 删掉的状态行数（0 或 1）
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM state WHERE offset = ?", (int(offset),)
            )
        return int(cur.rowcount or 0)

    def save_blobs(self, offset: int, blobs: dict[int, bytes]) -> None:
        """**只写新增的** —— 每次都重写全部的话，n=1024 时就是 1024 行。"""
        if not blobs:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO blobs(offset, local_index, ciphertext) VALUES(?, ?, ?) "
                "ON CONFLICT(offset, local_index) DO UPDATE SET "
                "  ciphertext = excluded.ciphertext",
                [(int(offset), int(i), sqlite3.Binary(ct)) for i, ct in blobs.items()],
            )

    def delete_blobs(self, offset: int, indices: Iterable[int]) -> int:
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
                "DELETE FROM blobs WHERE offset = ? AND local_index = ?",
                [(int(offset), i) for i in want],
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
            n_vectors = self._conn.execute(
                "SELECT COUNT(*) FROM state"
            ).fetchone()[0]
        return {
            "file": str(self.path),
            "blobs": int(n_blobs),
            "vectors": int(n_vectors),
            "has_state": n_vectors > 0,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
