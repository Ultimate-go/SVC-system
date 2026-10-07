"""★ 待补推现场能不能**跨重启**活下来。

用户报的症状原文：「一次失败 + 重启 = 起不来」——

   写推失败时，协调者**内存**里留住了现场（补推就能收敛），可进程一没，
   现场就永久丢了，而**节点上已经跟进的那几台不会回退**。于是重启时
   启动守卫看到"节点跑在前面"，报一句「存储节点与协调者不同步，拒绝启动」
   就把整个系统锁死 —— 唯一出路是人工去每台节点上清那半段。

这份脚本测的就是"修好之后"的合同。两段：

**第一段（store 层）—— 等价性**。同一套确定性装置跑两遍：

* A：四个操作各自**一次就成功**，快照下 ``files`` / ``n`` / ``check()``；
* B：**让两台收不到写** ⇒ 真失败 ⇒ ``export_pending()`` ⇒ ``json`` 往返
  （模拟落库与读回）⇒ **换一套全新的装置**（模拟重启）⇒ 把前面的步骤重放
  ⇒ ``import_pending()`` ⇒ 节点回来 ⇒ ``retry_pending()``。

然后断言 **B 的快照 == A 的快照**。这条比"没报错"强得多：它证明
「补推成功」与「第一次就成功」**算出的是同一本账** —— 而偏差恰恰只在
故障恢复那条路上才看得见（本地全绿也发现不了）。

**第二段（manager 层）—— 真重启**。走真 ``Settings`` + ``Database`` +
``StoreManager``：

* 写失败 ⇒ 盘上留下现场（``pending_write`` 表的唯一一行）；
* ``db.dispose()`` + 重建一套（**这就是重启**）⇒ 必须**起得来**，
  现场被装回内存，守卫记下"哪几台已经跑在前面"；
* 节点回来 ⇒ ``retry_push()`` ⇒ 收敛 + 补落库 + **明文逐位相同** + 盘上抹掉；
* 陈旧现场（节点其实已经对齐）⇒ 判定陈旧并丢弃，不许凭空报警；
* 缺"落库那半"的现场 ⇒ **拒绝恢复**（宁可按老办法人工处理，也不造出
  一个"向量自洽、库里空着"的**假收敛**）。

.. note::

   库建在 ``scripts/`` 里、用完即删，**不碰**项目根那份 ``vds.db``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ★ 必须在 import Settings 之前设好：db_path 的 default_factory 会读它。
DB_FILE = ROOT / "scripts" / "_tmp_pending_restart.db"
os.environ["VDS_DB_PATH"] = str(DB_FILE)
os.environ.setdefault("VDS_SECRET_KEY", "pending-restart-test-key")

from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from core.transport import LocalTransport, WriteError  # noqa: E402

FAILURES: list[str] = []
NODES = ("n1", "n2", "n3", "n4")
VICTIMS = ("n3", "n4")
SEG = 8
OWNER, KEY = "alice", "f1"
PW = "vds12345"
PLAIN = b"AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"      # 32 字节 → 4 块
NEWBLK = b"cccccccc"                            # 改第 1 块
MORE = b"IIIIJJJJKKKKLLLL"                      # 追加 2 块


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


def raises(fn, exc_type) -> tuple[bool, str]:
    try:
        fn()
    except exc_type as exc:
        return True, str(exc).replace("\n", " ")
    except Exception as exc:  # noqa: BLE001
        return False, f"抛的是 {type(exc).__name__}: {exc}"
    return False, "没有抛异常"


# ===========================================================================
# 假传输层 —— 只做一件事：让指定的那几台**收不到写**（读照常）
# ===========================================================================
#
# ★ 为什么要有它：真系统里这一层是 ``HttpTransport``，它会自己把"哪几台没收到"
#   与"该发给它们的那份请求体"打包成 ``WriteError``。这里要复现的正是那个合同，
#   所以假传输层的 ``payloads`` 也必须是**能 JSON 化**的东西（现场要落库）。
#
# ★ ``_HELD`` 故意放在**模块级**：它代表**节点那一侧**。节点是独立进程，
#   协调者重启它们不跟着重启 —— 所以"要发的东西"按凭证放在这里，是忠实的。

_HELD: dict[str, tuple[str, dict]] = {}
_HELD_N = [0]


def _hold(method: str, kw: dict) -> str:
    _HELD_N[0] += 1
    tok = f"{method}-{_HELD_N[0]}"
    _HELD[tok] = (method, kw)
    return tok


class _Flaky:
    """包住 ``LocalTransport``：让指定那几台**收不到写**（读照常）。

    ★ 怎么表达"这几台没收到"：同进程实现（``LocalTransport``）的一次广播是
      **一次推给所有人**的，光把 ``blobs`` 摘掉还不够 —— 那些机器照样会被通知
      "该更新了"，只是手里没新密文，于是内层当场报「更新后本地视图不合法」。
      那是**另一次失败**，不是我们要演的"这台根本没收到"。

      所以这里不去改它的逻辑，只**换一份名单**：造一个只含"能收到的那几台"的
      传输层，**共用同一批节点状态**。这正是跨进程实现天然的样子 —— 一次广播
      对每一台各发一个请求，谁收到谁没收到本来就可以不一样。
    """

    def __init__(self, session, node_ids) -> None:
        self.session = session
        self.node_ids = tuple(node_ids)
        self._inner = LocalTransport(session, self.node_ids)
        self.victims: set[str] = set()
        self.tripped = False
        self.writes = 0
        self.retries = 0

    @property
    def states(self):                     # 节点状态表现在是**共用**的
        return self._inner.states

    def __getattr__(self, name):          # 其余方法（read/report/drop/...）照旧
        return getattr(self._inner, name)

    def import_states(self, states) -> None:
        """★ **什么都不做**：节点状态归节点自己。

        真链路里这一条根本不会发生 —— ``HttpTransport`` 没有 ``import_states``，
        而 ``_persist_nodes`` 在**跨进程模式下是空操作**（节点状态存在各节点
        自己的库里）。本脚本演示的正是跨进程那个场景：节点是独立进程，
        协调者重启它们**不跟着重启**。

        （同进程模式的 ``LocalTransport`` 会拿协调者库里的行**覆盖**节点状态
        —— 那正是单进程部署该有的语义，但会把本脚本要演的东西一并抹掉。）
        """
        return None

    def trip(self, victims=VICTIMS) -> None:
        self.victims = set(victims)
        self.tripped = True

    def untrip(self) -> None:
        self.tripped = False

    def _route(self, nids):
        """只含 ``nids`` 的传输层，共用同一批节点状态（其余方法走内层）。"""
        if set(nids) == set(self.node_ids):
            return self._inner
        sub = LocalTransport(self.session, tuple(nids))
        sub.states = self._inner.states
        return sub

    # -- 写：三个入口共用一套骨架（kwargs **原样**传下去）------------------
    def _write(self, method: str, kw: dict) -> None:
        self.writes += 1
        if not (self.tripped and self.victims):
            return getattr(self._inner, method)(**kw)
        ok = [n for n in self.node_ids if n not in self.victims]
        getattr(self._route(ok), method)(**kw)
        tok = _hold(method, kw)
        raise WriteError(
            "演练：这几台收不到这次写\n  " + "\n  ".join(sorted(self.victims)),
            [{"node": n, "reason": "演练：联系不上"} for n in sorted(self.victims)],
            {"apply_append": "append", "apply_update": "update", "apply_delete": "delete"}[method],
            {n: {"token": tok, "node": n} for n in sorted(self.victims)},
        )

    def apply_append(self, **kw):
        return self._write("apply_append", kw)

    def apply_update(self, **kw):
        return self._write("apply_update", kw)

    def apply_delete(self, **kw):
        return self._write("apply_delete", kw)

    # -- 补推：把当时没送到的那几台补上 ---------------------------------
    def retry_write(self, error) -> list[dict]:
        self.retries += 1
        still: list[dict] = []
        for nid, p in error.payloads.items():
            if self.tripped and nid in self.victims:
                still.append({"node": nid, "reason": "演练：还是联系不上"})
                continue
            method, kw = _HELD[p["token"]]
            getattr(self._route([nid]), method)(**kw)
        return still


# ===========================================================================
section("第一段 · store 层：补推成功与「一次就成功」必须算出同一本账")

CASES = {
    "上传": ("upload", (OWNER, KEY, PLAIN)),
    "改块": ("modify", (OWNER, KEY, 1, NEWBLK)),
    "追加": ("append", (OWNER, KEY, MORE)),
    "截断": ("truncate", (OWNER, KEY, 2)),
}
BASE = ("upload", (OWNER, KEY, PLAIN))
EXPECT_PLAIN = {
    "上传": PLAIN,
    "改块": PLAIN[:8] + NEWBLK + PLAIN[16:],
    "追加": PLAIN + MORE,
    "截断": PLAIN[:16],
}


def rig(seed: bytes):
    """一套确定性装置（同样的 seed ⇒ 同样的分片与同样的密钥）。

    返回 ``(session, 传输层, store)`` —— 分开返回是为了让"换一个协调者对象、
    但**沿用同一套节点**"（那就是重启）能写得出来。
    """
    session = new_session(l=256, n_max=64, modulus_bits=1024, seed=seed)
    fl = _Flaky(session, NODES)
    store = PlainKeyStore(
        session, node_ids=NODES, segment_bytes=SEG, transport=fl, replica_factor=2
    )
    return session, fl, store


def run(store, steps) -> None:
    for method, args in steps:
        getattr(store, method)(*args)


def snap(store) -> dict:
    """把账目快照成**纯数据**（这样两边可以直接比）。

    ★ **不比 IV、也不比 "谁持有哪块"**：

    * IV 是**随机**的（同一把密钥下让密文各不相同，这正是它的用处）；
    * 分到哪台会**先打乱再轮转**（见 ``_plan``），两次独立运行本来就可以不同
      —— 它只影响分发，**不影响密码学**。

      两项都不属于"算出来的账"。真正该逐位相同的是：占了哪些下标、每块多长、
      总长、内容指纹，以及摘要里的长度/起点/位置段/身份与自检结果。

    ★ **也不比 :math:`U, C`**：它们是密文的承诺，而密文随**随机 IV** 变 ——
      两次独立运行本来就不一样。（“补推后的明文”与“自检全绿”两条断言已经把
      这一块盖住了：自检就是拿密文重新算一遍承诺去比。）
    """
    return {
        "n": store.n,
        "files": {
            f"{o}/{k}": (
                tuple(r.indices),
                list(r.plain_lengths),
                r.total_bytes,
                r.segment_bytes,
                r.content_digest,
            )
            for (o, k), r in sorted(store.files.items())
        },
        "delta": {
            f"{o}/{k}": (
                int(d.n), int(d.offset),
                [[int(a), int(b)] for a, b in d.chunks],
                str(getattr(d, "identity", "") or ""),
            )
            for (o, k), d in sorted(store.deltas.items())
        },
        "check": list(store.check()),
    }


# ===========================================================================
def snap_diff(a: dict, b: dict) -> str:
    """两边快照不一样时，把这不一样指出来（否则只看一个 FAIL 很难查）。"""
    out: list[str] = []
    if a["n"] != b["n"]:
        out.append(f"n: {a['n']} != {b['n']}")
    for key in ("files", "delta"):
        for k in sorted(set(a[key]) | set(b[key])):
            if a[key].get(k) != b[key].get(k):
                out.append(f"{key}[{k}]: {a[key].get(k)} != {b[key].get(k)}")
    if a["check"] != b["check"]:
        out.append(f"check: {a['check'][:2]} != {b['check'][:2]}")
    return " | ".join(out)[:280]


for name, (last_method, last_args) in CASES.items():
    seed = f"pending-{last_method}".encode()
    steps = [BASE] if last_method == "upload" else [BASE, (last_method, last_args)]

    # A —— 一次就成功（自己一套干净装置）
    _sa, _fl_a, store_a = rig(seed)
    run(store_a, steps)
    snap_a = snap(store_a)
    check(f"[{name}] 基线：一次成功且明文对得上",
          store_a.read(OWNER, KEY) == EXPECT_PLAIN[name])

    # B —— 共用一套装置：让两台收不到，真失败，把现场导出来
    session_b, fl_b, store_b = rig(seed)
    run(store_b, steps[:-1])
    fl_b.trip()
    ok, msg = raises(lambda: run(store_b, steps[-1:]), WriteError)
    check(f"[{name}] 两台掉线：写如约失败（WriteError）", ok, msg[:70])
    data = store_b.export_pending()
    check(f"[{name}] 失败之后留下了现场", data is not None)
    check(f"[{name}] 现场记得是哪几台没跟上",
          [f["node"] for f in (data or {}).get("failures", [])] == list(VICTIMS),
          str((data or {}).get("failures")))
    try:
        site = json.loads(json.dumps(data, ensure_ascii=False))
        check(f"[{name}] 现场能 JSON 往返（落库/读回同一份）", True)
    except Exception as exc:  # noqa: BLE001
        site = None
        check(f"[{name}] 现场能 JSON 往返（落库/读回同一份）", False,
              f"{type(exc).__name__}: {exc}")
    if site is None:
        continue

    # C —— 把现场装回去，节点回来，补推
    #
    # ★ 上传走**真·跨对象**：新起一个协调者对象（登记表/账目全空 —— 正是"从库里
    #   重建一份新文件"的样子），但**节点装置沿用 B 那一套**（节点是独立进程，
    #   协调者重启它们不跟着重启）。改块/追加/截断没法这样做：它们的起点是
    #   "库里有 BASE"，而 store 层没有库，重放 BASE 会往节点上再推一遍同一份
    #   （节点会以「S_K 不是当前版本的成员见证」拒绝）—— 所以那三种在**同一个
    #   协调者对象**上重装现场，比的是"装回去之后算出的账与一次成功是否相同"。
    #   跨重启那一段由第二段（manager 层，有真库）负责。
    if last_method == "upload":
        store_c = PlainKeyStore(
            session_b, node_ids=NODES, segment_bytes=SEG, transport=fl_b, replica_factor=2
        )
        # ★ 块密钥本来就是这么过重启的：真系统里它们被所有者公钥包成密文、
        #   存进 ``key_ct``（落库那一步），重启后解封再用。``PlainKeyStore``
        #   不上库，所以这里手工把那份"内存明文"搬过去 —— 模拟的是同一件事。
        store_c._plain_keys.update(store_b._plain_keys)
        how = "跨对象重装"
    else:
        store_c = store_b
        how = "同对象重装"
    store_c.import_pending(site)
    check(f"[{name}] 恢复后：确实有待补推", store_c.pending_write() is not None)
    fl_b.untrip()
    out = store_c.retry_pending()
    check(f"[{name}] 补推：收敛（ok=True）", out.get("ok") is True, str(out)[:80])
    check(f"[{name}] 补推：待补推清空", store_c.pending_write() is None)
    check(f"[{name}] ★★ {how}：补推后的账 == 一次成功的账",
          snap(store_c) == snap_a, snap_diff(snap(store_c), snap_a))
    check(f"[{name}] ★★ 补推后的明文 == 一次成功的明文",
          store_c.read(OWNER, KEY) == EXPECT_PLAIN[name])


section("第一段 · 收尾：没有收尾说明（restore）的现场，拒绝恢复")

_sd, _fld, store_d = rig(b"pending-refuse")
_h, _ = raises(
    lambda: store_d.import_pending({
        "op": "append", "failures": [], "payloads": {}, "kind": "add",
        "file_id": [OWNER, KEY], "K": [0], "elements": ["1"], "per_index": {},
        "delta_new": {"U": "1", "C": "1", "n": 1, "offset": 0, "chunks": [], "identity": ""},
        "restore": {},
    }),
    ValueError,
)
check("没有 restore 的现场：拒绝恢复（不造假收敛）", _h)
check("空装置：本来就没有现场", store_d.export_pending() is None)


# ===========================================================================
section("第二段 · manager 层：真重启")

from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.manager import StoreManager  # noqa: E402
from backend.models import PendingWriteRow, UserRow  # noqa: E402
from backend.security import hash_password  # noqa: E402

#: manager 这一层要过 ``_check_segment_bytes``，块大小下限是 64 字节。
SEG_M = 64
PLAIN_OK = b"A" * SEG_M * 3          # 3 块（轮转下会落到那两台身上）
PLAIN_BIG = b"B" * SEG_M * 4         # 4 块

#: **节点那一侧**：整份脚本里只有一套 —— 协调者重启，节点不动。
_NODE_RIG: list[_Flaky] = []


class _Mgr(StoreManager):
    """写推接到"能让某几台收不到"的假传输层上；不装内置演练（本脚本自己就是演练）。"""

    def _reload(self, store, db):
        # ★ 在这里换传输层，而不是在 ``_make_transport`` 里造一个：那个时点
        #   ``sess`` 还没建出来，而**会话必须与协调者自己那份是同一套参数**
        #   （凭猜的 seed 造出来的 CRS 会让节点回一句「S_I 校验失败」——
        #   正是本会话一开始踩过的那个坑）。到了 ``_reload`` 这一步，
        #   ``store.session`` 就是权威那一份，直接拿它建节点。
        #   而且这一步在 ``_verify_nodes_at_startup`` **之前**，所以守卫
        #   看到的就是这套节点。
        if not _NODE_RIG:
            _NODE_RIG.append(_Flaky(store.session, NODES))
        store.transport = _NODE_RIG[0]
        super()._reload(store, db)

    def _install_fault_drill(self, store):  # 本脚本自带演练，别叠一层
        return


def fresh():
    """从**只存在于磁盘上的那个库**建一套全新的对象 —— 这就是"重启"。"""
    settings = Settings()
    settings.db_path = DB_FILE
    settings.node_ids = NODES
    settings.node_urls = ()
    settings.retry_auto = False          # 别让后台线程跟断言抢着收敛
    db = Database(settings.database_url())
    mgr = _Mgr(settings, db)
    mgr.bootstrap()
    return settings, db, mgr


for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_FILE) + suffix)
    if p.exists():
        p.unlink()

settings, db, mgr = fresh()
with db.session() as s:
    s.add(UserRow(username=OWNER, pwd_hash=hash_password(PW), role="user", display_name="甲"))
    s.commit()
check("生成用户密钥对", mgr.ensure_user_key(OWNER, PW))
sk = mgr.unseal_user_key(OWNER, PW)

mgr.upload(OWNER, "ok1", PLAIN_OK, segment_bytes=SEG_M)
check("基线：先有一份好文件（免得闸门在 n=0 时直接返回）", True)
check("基线：读得回来",
      bytes.fromhex(mgr.read_plain(OWNER, "ok1", sk)["data_hex"]) == PLAIN_OK)

rig_now = mgr._require().transport
rig_now.trip()
ok, msg = raises(
    lambda: mgr.upload(OWNER, "big", PLAIN_BIG, segment_bytes=SEG_M), WriteError
)
check("两台掉线时上传：如约失败（WriteError）", ok, msg[:70])
check("★ 失败之后**盘上**留下现场", mgr._load_pending_site() is not None)
check("★ 状态视图如实报「有待补推」", mgr._require().pending_write() is not None)
check("★ 落库那半（key_cts）也挂上了", bool((mgr._finish_spec or {}).get("key_cts")))
check("还没重启：现场不是「恢复来的」", mgr._restored_site is False)

# ---- 重启 ---------------------------------------------------------------
db.engine.dispose()
db1 = db
try:
    settings2, db2, mgr2 = fresh()
    booted, boot_msg = True, ""
except Exception as exc:  # noqa: BLE001
    settings2 = db2 = mgr2 = None
    booted, boot_msg = False, f"{type(exc).__name__}: {exc}"

check("★★ 重启：**没有**以「存储节点与协调者不同步」拒绝启动", booted, boot_msg[:120])
if booted:
    store2 = mgr2._require()
    check("★ 重启后：现场被恢复（待补推还在）", store2.pending_write() is not None)
    check("★ 重启后：落库那半也恢复了（key_cts 在）",
          bool((mgr2._finish_spec or {}).get("key_cts")))
    check("★ 重启后：现场标记为「从盘上恢复」", mgr2._restored_site is True)
    check("★ 重启后：守卫记下了「已经跑在前面」的节点",
          len(mgr2._startup_ahead) == len(VICTIMS), str(mgr2._startup_ahead))
    check("★ 重启后：留了一句给用户看的话", bool(mgr2._site_note), mgr2._site_note)

    # ---- 节点回来 + 补推 -------------------------------------------------
    rig_now.untrip()
    out = mgr2.retry_push(manual=True)
    check("补推：收敛（ok=True）", out.get("ok") is True, str(out)[:100])
    check("补推：待补推清空", out.get("pending") is None)
    check("补推：落库那一步补上了（没有 persist_pending 欠账）",
          out.get("persist_pending") is False, str(out.get("persist_error", ""))[:80])
    got = bytes.fromhex(mgr2.read_plain(OWNER, "big", sk)["data_hex"])
    check("★★ 补推之后：明文逐位相同", got == PLAIN_BIG,
          f"{len(got)} vs {len(PLAIN_BIG)} 字节")
    check("★ 补推之后：盘上现场被抹掉", mgr2._load_pending_site() is None)
    check("补推之后：自检全绿", mgr2._require().check() == [],
          str(mgr2._require().check()[:2]))
    with db2.session() as s:
        check("补推之后：pending_write 表是空的",
              s.get(PendingWriteRow, 1) is None)
    # 再重启一次：这一次应当**干净启动**（没有现场、没有跑在前面的节点）
    db2.engine.dispose()
    settings3, db3, mgr3 = fresh()
    check("★ 再重启一次：干净启动，且没有恢复任何现场",
          mgr3._restored_site is False and mgr3._load_pending_site() is None)
    check("★ 再重启一次：读得回来（补推真的落到了库里）",
          bytes.fromhex(mgr3.read_plain(OWNER, "big", sk)["data_hex"]) == PLAIN_BIG)
    db3.engine.dispose()


# ===========================================================================
section("第二段 · 陈旧现场：节点其实已经对齐 ⇒ 判定陈旧并丢弃")

settings4, db4, mgr4 = fresh()
stale = {
    "op": "append", "failures": [], "payloads": {}, "kind": "add",
    "file_id": [OWNER, "ghost"], "K": [0], "elements": ["1"], "per_index": {},
    # ★ offset 挑一个**任何节点都不会报**的：于是没有节点"跑在前面" ⇒ 陈旧
    "delta_new": {"U": "1", "C": "1", "n": 1, "offset": 99999,
                  "chunks": [], "identity": ""},
    "restore": {"kind": "file", "file_id": [OWNER, "ghost"], "K": [0],
                "record": {"owner": OWNER, "file_key": "ghost", "indices": [0],
                           "ivs": ["00000000000000000000000000000000"],
                           "plain_lengths": [1], "total_bytes": 1,
                           "segment_bytes": SEG, "content_digest": "x"}},
}
with db4.session() as s:
    s.add(PendingWriteRow(id=1, payload=json.dumps(stale, ensure_ascii=False)))
    s.commit()

db4.engine.dispose()
try:
    _s5, db5, mgr5 = fresh()
    check("陈旧现场：启动没被它挡住", True)
except Exception as exc:  # noqa: BLE001
    db5, mgr5 = None, None
    check("陈旧现场：启动没被它挡住", False, f"{type(exc).__name__}: {exc}")
if mgr5 is not None:
    check("陈旧现场：没有恢复它", mgr5._restored_site is False)
    check("陈旧现场：盘上那一行也抹掉了", mgr5._load_pending_site() is None)
    check("陈旧现场：留了一句实话", "陈旧" in (mgr5._site_note or ""), mgr5._site_note)
    check("陈旧现场：没有凭空冒出待补推", mgr5._require().pending_write() is None)
    db5.engine.dispose()


# ===========================================================================
section("第二段 · 缺「落库那半」的现场 ⇒ 拒绝恢复（宁可人工，不要假收敛）")

settings6, db6, mgr6 = fresh()
mgr6._startup_ahead = ["n1"]        # 假装"确实有节点跑在前面"
before = mgr6._require().n
mgr6._restore_pending_site(mgr6._require(), dict(stale, finish=None))
check("缺落库那半：拒绝恢复", mgr6._restored_site is False)
check("缺落库那半：没有把向量现场装进来（store 没被动过）",
      mgr6._require().pending_write() is None and mgr6._require().n == before)
check("缺落库那半：留了一句实话", "落库" in (mgr6._site_note or ""), mgr6._site_note)
db6.engine.dispose()


# ===========================================================================
# 收尾：临时库用完即删（**先放开引擎**，否则 Windows 上文件句柄还在）
for _name in ("db", "db2", "db3", "db4", "db5", "db6"):
    _o = globals().get(_name)
    if _o is not None:
        try:
            _o.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
for _suffix in ("", "-wal", "-shm"):
    _p = Path(str(DB_FILE) + _suffix)
    if _p.exists():
        try:
            _p.unlink()
            print(f"[cleanup] 已删除临时库 {_p.name}")
        except OSError as _exc:  # noqa: PERF203
            print(f"[cleanup] 删不掉 {_p.name}：{_exc}")

print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 条未通过 ✗")
    for f in FAILURES:
        print("   - " + f)
else:
    print("结果：ALL PASS —— 待补推现场能跨重启活下来，且补推算出的是同一本账")
print("=" * 72)
sys.exit(1 if FAILURES else 0)
