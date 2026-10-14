"""验证「版本本体留存 + 回退到上一版」—— ★ 2026-10-12 用户要的条目 6。

要证明的四件事（按用户的话）：

1. **留的是本体，不是哈希** —— 改块之前把这一块那套本体（密文 + IV + 分量 +
   块密钥密文 + 长度）整套抄下来；抄下来的密文**就是**当时那一份（逐字节比对）。
   只留哈希的话「验证得对、原文找不回」，那不算数。
2. **只留两版** —— 当前 + 上一版；每块的历史行只有一行，新的覆盖旧的。
3. **回退 = 内容真的回到上一版，并且重新承诺** —— 回退后：解出的明文与改块前
   逐字节相同、分量回到原值、公开承诺 ``C`` 回到上一版那个值、``check()`` 全绿、
   公开验证仍然通过。
4. **只增不减 + 可来回** —— 回退产生**新版本号**（不是把版本号改回去），
   而且会把「被换下来的那一版」留成新的上一版 —— 所以再回退一次能回到改块那版。

另外两条边界：没留过上一版的块不能回退；回退只碰点名的那些块。

.. note::

   库建在 ``scripts/`` 里、用完即删，**不碰**项目根那份 ``vds.db``。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ★ 必须在 import Settings 之前设好：db_path 的 default_factory 会读它。
# ★ 落**系统临时目录**，不在仓库里留脏文件。
DB_FILE = Path(tempfile.gettempdir()) / "vds_version_restore.db"
os.environ["VDS_DB_PATH"] = str(DB_FILE)
os.environ.setdefault("VDS_SECRET_KEY", "version-restore-test-key")

from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.manager import Conflict, NotFound, OutOfRange, StoreManager  # noqa: E402
from backend.models import BlockHistoryRow, UserRow  # noqa: E402
from backend.security import hash_password  # noqa: E402
from sqlalchemy import select  # noqa: E402

FAILURES: list[str] = []
PASSWORD = "vds12345"
OWNER = "zhangsan"
SEG = 256
PLAIN = bytes(range(256)) * 4  # 4 块 × 256 B
NEW0 = b"Z" * SEG  # 改块 0 之后的内容


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def fresh_manager() -> tuple[Settings, Database, StoreManager]:
    """从**只存在于磁盘上的那个库**建一套全新对象 —— 这就是"重启"。"""
    settings = Settings()
    settings.db_path = DB_FILE
    db = Database(settings.database_url())
    manager = StoreManager(settings, db)
    manager.bootstrap()
    return settings, db, manager


def read_bytes(mgr: StoreManager, sk: int) -> bytes:
    return bytes.fromhex(mgr.read_plain(OWNER, "f1", sk)["data_hex"])


def public_verify_ok(mgr: StoreManager) -> bool:
    """**公开验证**那条路（不需要任何密钥）—— 这才是给外人看的那条。"""
    res = mgr.query_files([(OWNER, "f1")], [0, 1])
    return bool((res.get("verify") or {}).get("ok"))


def raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def has_history_rows(db_: Database) -> bool:
    with db_.session() as s:
        return s.execute(select(BlockHistoryRow)).scalars().first() is not None


section("0. 建临时库 + 账号 + 密钥对 + 上传 4 块")

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    if p.exists():
        p.unlink()

settings, db, mgr = fresh_manager()
with db.session() as s:
    s.add(
        UserRow(
            username=OWNER,
            pwd_hash=hash_password(PASSWORD),
            role="user",
            display_name="张三",
        )
    )
    s.commit()
check("生成用户密钥对", mgr.ensure_user_key(OWNER, PASSWORD))
mgr.upload(OWNER, "f1", PLAIN, segment_bytes=SEG)
store = mgr._require()
sk = mgr.unseal_user_key(OWNER, PASSWORD)

indices = store.file_indices(OWNER, "f1")
elems0 = [store.values[g] for g in indices]
ct0 = [
    store.cipher_of(OWNER, "f1", [i])["blocks"][0]["ciphertext_hex"]
    for i in range(len(indices))
]
dg0 = store.delta_of(OWNER, "f1")
C0 = dg0.C
check("上传后 4 块", len(indices) == 4, str(len(indices)))
check("上传后解得出原文", read_bytes(mgr, sk) == PLAIN)
check("上传后公开验证通过", public_verify_ok(mgr))
check("上传后 check() 全绿", mgr.check() == [], str(mgr.check()[:2]))


section("1. 还没改写时：没有上一版，回退要被拒（不能假装有）")

check("history 为空", mgr.history_of(OWNER, "f1") == [], str(mgr.history_of(OWNER, "f1")))
check(
    "此时回退被拒（Conflict）",
    raises(Conflict, mgr.restore_previous, OWNER, "f1", None),
)
check("库里一行历史都没有", not has_history_rows(db))


section("2. 改块：改写前的那一版本体必须被留成上一版")

out = mgr.modify_block(OWNER, "f1", 0, NEW0)
check("改块后版本 = 2", out["version"] == 2, str(out["version"]))
hist = mgr.history_of(OWNER, "f1")
check("只留了被改写那一块", [h["block_idx"] for h in hist] == [0], str(hist))
check("留的版本号是「改写前那一版」（1）", hist and hist[0]["version"] == 1, str(hist))
check("留的是**完整本体**（256 B）", hist and hist[0]["bytes"] == SEG, str(hist))
check("改块后公开验证仍通过", public_verify_ok(mgr))

with db.session() as s:
    hrow = s.execute(select(BlockHistoryRow)).scalars().one()
check(
    "★★ 留存下来的密文**逐字节等于**改写前那一份（不是哈希、不是占位）",
    hrow.ciphertext_hex == ct0[0],
    f"{len(hrow.ciphertext_hex)} B hex",
)
check("留存的分量与改写前一致", str(elems0[0]) == hrow.element, hrow.element)
check("留存的块密钥密文非空（回退要能用它解开）", bool(hrow.key_ct), hrow.key_ct[:40])
check("改块之后解出的是新内容", read_bytes(mgr, sk) == NEW0[:SEG] + PLAIN[SEG:])


section("3. 回退：内容、分量、承诺都要回到上一版")

out2 = mgr.restore_previous(OWNER, "f1", [0])
check("回退了第 0 块", out2["restored"] == [0], str(out2))
check("★ 回退产生新版本号（只增不减）= 3", out2["version"] == 3, str(out2["version"]))
check("★★ 回退后解出的明文与改写前逐字节相同", read_bytes(mgr, sk) == PLAIN)
elems1 = [store.values[g] for g in store.file_indices(OWNER, "f1")]
check("★★ 分量回到改写前的值", elems1[0] == elems0[0], f"{elems1[0]} vs {elems0[0]}")
check("★★ 公开承诺 C 回到上一版的值（重新承诺成立）", store.delta_of(OWNER, "f1").C == C0)
check("回退后公开验证通过", public_verify_ok(mgr))
check("回退后 check() 全绿", mgr.check() == [], str(mgr.check()[:2]))
ct1 = [
    store.cipher_of(OWNER, "f1", [i])["blocks"][0]["ciphertext_hex"]
    for i in range(4)
]
check("★ 只有第 0 块的密文变了，其余三块一字未动", ct1[1:] == ct0[1:] and ct1[0] == ct0[0])


section("4. 来回回退：被换下来的那一版成了新的上一版")

hist2 = mgr.history_of(OWNER, "f1")
check("历史里现在是「改写那版」（版本 2）", hist2 and hist2[0]["version"] == 2, str(hist2))
check("★ 每块仍然只有一行（当前 + 上一版，不是无限快照）", len(hist2) == 1, str(len(hist2)))
out3 = mgr.restore_previous(OWNER, "f1", [0])
check("再回退一次 → 版本 4", out3["version"] == 4, str(out3["version"]))
check("★★ 内容回到「改写那版」", read_bytes(mgr, sk) == NEW0[:SEG] + PLAIN[SEG:])
check("再回退后公开验证通过", public_verify_ok(mgr))


section("5. 边界：越界 / 空请求 / 没留过的块")

check("越界块号被拒（OutOfRange）", raises(OutOfRange, mgr.restore_previous, OWNER, "f1", [99]))
check("空列表被拒（OutOfRange）", raises(OutOfRange, mgr.restore_previous, OWNER, "f1", []))
check(
    "没留过上一版的块被拒（NotFound，不会假装回退成功）",
    raises(NotFound, mgr.restore_previous, OWNER, "f1", [1]),
)
check("别的文件不存在（NotFound）", raises(NotFound, mgr.restore_previous, OWNER, "没有这份", None))


section("6. 重启：历史必须落在库里，重启后照样能回退")

del store, mgr
db.engine.dispose()
settings2, db2, mgr2 = fresh_manager()
hist3 = mgr2.history_of(OWNER, "f1")
check("★ 重启后历史还在", len(hist3) == 1 and hist3[0]["block_idx"] == 0, str(hist3))
check("重启后 check() 全绿", mgr2.check() == [], str(mgr2.check()[:2]))
check("重启后公开验证通过", public_verify_ok(mgr2))
sk2 = mgr2.unseal_user_key(OWNER, PASSWORD)
check("重启后解得出内容", bytes.fromhex(mgr2.read_plain(OWNER, "f1", sk2)["data_hex"]).startswith(NEW0[:SEG]))
out4 = mgr2.restore_previous(OWNER, "f1", None)
check("★ 重启后回退仍然可用（不点名 = 所有留过的块）", out4["count"] == 1, str(out4))
check("重启后回退完，公开验证通过", public_verify_ok(mgr2))


section("收尾")

print()
if FAILURES:
    print(f"失败 {len(FAILURES)} 项：")
    for f in FAILURES:
        print("  - " + f)
else:
    print("全部通过 ✓ —— 留存的是本体、只留两版、回退会重新承诺且可来回")
db2.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
print("[cleanup] 已删除临时库")
sys.exit(1 if FAILURES else 0)
