"""数据库连接与建表。

两个坑在这里一次性处理掉（规划 §2.3）：

1. **``check_same_thread=False``** —— FastAPI 把同步路由丢进线程池执行，
   而 SQLite 连接默认 ``check_same_thread=True``，跨线程用同一个连接会直接抛异常。
2. **WAL** —— 读写不互相阻塞，演示时前端轮询不会卡住写请求。

另外全局加一把**写锁**：SQLite 同一时刻只允许一个写事务，串行化掉最省心。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

__all__ = ["Base", "Database"]


class Base(DeclarativeBase):
    """所有表的基类。"""


# 这里曾有一个 ``WRITE_LOCK = threading.RLock()``，**从未被任何地方获取**
# （安全审计 I2）；而 ``__all__`` 里又导出了它 —— 读代码的人会以为
# 写操作已经被串行化了。声明了却不用，比不声明更危险，所以直接删掉。
#
# 本项目真正的并发模型是：
#   * **单进程单 worker**（``backend/manager.py`` 的模块说明里写死了这一点）；
#   * 所有会改**内存状态**的操作由 ``StoreManager._lock`` 串行化；
#   * 每个请求各开一个 SQLAlchemy ``Session``（各一个连接），所以**并发写**
#     仍可能撞 SQLITE_BUSY —— 那是 SQLite 的固有行为，由 ``_sqlite_pragmas``
#     里的 WAL + busy_timeout 兜着，**不是**靠这把锁。


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
            # ★ busy_timeout（安全审计 N5）：上面那段注释提到了它，但以前
            #   **从未设置** —— 注释与实现不一致，读代码的人会以为撞上
            #   SQLITE_BUSY 时有等待窗口。每个请求各开一个 Session（各一个
            #   连接），并发写仍可能撞上；给 5 秒等待窗口比立刻报
            #   “database is locked”合理得多。
            cur.execute("PRAGMA busy_timeout=5000")
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
    _ADDED_COLUMNS = {
        "files": {"version": "INTEGER NOT NULL DEFAULT 1"},
        # 管理员给某条审计流水写的备注。加它是为了能对一条记录做人工批注
        # （"这次解密被拒是演示用的，不是故障"），不必去改那条原始记录。
        "audit_log": {
            "remark": "TEXT NOT NULL DEFAULT ''",
            "remark_by": "VARCHAR(64) NOT NULL DEFAULT ''",
            "remark_at": "DATETIME",
        },
        # ★ 新方案："第 i 块配哪个素数"由**块身份**决定（``H(owner‖key‖i)``），
        #   不再查全局素数表。老库里的行默认 ``''`` = **老坐标**，
        #   而老坐标依然能跑 —— 所以旧库**不必清空**：老文件用老坐标、
        #   新文件用新坐标，两种坐标在同一份库里共存（合并时按各文件自己的
        #   素数收集）。这一列一旦丢了，重启后新文件会退回老坐标，
        #   而它的 ``U/C`` 是身份坐标算出来的 —— 验证必然不过。
        "file_deltas": {"identity": "TEXT NOT NULL DEFAULT ''"},
        "node_state": {"delta_identity": "TEXT NOT NULL DEFAULT ''"},
    }

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
