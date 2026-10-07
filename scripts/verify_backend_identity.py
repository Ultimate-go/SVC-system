"""验证 **身份坐标能跨重启存活** —— 后端落库 / 重建这条路上不能丢 identity。

这是"去掉全局下标"改造的第 4 关（继 svc 内核、会话层、store 业务流之后）。
前三关都在**进程内**，而身份一旦落库漏了字段，症状是
「重启后这份文件忽然验不过」——离线跑单测永远发现不了。

所以这里做三件事：

1. **建一个真库**（走 ``Settings`` + ``Database`` + ``StoreManager``，与会话里
   跑的是同一条路），上传两份文件，确认身份落进 ``file_deltas.identity``、
   节点侧落进 ``node_state.delta_identity``；
2. **模拟重启** —— 丢掉全部内存对象，只留那个 ``.db`` 文件，重新
   ``bootstrap()``，确认重建出来的 δ 仍带身份、``check()`` 仍然全绿；
3. **重启之后继续写** —— 改块 / 追加 / 跨文件检索，全部要通。
   （这一条才是真正的把关：光"读得出来"不够，更新路径还依赖
   节点那边算出的 ``e_i`` 与协调者一致。）

.. note::

   库建在 ``scripts/`` 里、用完即删，**不碰**项目根那份 ``vds.db``。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ★ 必须在 import Settings 之前设好：db_path 的 default_factory 会读它。
DB_FILE = ROOT / "scripts" / "_tmp_identity_restart.db"
os.environ["VDS_DB_PATH"] = str(DB_FILE)
os.environ.setdefault("VDS_SECRET_KEY", "identity-restart-test-key")

from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.manager import StoreManager  # noqa: E402
from backend.models import FileDeltaRow, NodeStateRow, UserRow  # noqa: E402
from backend.security import hash_password  # noqa: E402
from vds.digest import make_identity  # noqa: E402
from sqlalchemy import select  # noqa: E402

FAILURES: list[str] = []
PASSWORD = "vds12345"
OWNER = "zhangsan"
SEG = 256
BLOCK = b"A" * SEG


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def read_bytes(mgr, owner: str, file_key: str, sk: int) -> bytes:
    """``read_plain`` 回的是 ``data_hex``（不是裸 bytes），这里转一下。"""
    got = mgr.read_plain(owner, file_key, sk)
    return bytes.fromhex(got["data_hex"])


def ok_verify(res: dict) -> bool:
    """``manager.query`` / ``cipher_pack`` 的 ``verify`` 子字典是否通过。"""
    v = res.get("verify") or {}
    return bool(v.get("ok"))


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def fresh_manager() -> tuple[Settings, Database, StoreManager]:
    """从**只存在于磁盘上的那个库**建一套全新的对象 —— 这就是"重启"。"""
    settings = Settings()
    settings.db_path = DB_FILE
    db = Database(settings.database_url())
    manager = StoreManager(settings, db)
    manager.bootstrap()
    return settings, db, manager


# ---------------------------------------------------------------------------
section("0. 建库 + 建账号 + 生成 SM2 密钥")

if DB_FILE.exists():
    DB_FILE.unlink()
for suffix in ("-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    if p.exists():
        p.unlink()

settings, db, mgr = fresh_manager()
db1 = db  # 只为了最后能 dispose 引擎（否则 Windows 上删不掉临时库）
print(f"        n_max = {settings.n_max}, 块大小范围 "
      f"{settings.segment_bytes_min}~{settings.segment_bytes_max}")
with db.session() as s:
    s.add(UserRow(username=OWNER, pwd_hash=hash_password(PASSWORD),
                  role="user", display_name="张三"))
    s.commit()
check("生成用户密钥对", mgr.ensure_user_key(OWNER, PASSWORD))


section("1. 上传：身份必须写进库")

PLAIN_A = BLOCK * 4
PLAIN_B = b"B" * SEG * 3
mgr.upload(OWNER, "f1", PLAIN_A, segment_bytes=SEG)
mgr.upload(OWNER, "f2", PLAIN_B, segment_bytes=SEG)

store = mgr._require()
check("f1 内存里的 δ 带身份",
      store.delta_of(OWNER, "f1").identity == make_identity(OWNER, "f1"))
check("f2 内存里的 δ 带身份",
      store.delta_of(OWNER, "f2").identity == make_identity(OWNER, "f2"))
check("f1 的块数正确（4 块）", store.delta_of(OWNER, "f1").n == 4)
check("f2 的块数正确（3 块）", store.delta_of(OWNER, "f2").n == 3)

with db.session() as s:
    rows = {r.file_key: r for r in s.execute(select(FileDeltaRow)).scalars()}
check("★ file_deltas 里落了 identity",
      rows["f1"].identity == make_identity(OWNER, "f1")
      and rows["f2"].identity == make_identity(OWNER, "f2"),
      f"f1={rows['f1'].identity!r}")
with db.session() as s:
    nrows = list(s.execute(select(NodeStateRow)).scalars())
check("★ node_state 里落了 delta_identity",
      nrows and all(r.delta_identity for r in nrows),
      f"{len(nrows)} 行，样例={nrows[0].delta_identity!r}" if nrows else "无行")

sk = mgr.unseal_user_key(OWNER, PASSWORD)
check("上传后能解密 f1", read_bytes(mgr, OWNER, "f1", sk) == PLAIN_A)
check("上传后 check() 全绿", mgr.check() == [], str(mgr.check()[:2]))


section("2. 模拟重启：丢掉全部内存对象，只留那个 .db 文件")

del store, mgr
db1.engine.dispose()  # 真的把第一个管理器放干净（连接、写锁都释放）
settings2, db2, mgr2 = fresh_manager()
store2 = mgr2._require()

d1 = store2.delta_of(OWNER, "f1")
d2 = store2.delta_of(OWNER, "f2")
check("★ 重启后 f1 的 δ 仍带身份", d1.identity == make_identity(OWNER, "f1"),
      repr(d1.identity))
check("★ 重启后 f2 的 δ 仍带身份", d2.identity == make_identity(OWNER, "f2"),
      repr(d2.identity))
check("重启后 n 复原", (d1.n, d2.n) == (4, 3), f"{(d1.n, d2.n)}")
check("重启后位置段复原", len(d1.positions) == 4 and len(d2.positions) == 3)
check("★ 重启后 check() 全绿（节点与协调者对齐）",
      mgr2.check() == [], str(mgr2.check()[:3]))

sk2 = mgr2.unseal_user_key(OWNER, PASSWORD)
check("★ 重启后仍能解密 f1", read_bytes(mgr2, OWNER, "f1", sk2) == PLAIN_A)
check("★ 重启后仍能解密 f2", read_bytes(mgr2, OWNER, "f2", sk2) == PLAIN_B)


section("3. 重启之后继续写：更新路径也依赖正确坐标")

mgr2.modify_block(OWNER, "f1", 1, b"C" * SEG)
check("重启后改块：身份未丢",
      store2.delta_of(OWNER, "f1").identity == make_identity(OWNER, "f1"))
expect = BLOCK + b"C" * SEG + BLOCK + BLOCK
check("重启后改块：明文正确", read_bytes(mgr2, OWNER, "f1", sk2) == expect)
check("重启后改块：check() 全绿", mgr2.check() == [], str(mgr2.check()[:2]))

mgr2.append_to_file(OWNER, "f1", b"D" * SEG)
check("重启后追加：n = 5", store2.delta_of(OWNER, "f1").n == 5)
check("重启后追加：身份未丢",
      store2.delta_of(OWNER, "f1").identity == make_identity(OWNER, "f1"))
check("重启后追加：明文正确",
      read_bytes(mgr2, OWNER, "f1", sk2) == expect + b"D" * SEG)

pos1 = store2.registry.positions_of(OWNER, "f1")
pos2 = store2.registry.positions_of(OWNER, "f2")
qr = mgr2.query(list(pos1[:2]) + list(pos2[:2]))
check("★ 重启后跨文件检索 + 聚合验证通过", ok_verify(qr), str(qr.get("verify")))
check("重启后 check() 仍全绿", mgr2.check() == [], str(mgr2.check()[:2]))

with db2.session() as s:
    r = s.execute(select(FileDeltaRow)).scalars()
    still = {x.file_key: x.identity for x in r}
check("★ 更新之后库里仍是身份坐标", still["f1"] == make_identity(OWNER, "f1"),
      repr(still.get("f1")))


section("4. 兼容性：老库（identity 为空）仍走老坐标")

from core.session import new_session  # noqa: E402
from vds.digest import Digest  # noqa: E402

legacy_sess = new_session(l=256, n_max=8, modulus_bits=1024, seed=b"legacy")
old_delta = Digest(U=1, C=1, n=3, offset=0)  # identity 默认为空
check("老摘要的 identity 是空串", old_delta.identity == "")
cn_legacy = legacy_sess.crs_n_for(old_delta)
check("★ 老坐标下 crs_n_for 仍然可用（查全局素数表）",
      cn_legacy.crs.primegen.first(3) == legacy_sess.crs.primegen.first(3))
cn_new = legacy_sess.crs_n_for(
    Digest(U=1, C=1, n=3, offset=0, identity=make_identity("a", "b"))
)
check("★ 同一份公开参数可同时服务两种坐标（素数不同）",
      cn_new.crs.primegen.first(3) != cn_legacy.crs.primegen.first(3))


# ---------------------------------------------------------------------------
print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项未通过")
    for f in FAILURES:
        print("   FAIL " + f)
else:
    print("结果：ALL PASS —— 身份坐标跨重启存活，且老库仍走老坐标")
print("=" * 72)

# 清理：临时库不留在工作区里。
# ★ 必须先 dispose 引擎 —— SQLAlchemy 还握着文件句柄，不放开就删不掉
#   （Windows 上会报 WinError 32）。这不是"不影响结论"的小事：
#   临时库留在工作区里会被后续的 robocopy 同步到镜像仓库去。
try:
    db2.engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_FILE) + suffix)
        if p.exists():
            p.unlink()
    print("[cleanup] 已删除临时库")
except OSError as exc:  # pragma: no cover
    print(f"[cleanup] 删临时库失败（不影响结论）：{exc}")

sys.exit(1 if FAILURES else 0)
