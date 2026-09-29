"""数据库连接与建表。

两个坑在这里一次性处理掉（规划 §2.3）：

1. **``check_same_thread=False``** —— FastAPI 把同步路由丢进线程池执行，
   而 SQLite 连接默认 ``check_same_thread=True``，跨线程用同一个连接会直接抛异常。
2. **WAL** —— 读写不互相阻塞，演示时前端轮询不会卡住写请求。

另外全局加一把**写锁**：SQLite 同一时刻只允许一个写事务，串行化掉最省心。
"""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

__all__ = ["Base", "Database", "WRITE_LOCK"]


class Base(DeclarativeBase):
    """所有表的基类。"""


#: 全局写锁 —— SQLite 只允许一个写事务，串行化比死锁好。
WRITE_LOCK = threading.RLock()


class Database:
    """引擎 + 会话工厂的薄封装。"""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 依赖 sqlite3
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        # 让 models 先被导入，否则 Base.metadata 是空的
        from . import models  # noqa: F401

        Path(self.url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    #: 后来才加上的列：``表名 -> {列名: 建列子句}``。
    #:
    #: ``create_all`` 只建**缺的表**，不会给老表加列 —— 于是"代码升了、
    #: 库还是旧的"会以 ``no such column`` 的形式在运行期才炸出来，
    #: 而且看上去像业务 bug。这里补上，让那份已经在用的开发库不必被清空。
    #: 只处理**带默认值**的加列；真要改类型或约束就该上正经迁移工具了。
    _ADDED_COLUMNS = {"files": {"version": "INTEGER NOT NULL DEFAULT 1"}}

    def _add_missing_columns(self) -> None:
        with self.engine.begin() as conn:
            for table, cols in self._ADDED_COLUMNS.items():
                info = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                if not info:
                    continue  # 表还不存在（create_all 已经建好，下次迭代自然会补）
                have = {row[1] for row in info}
                for name, ddl in cols.items():
                    if name not in have:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                        )

    def drop_all(self) -> None:
        from . import models  # noqa: F401

        Base.metadata.drop_all(self.engine)

    def session(self) -> Session:
        return self.Session()
