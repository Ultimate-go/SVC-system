"""★ 故障演练的验证 —— 节点突然没了，各功能到底会怎样。

这份脚本**不是**测"能不能模拟出来"，而是测「模拟出来之后，业务功能的
真实反应是否正确」——因为整个演练的立足点就是"看到的失败是真代码的表现"。

三档递进，每一档都断言**两件事**：该坏的坏了、**不该坏的没坏**。
只断言"坏了"很容易，难的是证明"多副本确实救了场"。

+-------------------------------+----------------------------------+
| 场景                          | 应当发生什么                     |
+===============================+==================================+
| 掉线 1 台（可恢复）           | 读/验证/聚合**照常**（副本救场） |
|                               | 写（改块/追加/删）**会失败**     |
|                               | 恢复之后一切如初                 |
+-------------------------------+----------------------------------+
| 永久损毁 1 台（不可逆）       | 业务靠副本**仍然可用**           |
|                               | 但它回来是**空的**，集群不再自洽 |
+-------------------------------+----------------------------------+
| 永久损毁**同一块的全部副本**  | 那块**永久丢失**：验证、解密、   |
|                               | 聚合全挂 —— **恢复机器也救不回** |
+-------------------------------+----------------------------------+

.. note::

   库建在 ``scripts/`` 里、用完即删，**不碰**项目根那份 ``vds.db``。
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.faults import MODE_DESTROYED, MODE_DOWN, FaultyTransport  # noqa: E402
from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from core.transport import LocalTransport, TransportError, WriteError  # noqa: E402

FAILURES: list[str] = []
NODES = ("n1", "n2", "n3", "n4")
BLK = 8
OWNER, FILE_KEY = "alice", "病历"
PLAIN = b"AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"  # 32 字节 → 4 块


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def raises(fn, exc_type, *, label: str = "") -> tuple[bool, str]:
    """``fn()`` 是否抛出了 ``exc_type``（返回 (是否, 消息)）。"""
    try:
        fn()
    except exc_type as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001 - 抛了别的类型也算没通过
        return False, f"抛的是 {type(exc).__name__}: {exc}"
    return False, "没有抛异常"


def no_raise(fn) -> tuple[bool, str]:
    """``fn()`` 是否**顺利跑完**（返回 (是否, 失败原因)）。"""
    try:
        fn()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def fresh(replica: int = 2, *, seed: bytes = b"fault-drill"):
    """一套干净的装置：4 台同进程节点 + 可演练传输层 + 明文密钥库。"""
    session = new_session(l=256, n_max=64, modulus_bits=1024, seed=seed)
    inner = LocalTransport(session, NODES)
    ft = FaultyTransport(inner)
    store = PlainKeyStore(
        session,
        node_ids=NODES,
        segment_bytes=BLK,
        transport=ft,
        replica_factor=replica,
    )
    return session, inner, ft, store


def reps_of(store, owner, file_key, idx=0) -> tuple[str, ...]:
    """第 ``idx`` 块的**全部持有者**（主副本在前）。"""
    pos = store.registry.positions_of(owner, file_key)[idx]
    return tuple(store.replicas_of(pos))


def snapshot(inner) -> dict:
    """节点数据的**深快照** —— 用来断言演练"一个字节都没动"。

    ★★ 这是本轮最关键的一条回归测试。曾经的那个版本把"模拟磁盘被毁"
    实现成了真的去清节点数据库（``states.pop`` / ``/node/reset``），
    而跨进程模式下节点数据**只存在节点自己那份 SQLite 里**、协调者没有副本
    —— 于是"模拟"变成了一次真的、不可恢复的数据销毁。
    从此以后，只要这个快照在演练前后不相等，测试就会红。
    """
    return copy.deepcopy(inner.export_states())


def readable(store, owner=OWNER, file_key=FILE_KEY) -> bool:
    try:
        return store.read(owner, file_key) == PLAIN
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
section("0. 基线：4 台节点、每块 2 份副本")

_session, _inner, ft, store = fresh()
store.upload(OWNER, FILE_KEY, PLAIN)
store.upload(OWNER, "f2", b"12345678" * 4)

pos_all = store.registry.positions_of(OWNER, FILE_KEY)
check("上传后 4 块", len(pos_all) == 4, f"{len(pos_all)}")
check("每块 2 份副本", all(len(store.replicas_of(p)) == 2 for p in pos_all))
check("基线：读得出来", readable(store))
check("基线：承诺验证通过", store.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])
check("基线：聚合检索通过", bool(store.query(list(pos_all[:2])).ok))
check("基线：自检全绿", store.check() == [], str(store.check()[:2]))
check("基线：演练名单为空", ft.faulty() == set())

all_reps = {n for p in pos_all for n in store.replicas_of(p)}
check("4 台里至少 3 台分到了块（轮转）", len(all_reps) >= 3, f"{sorted(all_reps)}")


# ===========================================================================
section("1. 掉线 1 台（可恢复）—— 读/验证照常，写会失败")

victim = sorted(all_reps)[0]
ft.knock_out([victim])
print(f"        已让 {victim} 掉线")

rows = {r["node_id"]: r for r in store.transport.report()}
check(f"{victim} 被报成 unreachable", rows[victim].get("unreachable") is True)
check("其它三台仍然在线", all(not rows[n].get("unreachable") for n in NODES if n != victim))

check("★ 读明文**照常**（自动退到副本）", readable(store))
check("★ 承诺验证**照常**通过", store.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])
check("★ 聚合检索**照常**通过", bool(store.query(list(pos_all)).ok))

hit_read, msg1 = raises(lambda: store.modify(OWNER, FILE_KEY, 0, b"X" * BLK),
                        TransportError)
check("★ 改块**会失败**（广播写打不到掉线的那台）", hit_read, msg1.split("\n")[0][:90])
check("失败信息里说清了是「故障演练」", "故障演练" in msg1)

hit_add, msg2 = raises(lambda: store.append(OWNER, FILE_KEY, b"Y" * BLK),
                       TransportError)
check("★ 追加**会失败**", hit_add, msg2.split("\n")[0][:90])

check("掉线不改变分片（副本表没动）",
      store.replicas_of(pos_all[0]) == reps_of(store, OWNER, FILE_KEY, 0))

# ★ 这里**不断言**「失败写之后还读得出明文」：``PlainKeyStore.modify`` 是
#   "密钥一生成就直接塞进内存字典"，写失败也不回退 —— 于是旧密文配上了新
#   钥匙。那是**这个测试替身**的局限（真系统走 ``manager``：块密钥封好后只在
#   ``_finish`` 里落库，写失败即丢弃）。真系统那条在第 5 节验证。
check("掉线期间密文没被动过（承诺仍对得上）",
      store.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])

ft.restore()
print(f"        {victim} 已恢复")
ok, why = no_raise(lambda: store.modify(OWNER, FILE_KEY, 0, b"X" * BLK))
check("★ 恢复后：改块成功", ok, why)
check("★ 恢复后：明文更新正确", store.read(OWNER, FILE_KEY) == b"X" * BLK + PLAIN[BLK:])
check("恢复后：自检重新全绿", store.check() == [], str(store.check()[:3]))
check("恢复后：演练名单清空", ft.faulty() == set())


# ===========================================================================
section("2. 永久损毁 1 台（不可逆）—— 业务靠副本仍可用，但它回来是空的")

_session, _inner, ft2, store2 = fresh(seed=b"destroy-one")
store2.upload(OWNER, FILE_KEY, PLAIN)
snap2 = snapshot(_inner)
pos2 = store2.registry.positions_of(OWNER, FILE_KEY)
victim2 = sorted({n for p in pos2 for n in store2.replicas_of(p)})[0]

held_before = sum(
    1 for p in pos2 if victim2 in store2.replicas_of(p)
)
ft2.knock_out([victim2], destroy=True)
print(f"        已将 {victim2} 标记为「永久损毁」（它参与 {held_before} 块）")

check("损毁期间：读明文照常（副本救场）", readable(store2))
check("损毁期间：承诺验证照常", store2.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])
check("★★ 标记损毁**不动数据**（节点状态快照逐位相同）",
      snapshot(_inner) == snap2)

ft2.restore()
print(f"        {victim2} 已恢复")
row_after = {r["node_id"]: r for r in store2.transport.report()}[victim2]
check("★ 恢复后它手里还是原来那些块（held 不变）",
      row_after.get("held") == held_before, f"held={row_after.get('held')}")
check("★ 恢复后视图完好", bool(row_after.get("vectors")))
check("★ 恢复后读得出明文", readable(store2))
check("★ 恢复后承诺验证通过", store2.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])
check("★ 恢复后自检重新全绿（演练零残留）", store2.check() == [],
      str(store2.check()[:3]))
check("★★ 演练全程零破坏（快照逐位相同）", snapshot(_inner) == snap2)


# ===========================================================================
section("3. 把**某一块的全部副本**都标成永久损毁 —— 那块会真的丢")

_session, _inner, ft3, store3 = fresh(seed=b"destroy-all-replicas")
store3.upload(OWNER, FILE_KEY, PLAIN)
snap3 = snapshot(_inner)
pos3 = store3.registry.positions_of(OWNER, FILE_KEY)
target = pos3[1]                       # 挑第 2 块下手
reps3 = tuple(store3.replicas_of(target))
print(f"        第 2 块（位置 {target}）的副本在：{list(reps3)}")

ft3.knock_out(list(reps3), destroy=True)
check("受影响的是 2 台（副本数）", len(reps3) == 2)
check("★★ 标记损毁仍不动数据（快照逐位相同）", snapshot(_inner) == snap3)

kind, m = raises(lambda: store3.read(OWNER, FILE_KEY), TransportError)
check("★ 取明文失败（副本全不可达）", kind, m[:90])
kind, m = raises(lambda: store3.cipher_of(OWNER, FILE_KEY), Exception)
check("★ 验证完整性失败", kind, m.split("\n")[0][:100])
kind, m = raises(lambda: store3.query([target]), Exception)
check("★ 证据池聚合失败", kind, m.split("\n")[0][:100])
check("★ 报错里列清了「谁持多少块、谁联系不上」", "联系不上" in m or "副本" in m, m[:110])

# ★ 顺序很重要：``modify`` / ``truncate`` 会**先把新块密钥塞进 PlainKeyStore
#   的内存字典**（那个测试替身"密钥一生成就收下"，写失败也不回退），
#   所以**做过这两条之后就别再断言"读得出明文"了** —— 那是替身的局限，
#   不是核心的问题（真系统的块密钥封好后只在成功时落库，第 5 节验证过）。
kind, m = raises(lambda: store3.modify(OWNER, FILE_KEY, 1, b"Z" * BLK), Exception)
check("★ 改块失败", kind, m.split("\n")[0][:90])
kind, m = raises(lambda: store3.truncate(OWNER, FILE_KEY, 1), Exception)
check("★ 截断失败", kind, m.split("\n")[0][:90])
check("★★ 故障期间数据始终没被动过（快照逐位相同）", snapshot(_inner) == snap3)

# ★★ 与前一版的**关键差别**：恢复之后一切如初 —— 因为整个演练从不删数据。
#    旧版这里是"恢复也救不回"，而那正是它真正的 bug：跨进程模式下节点
#    数据只存在节点自己那份 SQLite 里、协调者没有副本 —— 真删了就真没了。
ft3.restore()
print("        已恢复 —— 因为演练从不删数据，一切应当复原")
check("★★ 恢复动作本身也不动数据（快照逐位相同）", snapshot(_inner) == snap3)
# ★ 这里**不断言明文可读**：上面那次失败的 modify 已经把替身的钥匙换掉了。
#   真系统那条由第 5 节（manager + 真密钥封装）负责。
check("★ 恢复后承诺验证通过", store3.cipher_of(OWNER, FILE_KEY)["verify"]["ok"])
check("★ 恢复后聚合通过", bool(store3.query(list(pos3)).ok))
check("★ 恢复后自检全绿", store3.check() == [], str(store3.check()[:3]))
ok3, why3 = no_raise(lambda: store3.modify(OWNER, FILE_KEY, 1, b"Z" * BLK))
check("★ 恢复后改块也通", ok3, why3[:80])


# ===========================================================================
section("4. 副本数 = 1：坏一台就立刻丢块（对照）")

_session, _inner, ft4, store4 = fresh(replica=1, seed=b"replica-one")
store4.upload(OWNER, FILE_KEY, PLAIN)
snap4 = snapshot(_inner)
pos4 = store4.registry.positions_of(OWNER, FILE_KEY)
single = store4.replicas_of(pos4[0])
check("每块只有 1 份副本", len(single) == 1, f"{list(single)}")

ft4.knock_out([single[0]], destroy=True)
check("★ 副本=1 时，坏一台就**立刻**读不出来", not readable(store4))
ft4.restore()
check("★ 恢复后立刻读得出来（演练没删任何东西）", readable(store4))
check("★ 自检也全绿", store4.check() == [], str(store4.check()[:3]))
check("★★ 演练全程零破坏（快照逐位相同）", snapshot(_inner) == snap4)


# ===========================================================================
section("5. 管理端接口：fault_drill + 影响面")

DB_FILE = ROOT / "scripts" / "_tmp_fault_drill.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    if p.exists():
        p.unlink()
os.environ["VDS_DB_PATH"] = str(DB_FILE)
os.environ.setdefault("VDS_SECRET_KEY", "fault-drill-test-key")

from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.manager import StoreManager  # noqa: E402
from backend.models import UserRow  # noqa: E402
from backend.security import hash_password  # noqa: E402

_settings = Settings()
_settings.db_path = DB_FILE
_db = Database(_settings.database_url())
_db.create_all()
mgr = StoreManager(_settings, _db)
mgr.bootstrap()
with _db.session() as s:
    s.add(UserRow(username="alice", pwd_hash=hash_password("vds12345"),
                  role="user", display_name="Alice"))
    s.commit()
mgr.ensure_user_key("alice", "vds12345")
mgr.upload("alice", "mid", b"M" * (_settings.segment_bytes * 3), segment_bytes=_settings.segment_bytes)
mstore = mgr._require()
check("管理端已装上演练代理", isinstance(mstore.transport, FaultyTransport))

st = mgr.fault_drill("status")
check("status 动作可用", st["faulty"] == [] and st["impact"]["lost_count"] == 0)
check("无故障时给出明确结论", "在线" in st["impact"]["note"], st["impact"]["note"][:60])

# 挑一台**主副本**在场上的机器 —— 这样 degraded 一定 > 0
mid_pos = mstore.registry.positions_of("alice", "mid")
pick = mstore.replicas_of(mid_pos[0])[0]
st = mgr.fault_drill("knock_out", [pick], mode=MODE_DOWN)
check(f"掉线 {pick} 后 faulty 名单正确", st["faulty"] == [pick], str(st["faulty"]))
check("影响面：主副本受损的块被数出来", st["impact"]["degraded_blocks"] > 0,
      f"degraded={st['impact']['degraded_blocks']}")
check("影响面：不是永久丢失", st["impact"]["lost_count"] == 0)
check("影响面文案说清「多副本救场」", "副本" in st["impact"]["note"],
      st["impact"]["note"][:70])

# ★★ 真系统的关键性质：**写失败不会弄坏已经存下来的数据**。
#    manager 里块密钥是"封好后只在 _finish 里落库"，写失败即丢弃 ——
#    与 PlainKeyStore（密钥一生成就塞内存）不同，所以这一条只能在真系统上验。
wok, wwhy = raises(lambda: mgr.modify_block("alice", "mid", 0, b"Q" * _settings.segment_bytes),
                   Exception)
check("掉线时改块失败", wok, wwhy[:80])
sk_m = mgr.unseal_user_key("alice", "vds12345")
ok, why = no_raise(lambda: mgr.read_plain("alice", "mid", sk_m))
check("★ 写失败**不会**弄坏已存数据（块密钥只在成功时落库）", ok, why[:90])

# ★★ 掉线模式必须能**完整还原** —— 演练不能在集群里留下任何残留。
#    这一条比"恢复后能读"严得多：``check()`` 会逐台核对节点视图、
#    副本表与实际持有者集合、以及增量摘要与一次性承诺是否一致。
#    （原来的实现**做不到**：广播写的“半成功”会在恢复后留下一台
#      摘要超前、另一台落后的死局；那正是被这个演练逼出来修掉的 bug 之一。）
st = mgr.fault_drill("restore")
check("掉线后可完整恢复：名单清空", st["faulty"] == [])
check("★ 掉线演练**零残留**：全面自检重新全绿", mgr.check() == [], str(mgr.check()[:3]))

# 把某一块的**全部**副本都永久损毁
allreps = list(mstore.replicas_of(mid_pos[0]))
st = mgr.fault_drill("knock_out", allreps, mode=MODE_DESTROYED)
check("★ 影响面报出会永久丢失的块", st["impact"]["lost_count"] >= 1,
      f"lost={st['impact']['lost_count']}")
check("★ 影响面列清是哪份文件的哪一块",
      any(f["file_key"] == "mid" for f in st["impact"]["files_affected"]),
      str(st["impact"]["files_affected"])[:90])
check("★ 文案点明「副本数不够 + 不可逆故障」", "不可逆" in st["impact"]["note"],
      st["impact"]["note"][:80])
# ★★ 这一条是给用户的**安全承诺**：文案必须说清它不会删数据。
#    曾经的那个版本真的去清节点数据库，用户看到"永久损毁"四个字
#    以为只是演示 —— 结果真丢了数据。
check("★★ 文案说清「本演练不删数据」", "不删数据" in st["impact"]["note"],
      st["impact"]["note"][-40:])

kind, m = raises(lambda: mgr.read_plain("alice", "mid",
                                        mgr.unseal_user_key("alice", "vds12345")),
                 Exception)
check("★ 管理端读这文件也失败", kind, m[:80])

st = mgr.fault_drill("restore")
check("恢复后 faulty 清空", st["faulty"] == [])
check("恢复后 influence 归零", st["impact"]["lost_count"] == 0)
# ★★ 管理端也要证明"零残留"：恢复之后盘账、节点视图、副本表全对得上。
check("★★ 管理端：恢复后自检全绿（演练零残留）", mgr.check() == [],
      str(mgr.check()[:3]))
sk_after = mgr.unseal_user_key("alice", "vds12345")
ok, why = no_raise(lambda: mgr.read_plain("alice", "mid", sk_after))
check("★★ 管理端：恢复后数据完好可读（演练没删任何东西）", ok, why[:80])

kind, m = raises(lambda: mgr.fault_drill("bogus"), ValueError)
check("未知 action 被拒", kind, m[:60])
kind, m = raises(lambda: mgr.fault_drill("knock_out", ["n99"]), ValueError)
check("不认识的节点被拒", kind, m[:60])
kind, m = raises(lambda: mgr.fault_drill("knock_out", list(mgr.settings.node_ids),
                                        mode="no-such-mode"), ValueError)
check("未知 mode 被拒", kind, m[:60])
# ★ 参数形状的两道闸：
#   ① 直接给字符串会被 tuple("n1") 逐字符拆开 —— 必须当场拒掉（否则报错
#      长成“不认识的节点 ['n','1']”，完全看不出是自己参数给错了）；
#   ② restore 给一个不认识的名字必须报错，**不能静默忽略** ——
#      默默略过会让“我明明恢复了 n9”变成一次假成功。
kind, m = raises(lambda: mgr.fault_drill("knock_out", "node-1"), TypeError)
check("传字符串做名单被拒（防逐字符拆开）", kind, m[:70])
kind, m = raises(lambda: mgr.fault_drill("restore", ["n99"]), ValueError)
check("restore 给不认识的节点被拒（不静默忽略）", kind, m[:60])
kind, m = raises(lambda: mgr.fault_drill("knock_out", []), ValueError)
check("空名单被拒", kind, m[:50])

_db.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    if p.exists():
        p.unlink()
print("[cleanup] 已删除临时库")


# ===========================================================================
section("6. HTTP 接口层（前端实际会打的那两条）")

DB2 = ROOT / "scripts" / "_tmp_fault_drill_http.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB2) + suffix)
    if p.exists():
        p.unlink()
os.environ["VDS_DB_PATH"] = str(DB2)
os.environ["VDS_SECRET_KEY"] = "fault-drill-http-key"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select as _select  # noqa: E402

from backend.config import Settings as _S2  # noqa: E402
from backend.db import Database as _DB2  # noqa: E402
from backend.main import create_app  # noqa: E402
from backend.models import AuditRow as _AR  # noqa: E402
from backend.models import UserRow as _UR  # noqa: E402
from backend.security import hash_password as _hp  # noqa: E402

_s2 = _S2()
_s2.db_path = DB2
_db2 = _DB2(_s2.database_url())
_db2.create_all()
with _db2.session() as s:
    s.add(_UR(username="root", pwd_hash=_hp("vds12345"), role="admin",
              display_name="Root"))
    s.add(_UR(username="plain", pwd_hash=_hp("vds12345"), role="user",
              display_name="Plain"))
    s.commit()
_db2.engine.dispose()


def login(client, who: str) -> dict:
    r = client.post("/api/auth/login", json={"username": who, "password": "vds12345"})
    return {"Authorization": f"Bearer {r.json().get('token', '')}"}


_capp = create_app()
with TestClient(_capp) as client:
    # ★ 登录要求这个账号**已经有密钥对**（没有会 409）——
    #   ``seed.py`` 也是这么做的：建号之后先 ``ensure_user_key``。
    _mgr = client.app.state.manager
    _mgr.ensure_user_key("root", "vds12345")
    _mgr.ensure_user_key("plain", "vds12345")

    adm = login(client, "root")
    usr = login(client, "plain")

    r = client.get("/api/admin/fault-drill", headers=adm)
    check("GET /api/admin/fault-drill → 200", r.status_code == 200, str(r.status_code))
    check("初始名单为空、且带影响面", r.json().get("faulty") == [] and "impact" in r.json())

    nids = list(_mgr.settings.node_ids)
    r = client.post(
        "/api/admin/fault-drill",
        json={"action": "knock_out", "nodes": [nids[0]], "mode": "down"},
        headers=adm,
    )
    check("POST knock_out → 200", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
    check("faulty 名单与请求一致", r.json().get("faulty") == [nids[0]],
          str(r.json().get("faulty")))

    r = client.post("/api/admin/fault-drill",
                    json={"action": "knock_out", "nodes": [nids[0]],
                          "mode": "destroyed"}, headers=adm)
    check("POST knock_out(destroyed) → 200 且归入损坏名单",
          r.status_code == 200 and r.json().get("destroyed") == [nids[0]],
          str(r.json().get("destroyed")))

    r = client.post("/api/admin/fault-drill", json={"action": "restore"}, headers=adm)
    check("POST restore → 200 且名单清空",
          r.status_code == 200 and r.json().get("faulty") == [])

    r = client.post("/api/admin/fault-drill", json={"action": "bogus"}, headers=adm)
    check("未知 action → 422（pydantic 拦下）", r.status_code == 422, str(r.status_code))
    r = client.post("/api/admin/fault-drill",
                    json={"action": "knock_out", "nodes": ["n99"]}, headers=adm)
    check("不认识的节点 → 400", r.status_code == 400, str(r.status_code))
    r = client.post("/api/admin/fault-drill", json={"action": "knock_out"}, headers=adm)
    check("knock_out 不给节点 → 400", r.status_code == 400, str(r.status_code))

    # ★★ 越权面：这是**破坏性**操作，普通用户必须打不动
    r = client.get("/api/admin/fault-drill", headers=usr)
    check("★ 普通用户读演练状态 → 403", r.status_code == 403, str(r.status_code))
    r = client.post("/api/admin/fault-drill",
                    json={"action": "knock_out", "nodes": [nids[0]]}, headers=usr)
    check("★ 普通用户发动演练 → 403", r.status_code == 403, str(r.status_code))
    check("★ 被拒后**没有**真的掉线", _mgr.fault_status()["faulty"] == [])

    r = client.get("/api/admin/fault-drill")
    check("未登录 → 401/403", r.status_code in (401, 403), str(r.status_code))

with _db2.session() as s:
    acts = [a.action for a in s.execute(_select(_AR)).scalars()]
check("★ 演练动作留了审计", "fault_drill" in acts, str(sorted(set(acts))[:8]))

# ★ 释放 app 自己的引擎，否则 Windows 上删不掉这个库（WinError 32）
_capp.state.manager.db.engine.dispose()
_db2.engine.dispose()
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB2) + suffix)
    if p.exists():
        p.unlink()
print("[cleanup] 已删除 HTTP 测试库")


# ===========================================================================
section("7. 回归：传输层代理**不得**伪装成同进程模式")


# ★★ 这一条盯的是本轮修掉的第二个 bug：
#    ``FaultyTransport`` 曾经**定义**了 ``export_states`` / ``import_states``，
#    而 ``backend/manager.py`` 用 ``getattr(transport, "export_states", None)``
#    判断"是不是同进程模式"。代理一旦**定义**了它，那个 ``getattr`` 就永远
#    不是 ``None`` —— 跨进程模式会误入只属于同进程的落库分支，在每一次
#    写入成功后炸一个 ``AttributeError``。
class _HttpLike:
    """只实现协议要求的方法 —— 模拟**跨进程**传输（没有 export_states）。"""

    node_ids = ("x1",)

    def set_crs(self, crs_dict): ...
    def report(self, *, verify: bool = False): return []
    def retrieve(self, node_id, offset, Q): ...
    def pos_prove(self, node_id, offset, indices): ...
    def adopt(self, node_id, **kw): ...
    def apply_append(self, **kw): ...
    def apply_update(self, **kw): ...
    def drop(self, offset, positions): ...
    def apply_delete(self, **kw): ...


_proxy = FaultyTransport(_HttpLike())
check("★ 内层没有 export_states 时，代理**也不报告**有它",
      getattr(_proxy, "export_states", None) is None)
check("★ import_states 同理", getattr(_proxy, "import_states", None) is None)
check("★ 同进程实现该有的它照样转发（可调用）",
      callable(getattr(ft4, "export_states", None)))
check("★ 其它方法（如 report）正常透传",
      _proxy.report() == [] and _proxy.node_ids == ("x1",))


# ---------------------------------------------------------------------------
section("8. ★ 跨进程：故障节点必须**真的**联系不上（否则写入会假成功）")

# ★★ 这一条盯的是**第三个**坑（本轮实测发现）：
#    演练只在协调者这一侧标记"不可达"，而节点进程**还活着** ——
#    内层不包一层，就会照常把更新推过去、还拿到 200，于是写入**成功**，
#    待补推现场永远建不起来，面板承诺的「改块 / 追加 / 截断 会被拦下」是假的，
#    「补推」这条路在演示里根本走不到。
class _CrossProc:
    """模仿 ``HttpTransport``：所有请求都过 ``_request``，广播写逐台收集失败。"""

    node_ids = ("x1", "x2")

    def __init__(self) -> None:
        self.sent: dict[str, int] = {}

    def _request(self, method, node_id, path, **kw):
        self.sent[str(node_id)] = self.sent.get(str(node_id), 0) + 1
        return {"ok": True}

    def set_crs(self, crs_dict): ...
    def report(self, *, verify: bool = False): return []
    def retrieve(self, node_id, offset, Q):
        self._request("POST", node_id, "/node/retrieve")
        return None
    def pos_prove(self, node_id, offset, indices):
        self._request("POST", node_id, "/node/pos")
        return None
    def adopt(self, node_id, **kw): self._request("POST", node_id, "/node/adopt")
    def apply_append(self, **kw):
        failures: list[dict] = []
        payloads: dict[str, dict] = {}
        for nid in self.node_ids:
            try:
                self._request("POST", nid, "/node/append", json={})
            except TransportError as exc:
                failures.append({"node": nid, "reason": str(exc)})
                payloads[nid] = {"assigned": [0], "mark": f"payload-for-{nid}"}
        if failures:
            raise WriteError("有节点没能跟上这次更新", failures, "append", payloads)
    def apply_update(self, **kw): ...
    def drop(self, offset, positions): ...
    def apply_delete(self, **kw): ...


def _catch(fn):
    try:
        fn()
        return None
    except Exception as exc:  # noqa: BLE001 - 测试就是要抓住它
        return exc


def _append(_t):
    """按 ``NodeTransport`` 的真实签名发一次广播写。

    ★ ``FaultyTransport.apply_append`` 是**具名**参数（它要原样转给内层），
      所以这里不能只传一个 ``delta_old`` —— 少一个就是 ``TypeError``，
      看起来像"功能坏了"，其实只是测试自己没按签名调。
    """
    return _t.apply_append(
        delta_old=None,
        delta_new=None,
        op_delta=None,
        witness=None,
        assignments={},
        blobs={},
    )


_inner = _CrossProc()
_ft = FaultyTransport(_inner)
check("健康时读得到（对照组）", _catch(lambda: _ft.retrieve("x1", 0, [0])) is None)
_ft.knock_out(["x2"])

_err_read = _catch(lambda: _ft.retrieve("x2", 0, [0]))
check("★ 故障节点的**读**被切断", isinstance(_err_read, TransportError),
      type(_err_read).__name__)
check("★ 没被点名的节点照常", _catch(lambda: _ft.retrieve("x1", 0, [0])) is None)

_inner.sent.clear()   # ★ 上面那几次**读**也走 _request，先把计数清干净
_err = _catch(lambda: _append(_ft))
check("★★ 掉线期间广播写**失败**（而不是假成功）", isinstance(_err, WriteError),
      type(_err).__name__)
if isinstance(_err, WriteError):
    check("★★ 失败清单点名了那台节点", _err.nodes == ["x2"], str(_err.nodes))
    check("★★ 带上了补推要用的那份负载",
          (_err.payloads.get("x2") or {}).get("mark") == "payload-for-x2",
          str(_err.payloads)[:120])
    check("★ 健康的那台确实收到了这次写，故障那台一次都没发出去",
          _inner.sent == {"x1": 1}, str(_inner.sent))

_ft.restore()
check("★ 恢复之后它又联系得上了", _catch(lambda: _ft.retrieve("x2", 0, [0])) is None)
_inner.sent.clear()
check("★★ 恢复之后广播写恢复成功（「补推」就是这一步）",
      _catch(lambda: _append(_ft)) is None)
check("★ 恢复后两台都收到了",
      _inner.sent == {"x1": 1, "x2": 1}, str(_inner.sent))


# ---------------------------------------------------------------------------
print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项未通过")
    for f in FAILURES:
        print("   FAIL " + f)
    sys.exit(1)
print("结果：ALL PASS —— 掉线/损毁下各功能的真实反应全部符合预期")
print("=" * 72)
