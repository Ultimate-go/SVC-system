"""``StoreManager`` —— 把 :class:`~core.store.VectorStore` 与 SQLite 缝在一起。

职责边界
--------
**内存里的 ``VectorStore`` 是权威，数据库是写穿日志。** 数据库不参与任何
代数运算 —— 它只负责"重启后能把向量原样重建出来"。所以本模块里没有一行
模幂、没有一行群运算，只有搬运。

重建的正确性由 ``store.check()`` 兜底：它会把重建出来的增量摘要与
"对整条向量做一次 ``commit``" 的结果逐位比对。有测试钉住这一点。

并发
----
一把可重入锁罩住所有会改状态的操作。**因此后端必须单进程单 worker**
（``uvicorn backend.main:app``，不要加 ``--workers``），
否则多个进程各有一份内存状态，会互相覆盖。演示规模下这是正确的取舍。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from time import perf_counter
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy import delete, func, select

from core import GlobalSession, VectorStore, crs_from_dict, new_session
from core.crypto import split_segments
from core.keywrap import (
    KeyWrapError,
    KeyWrapFormatError,
    KeyWrapIntegrityError,
    make_user_keypair,
    public_bytes,
    public_from_bytes,
    unwrap_key,
    unwrap_private_key,
    wrap_key,
    # ★ ``rewrap_user_key`` 要用它来重封用户私钥。以前漏了这一个名字，
    #  于是「改自己的口令」每次都 NameError → 500，而且哈希已经先落库了
    #  （见 ``admin.patch_user`` 的注释）——账号当场锁死。
    wrap_private_key,
)
from core.store import proof_bytes
from core.timing import stage
from core.transport import NodeTransport, TransportError, WriteError
from svc import Opening, VerifyCode, as_index_set
from svc import disagg as svc_disagg
from svc import verify as svc_verify
from svc import verify_batch as svc_verify_batch
from vds.client_node import ClientNode
from vds.digest import Digest
from vds.pos import DEFAULT_LAMBDA_POS

from .config import Settings
from .db import Database
from .models import (
    BlockRow,
    CrsRow,
    FileRow,
    GlobalRow,
    NodeBlobRow,
    NodeStateRow,
    UserRow,
)

__all__ = [
    "StoreManager",
    "Conflict",
    "NotFound",
    "OutOfRange",
    "DecryptDenied",
]

G_DELTA_U = "delta_U"
G_DELTA_C = "delta_C"
G_DELTA_N = "delta_n"

#: 分片轮转的偏移量（模节点台数）。
#:
#: 必须落库：不存的话重启后偏移归零，之后再传小文件又永远从第 1、2 台
#: 开始堆，后几台长期空闲 —— 而 :meth:`VectorStore._plan` 的注释
#: 明确承诺了"连续多次小上传也能覆盖到所有服务器"。
G_NODE_OFFSET = "node_offset"


# 密钥模型（原先这里是"用哪个 ABE 实现"的说明，连同 ``VDS_ABE_SCHEME`` 一
# 起删了）：
#
#   * 每个用户一对 SM2 密钥；
#   * 公钥在 ``users.pub_key``，明文，本来就无需保护；
#   * 私钥用**用户口令**包一层，密文在 ``users.sk_wrapped``，从不明文落库；
#   * 块密钥用**文件所有者的公钥**封装，密文在 ``blocks.key_ct``。
#
# 因此不再有"换实现等于历史密文作废"这种全局旋钮 —— 格式标识写在密文里
# （``core.keywrap.KIND_BLOCK``），每段密文自己说自己是什么格式。


def verify_code_name(code: int | VerifyCode) -> str:
    r"""把验证失败环节的**枚举值**翻成环节名（``OK`` / ``BAD_SHAPE`` / ``BAD_S_I`` / ``BAD_LAMBDA``）。

    为什么要单独给一个名字：``verify.code`` 本身只是个整数（2、3），
    界面想显示"是哪一步没过"就得自己查表 —— 而前后端各写一张表必然分叉
    （本项目已经吃过一次：策略解析器写了两套）。这里名字**直接取自枚举**，
    所以最多"多一个/少一个"，不会"写成另一个意思"。

    未知值不抛异常：这是只读查询响应里的一个**展示字段**，
    为一个没见过的整数把整条验证路径打挂不值得。名字里带上原数字，一眼看得出来。
    """
    try:
        return VerifyCode(int(code)).name
    except ValueError:
        return f"UNKNOWN_{int(code)}"


class Conflict(Exception):
    """资源已存在 / 状态冲突。"""


class NotFound(Exception):
    """资源不存在。"""


class OutOfRange(Exception):
    """请求超出当前向量范围，或超出全局位置上限。"""


class DecryptDenied(Exception):
    """**密码学那道门**拒绝了：块密钥解不开。

    它与"无权解密该文件"（应用层那道门）是**两回事**，必须分开报 ——
    规划 §8.6 把访问控制拆成三道门，这两句拒绝话术正好对应其中两道。
    合并成一句话就把课讲不清楚了。

    ★ 删掉 ABE 之后这道门**反而更硬**了：以前"属性满不满足策略"是拿一段
      策略串现算的，现在直接是"你这把私钥能不能解开这段密文" ——
      **算不算得出来，不是查表查出来的**。
    """


class StoreManager:
    """全局向量的持久化外壳。"""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.db = database
        self._lock = threading.RLock()
        self.session: GlobalSession | None = None
        self.store: VectorStore | None = None
        #: 会话私钥：``令牌 -> (用户名, SM2 私钥标量)``。
        #:
        #: ★ 只在内存里，只在**登录那一刻**从 ``users.sk_wrapped`` 用口令
        #:   解封出来。不落盘、不进 JWT、不进日志。后端重启就没了 ——
        #:   那正是想要的：**"持有私钥"必须与"知道口令"绑定，
        #:   而不是与"手上有那个库"绑定**。
        self._keys: dict[str, tuple[str, int]] = {}
        #: 同时只保留这么多个会话私钥，超了丢掉最早的（防长期运行无限增长）。
        self._max_sessions: int = 64
        #: 写推失败时“还欠一步落库”的那个回调（见 :meth:`retry_push`）。
        #:
        #: ★ 为什么要有它：向量层的现场（``store._pending``）能保证
        #:   **补推之后向量是自洽的**，但库里还什么都没有 —— 而重启时
        #:   是从库重建向量的。少了这一步，补推反而会把系统搞成
        #:   “重启就报节点与协调者不同步”。它是一个**闭包**，
        #:   捕获了那次操作的全部参数（策略、块密钥密文、文件标识…）。
        self._finish_cb: Callable[[], object] | None = None
        #: 自动补推的后台线程与它的计数（见 :meth:`_kick_auto_retry`）。
        self._auto_thread: threading.Thread | None = None
        self._auto_attempts: int = 0
        self._auto_note: str = ""

    # -------------------------------------------------------------------
    # 启动 / 重建
    # -------------------------------------------------------------------

    def bootstrap(self) -> None:
        """建库（若无）并重建向量。**不重新生成 N、g** —— 那会让验证必然失败。"""
        with self._lock:
            self.settings.check_node_config()
            self.db.create_all()
            with self.db.session() as db:
                crs_row = db.get(CrsRow, 1)
                if crs_row is None:
                    sess = new_session(
                        l=self.settings.l,
                        n_max=self.settings.n_max,
                        modulus_bits=self.settings.modulus_bits,
                        seed=self.settings.crs_seed,
                    )
                    db.add(
                        CrsRow(
                            id=1,
                            N=str(sess.crs.N),
                            g=str(sess.crs.g),
                            l=sess.l,
                            n_max=sess.n_max,
                        )
                    )
                    db.commit()
                else:
                    sess = GlobalSession(crs_from_dict(crs_row.to_crs_dict()))

                store = VectorStore(
                    sess,
                    node_ids=self.settings.node_ids,
                    segment_bytes=self.settings.segment_bytes,
                    transport=self._make_transport(),
                    replica_factor=self.settings.replica_factor,
                )
                try:
                    store.set_crs()
                except TransportError as exc:
                    raise RuntimeError(
                        f"无法把公开参数交给存储节点：{exc}\n"
                        f"  （节点没全部起来？或者某台节点已经存了另一套 N、g？）"
                    ) from exc
                self._reload(store, db)
                self._verify_nodes_at_startup(store)

            self.session, self.store = sess, store

    # -------------------------------------------------------------------
    # 用户密钥（取代了原先的“ABE 参数”那一节）
    #
    # 三层各管各的：
    #   ① ``core.keywrap``      —— 纯密码学，不知道数据库存在；
    #   ② 这里                  —— 把密钥与 ``users`` 表缝起来；
    #   ③ ``routers/auth``      —— 登录时用口令解封一次。
    # -------------------------------------------------------------------

    def _user_row(self, db, username: str) -> UserRow:
        row = db.execute(
            select(UserRow).where(UserRow.username == username)
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"用户 {username} 不存在")
        return row

    def ensure_user_key(self, username: str, password: str) -> bool:
        """确保这个用户有密钥对；没有就**用他的登录口令**生成并封好。

        :returns: 真的新生成了才返回 ``True``。

        ★ 已经有密钥对就**什么都不做**。绝不能默默换一个新的：
          私钥一换，他以前传的文件会**全部解不开**（块密钥是用旧公钥封的）。

        调用时机：建用户时、seed 灌演示账号时。
        私钥**不返回给调用方**，只有密文落库 —— 调用方拿不到明文私钥，
        也就没法“顺手”把它存到别的地方去。
        """
        with self._lock, self.db.session() as db:
            row = self._user_row(db, username)
            if row.pub_key and row.sk_wrapped:
                return False
            with stage("生成密钥对并封装私钥"):
                _sk, pk, blob = make_user_keypair(password)
            row.pub_key = public_bytes(pk).hex()
            row.sk_wrapped = json.dumps(blob, ensure_ascii=False)
            db.commit()
            return True

    def rewrap_user_key(
        self,
        username: str,
        old_sk: int,
        new_password: str,
        *,
        db: object | None = None,
    ) -> None:
        """改口令时**重新封装**私钥（换口令不该让旧文件失效）。

        ★ 这正是“口令封装”相对“直接用口令派生私钥”的关键好处：
          私钥本身没变，只是外面那层包裹换了个口令 —— 所以已上传的文件
          一把都不用重传。

        :param old_sk: 调用方先前解封出来的私钥（应当来自 :meth:`unseal_user_key`）。
        :param db: 给了就**只写不提交** —— 让调用方把「新哈希 + 新私钥密文」
            放进**同一个事务**里。

            ★★ 为什么非得能传进来：这两样东西必须同生同死。分开提交的话，
            中间任何一步失败都会留下「口令与私钥包裹不同步」的账号 ——
            那样的账号**两个口令都登不进去**（新口令解不开私钥、旧口令过不了
            哈希校验），而私钥其实好好地躺在库里。
        """

        def _write(session: object) -> None:
            row = self._user_row(session, username)
            with stage("重新封装私钥（PBKDF2-HMAC-SM3）"):
                row.sk_wrapped = json.dumps(
                    wrap_private_key(new_password, old_sk), ensure_ascii=False
                )

        if db is not None:
            _write(db)
            return
        with self._lock, self.db.session() as own:
            _write(own)
            own.commit()

    def user_public_key(self, username: str):
        """取用户的 SM2 公钥点 —— 上传/改块/追加时用它封块密钥。"""
        with self.db.session() as db:
            row = self._user_row(db, username)
            if not row.pub_key:
                raise NotFound(
                    f"用户 {username} 还没有密钥对 —— 无法把块密钥封给他。\n"
                    f"  （建用户时没生成？用 scripts/seed.py --reset 重建，"
                    f"或让管理员删掉重建这个用户）"
                )
            return public_from_bytes(bytes.fromhex(row.pub_key))

    def unseal_user_key(self, username: str, password: str) -> int:
        """用登录口令把用户的 SM2 私钥解封出来（**只在登录时调一次**）。

        :raises KeyWrapIntegrityError: 口令不对、或者密文被改过。
            这两件事在密码学上分不开，也不应该分开 —— 分开就是一个
            “口令对不对”的 oracle。
        """
        with self.db.session() as db:
            row = self._user_row(db, username)
            if not row.sk_wrapped:
                raise NotFound(f"用户 {username} 还没有密钥对")
            # ★ 口令拉伸是登录里最贵的一步（生产档 20 万次，本机约 0.1 秒）。
            #   单独列一栏，免得被当成"后端慢" —— 慢是**故意**的，
            #   抬高暴力破解的代价。
            #   注：阶段名里**不写"20 万次"** —— 迭代数是可配置的
            #   （测试档就是 2000），写死一个可能不准的数比不写更糟。
            with stage("解封私钥（PBKDF2-HMAC-SM3）"):
                return unwrap_private_key(password, json.loads(row.sk_wrapped))

    # -- 会话私钥（只在内存，只在登录那一刻存在） -------------------------

    def remember_key(self, token: str, username: str, sk: int) -> None:
        """把解封出来的私钥挂到这次令牌上。**只在内存**。"""
        with self._lock:
            self._keys[token] = (username, sk)
            # 丢最早的（dict 保插入序）。演示规模下不会到上限，
            # 但后端一跑一整天的活，不设上限就是漏内存。
            while len(self._keys) > self._max_sessions:
                self._keys.pop(next(iter(self._keys)), None)

    def forget_key(self, token: str) -> None:
        """丢掉这次令牌的私钥（退出登录时调）。"""
        with self._lock:
            self._keys.pop(token or "", None)

    def session_key_of(self, token: str) -> int | None:
        """这次令牌对应的私钥；没登录过、或后端重启过，就是 ``None``。"""
        got = self._keys.get(token or "")
        return None if got is None else got[1]

    def keywrap_status(self) -> dict:
        """给界面看的密钥模型现状。**故意把“私钥在哪”说清楚**。"""
        with self.db.session() as db:
            users = list(db.execute(select(UserRow)).scalars())
        return {
            "scheme": "SM2-ECIES（封块密钥）+ PBKDF2-HMAC-SM3（封用户私钥）",
            "users": len(users),
            "user_keys": sum(1 for u in users if u.pub_key and u.sk_wrapped),
            "sessions_with_key": len(self._keys),
            "private_key_storage": "用登录口令派生的 KEK 封装后入库（users.sk_wrapped）",
            "note": (
                "块密钥用文件所有者的 SM2 公钥封装；用户私钥用他自己的口令"
                "包一层之后才入库。口令本身不落库、私钥明文从不落盘 ——"
                "所以“数据库被拿走”拿到的只有解不开的密文。"
            ),
        }

    def _check_segment_bytes(self, value: int | None) -> int:
        """把"这次上传要切多大一块"定下来，并做范围校验。

        ``None`` = 用部署默认值（``Settings.segment_bytes``）。

        为什么要**服务端**校而不是信前端那几个档：这个值决定块数，
        而块数又决定会不会撞上先定死的位置上限 ``n_max``。
        选 1 字节一块时，任何文件都会撞上限，而那时的报错看起来像
        "文件太大"，根本指不到"块选小了" —— 所以要在入口就把范围说清。
        """
        seg = self.settings.segment_bytes if value is None else int(value)
        lo = self.settings.segment_bytes_min
        hi = self.settings.segment_bytes_max
        if not (lo <= seg <= hi):
            raise OutOfRange(
                f"块大小 {seg} 字节超出允许范围 {lo}~{hi}。"
                f"块越小 → 块数越多（受全局位置上限约束，n_max = "
                f"{self.settings.n_max}），单块改起来越省；"
                f"块越大 → 块数越少，上传与检索越快。"
            )
        return seg

    def delta_fingerprint(self) -> str:
        """当前 δ 的短指纹（12 位十六进制）: ``sha256("U:C:n")[:12]``。

        为什么必须有它、而不能只看 ``n``：**改块不改变 n，却会改变承诺 C**
        （见 :meth:`modify_block`）。证据是 δ 的函数，所以"n 没变"**不等于**
        "证据还有效"。界面要回答"这张卡作废没有"，只能按指纹判。

        它不是密码学承诺，只是给界面用的一把尺子。
        """
        store = self._require()
        raw = f"{store.delta.U}:{store.delta.C}:{store.delta.n}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _make_transport(self) -> NodeTransport | None:
        """没配 ``node_urls`` → 单进程模式（返回 ``None``，由 ``VectorStore`` 自建）。"""
        if not self.settings.node_urls:
            return None
        if not self.settings.node_token:
            # 不去"降级成不鉴权"：节点那边令牌是必传的，没令牌只会一堆 401。
            # 与其等它们在调用时才炸，不如启动就说清缺哪个变量。
            raise RuntimeError(
                "跨进程模式必须同时设 VDS_NODE_TOKEN：\n"
                "  节点一律要求鉴权（否则 /node/reset 与 /node/retrieve 就只是\n"
                "  靠绑回环护着，而绑哪个地址是部署参数）。\n"
                "  用 scripts/run_nodes.py 起节点时，它会把这行打印出来。"
            )
        # 延迟导入：单进程模式不装 httpx 也能跑
        from node_service import HttpTransport

        return HttpTransport(
            self.settings.node_urls,
            token=self.settings.node_token,
            retries=self.settings.node_retries,
            backoff=self.settings.node_retry_backoff,
        )

    def _verify_nodes_at_startup(self, store: VectorStore) -> None:
        """启动时确认各节点与协调者停在同一个 ``n`` —— **两种模式都要查**。

        两种混用都必须拦住，而且**反方向那种更隐蔽**：

        * 跨进程模式读单进程的库 → 各节点的数据目录是空的；
        * 单进程模式读跨进程的库 → 协调者库里没有节点状态（跨进程模式下
          节点状态存在各节点自己的库里，``_persist_nodes`` 直接 return 了）。

        后者的表现是"能登录、能看到文件列表、一查询才 400" ——
        最难查的那种半死不活。宁可启动就拒，并且把原因说清楚。
        """
        if store.n == 0:
            return

        bad: list[str] = []
        down: list[str] = []
        for row in store.transport.report():
            if row.get("unreachable"):
                down.append(row["node_id"])
                continue
            if row["n"] != store.n:
                bad.append(f"{row['node_id']} 停在 n={row['n']}，协调者在 n={store.n}")
            elif not row["valid"]:
                bad.append(f"{row['node_id']} 声称持有的下标与实际密文对不上")

        if not bad and len(down) == len(store.node_ids):
            # 一台都没联系上 ⇒ 根本没东西可查，而且**令牌配错也是这个表现**
            # （每个请求都 401）。这种情况不能放过去。
            raise RuntimeError(
                "所有存储节点都联系不上（"
                + ", ".join(down)
                + "）—— 节点没起来？端口写错？或者 VDS_NODE_TOKEN 不一致？"
            )
        if not bad:
            # 部分节点掉线**不再拒绝启动**：开了副本时这是应当被容忍的状态，
            # 而“哪台掉了”在界面的存储节点页上有体现（unreachable）。
            return

        if self.settings.distributed:
            hint = "（节点数据目录丢了？或者这份库是用单进程模式写的？）"
        else:
            hint = (
                "（这份库是用跨进程模式写的：那时节点状态存在各节点自己的库里，"
                "协调者库这边是空的。请用 scripts/seed.py --reset 重建，"
                "或者改回跨进程模式启动）"
            )
        raise RuntimeError(
            "存储节点与协调者不同步，拒绝启动：\n  "
            + "\n  ".join(bad)
            + "\n  "
            + hint
        )

    def _reload(self, store: VectorStore, db) -> None:
        """从库里的行重建整个向量。"""
        g = {r.key: r.value for r in db.execute(select(GlobalRow)).scalars()}
        store.delta = Digest(
            U=int(g.get(G_DELTA_U, str(store.session.crs.g))),
            C=int(g.get(G_DELTA_C, "1")),
            n=int(g.get(G_DELTA_N, "0")),
        )
        # 分片偏移也要恢复。取模是为了容错：节点台数改过的话，
        # 旧偏移未必落在合法范围内，取模总比抛异常好（它只影响均匀度）。
        store._offset = int(g.get(G_NODE_OFFSET, "0")) % max(1, len(store.node_ids))

        buckets: dict[int, list[BlockRow]] = {}
        for b in db.execute(select(BlockRow).order_by(BlockRow.global_index)).scalars():
            idx = store.registry.alloc_block(b.owner, b.file_key, b.block_idx)
            if idx != b.global_index:
                raise RuntimeError(
                    f"登记表重建错位：期望下标 {b.global_index}，得到 {idx}"
                )
            store.values[idx] = int(b.element)
            store._holder[idx] = b.holder
            # 老行没写过 replicas（默认 "[]"）⇒ 退化成“只有主副本一份”。
            # 这样开副本之前传的文件照样能读，不会被新特性卡住。
            copies = tuple(json.loads(b.replicas or "[]")) or (b.holder,)
            if copies[0] != b.holder:
                raise RuntimeError(
                    f"下标 {idx} 的副本列表 {list(copies)} 与主副本 {b.holder!r} 不一致"
                )
            store._replicas[idx] = copies
            buckets.setdefault(b.file_id, []).append(b)

        for fid, rows in buckets.items():
            rows.sort(key=lambda r: r.block_idx)
            first = rows[0]
            frow = db.get(FileRow, fid)
            store.files[(first.owner, first.file_key)] = _file_record(first, rows, frow)

        self._restore_nodes(store, db)

    def _restore_nodes(self, store: VectorStore, db) -> None:
        """把节点状态交还给它自己。

        **协调者不解释这些字段的含义** —— 它只负责把库里那几行搬给
        ``transport``，由节点那边自己重建 :class:`~core.node_state.NodeState`。
        这样"节点的状态归节点"这条边界在代码上也成立。
        """
        states: dict[str, dict] = {}
        for r in db.execute(select(NodeStateRow)).scalars():
            states[r.node_id] = {
                "delta": (r.delta_U, r.delta_C, r.delta_n),
                "st": (r.S_I, r.Lambda_I),
                "I": r.I,
                "FI": r.FI,
                "blobs": {},
            }
        for b in db.execute(select(NodeBlobRow)).scalars():
            if b.node_id in states:
                states[b.node_id]["blobs"][b.global_index] = b.ciphertext

        importer = getattr(store.transport, "import_states", None)
        if states and importer is None:
            raise RuntimeError(
                "库里还留着节点状态，但当前跑的是跨进程模式 —— "
                "两种模式的库不能混用（节点状态应当存在各节点自己的库里）。"
                "请用 scripts/seed.py --reset 清库并重新上传。"
            )
        if importer is not None:
            importer(states)

    # -------------------------------------------------------------------
    # 写穿
    # -------------------------------------------------------------------

    def _persist_globals(self, db) -> None:
        store = self._require()
        for key, value in (
            (G_DELTA_U, store.delta.U),
            (G_DELTA_C, store.delta.C),
            (G_DELTA_N, store.delta.n),
            (G_NODE_OFFSET, store._offset),
        ):
            row = db.get(GlobalRow, key)
            if row is None:
                db.add(GlobalRow(key=key, value=str(value)))
            else:
                row.value = str(value)

    def _persist_nodes(self, db, indices: Sequence[int]) -> None:
        """落盘节点状态（**仅本地模式**）；跨进程时节点自己管。

        ``indices`` 是**这次变更涉及的**下标 —— 只写这几行的密文，
        不然每次上传都要重写整张 ``node_blobs``，n=1024 时就是 1024 行。

        .. important::

           它是 **upsert**，不是纯插入。改块改的是**已有**下标，
           而 ``node_blobs`` 的主键是 ``(node_id, global_index)`` ——
           拿 ``db.add`` 去写就会直接撞主键。

           而且漏写这类问题**不会当场暴露**：进程内的状态是对的，
           要等重启后从库里重建，才会以「第 1 层块哈希对不上、
           而第 2 层承诺照样通过」这种极难归因的形式冒出来。
        """
        store = self._require()
        exporter = getattr(store.transport, "export_states", None)
        if exporter is None:
            return
        snap = exporter()

        db.execute(delete(NodeStateRow))
        for nid, blob in snap.items():
            U, C, n = blob["delta"]
            S_I, Lam = blob["st"]
            db.add(
                NodeStateRow(
                    node_id=nid,
                    delta_U=str(U),
                    delta_C=str(C),
                    delta_n=int(n),
                    S_I=str(S_I),
                    Lambda_I=str(Lam),
                    I_json=json.dumps(list(blob["I"])),
                    FI_json=json.dumps(list(blob["FI"])),
                )
            )
        for i in indices:
            gidx = int(i)
            # ★ 每一份副本都要落库。只写主副本的话，重启后别的副本拿到的
            #   还是旧密文 —— 改块时尤其致命（第 1 层块哈希会当场对不上）。
            for nid in store.replicas_of(gidx):
                ct = snap[nid]["blobs"][gidx]
                row = db.get(NodeBlobRow, (nid, gidx))
                if row is None:
                    db.add(
                        NodeBlobRow(node_id=nid, global_index=gidx, ciphertext=ct)
                    )
                else:
                    row.ciphertext = ct

    # -------------------------------------------------------------------
    # 上传
    # -------------------------------------------------------------------

    def upload(
        self,
        owner: str,
        file_key: str,
        data: bytes,
        *,
        segment_bytes: int | None = None,
        nodes: Sequence[str] | None = None,
    ) -> FileRow:
        """加密 → 分块 → 承诺 → **用所有者的公钥封装块密钥** → 分发，全部写穿到库。

        :param segment_bytes: 这份文件按多少字节切一块。``None`` = 用部署默认值
            （``Settings.segment_bytes``）。

            .. important::

               **这个值是逐文件记的**（``files.segment_bytes``）。所以它是
               “这份文件的形状”而不是“当时那个全局配置” —— 以后改部署默认值
               不会把老文件弄坏，改块与追加也都按这份文件自己的值走。

        :param nodes: **这份文件只往哪几台机器上摊**（手动指定分发）。
            ``None`` = 全部机器。校验在 :meth:`core.store.VectorStore._resolve_pool`：
            不认识的名字、重复、比副本数还少，都当场报错（不静默忽略 ——
            静默忽略会让用户以为“我限制了机器”，实际没有）。

            .. note::

               追加时**自动跟着这份文件已有的机器**，不用再传一次。

        :raises OutOfRange: 块大小超出允许范围，或块数超过 ``n_max``
        """
        with self._lock:
            store = self._require()
            if (owner, file_key) in store.files:
                raise Conflict(f"文件 {owner}/{file_key} 已存在")

            # ① 块大小先校：它决定块数，而块数又会决定块序号是否越界。
            seg_bytes = self._check_segment_bytes(segment_bytes)

            # ② 谁的钥匙：**所有者的公钥**。
            #    这一步就把"谁能解密"定死了 —— 封块密钥用的就是这把公钥，
            #    而别人的私钥对它无用。没有任何"策略串"参与，所以也没有
            #    "打错一个字就谁都读不了"那个失败模式了。
            pk_owner = self.user_public_key(owner)

            n0 = store.n
            key_cts: dict[int, dict] = {}

            def sink(pos: int, key: bytes) -> None:
                # ★ 明文 key 只在这里出现一瞬：包完立刻被丢弃。
                #   本函数不留副本 —— 延续 core/store.py 那条性质。
                with stage("封装块密钥（ECIES）"):
                    key_cts[pos] = wrap_key(pk_owner, key)

            def _finish() -> FileRow:
                """把这次上传**落库** —— 正常路径与补推路径共用这一段。

                ★ 为什么必须是闭包，而不是一段直写的代码：写推失败时协调者的
                  **内存**里已经把现场留住了（补推就能收敛），但库里还什么都
                  没有。若补推之后不补这一步，重启重建时会以"节点与协调者
                  不同步"被拦下 —— 看起来像"补推把系统修坏了"，实际是账没记全。
                  :meth:`retry_push` 补成功之后会把这里再调一次。

                ★ 它**只读** ``store`` 的当前状态（不依赖上面那个 ``rec`` 变量）：
                  两条路上拿到的必须是同一份数据，所以统一从 ``store.files`` 取。
                """
                done = store.files[(owner, file_key)]
                holders = {i: store.holder_of(i) for i in done.indices}
                with self.db.session() as db:
                    frow = FileRow(
                        owner=owner,
                        file_key=file_key,
                        segment_bytes=done.segment_bytes,
                        total_bytes=done.total_bytes,
                        content_digest=done.content_digest,
                        block_count=done.block_count,
                    )
                    db.add(frow)
                    db.flush()
                    for pos, gidx in enumerate(done.indices):
                        db.add(
                            BlockRow(
                                global_index=gidx,
                                file_id=frow.id,
                                owner=owner,
                                file_key=file_key,
                                block_idx=pos,
                                element=str(store.values[gidx]),
                                key_ct=json.dumps(key_cts[pos], ensure_ascii=False),
                                iv=done.ivs[pos],
                                plain_len=done.plain_lengths[pos],
                                holder=holders[gidx],
                                replicas=json.dumps(list(store.replicas_of(gidx))),
                            )
                        )
                    self._persist_globals(db)
                    self._persist_nodes(db, done.indices)
                    db.commit()
                    db.refresh(frow)
                return frow

            try:
                store.upload(
                    owner, file_key, data, key_sink=sink, segment_bytes=seg_bytes, nodes=nodes
                )
            except WriteError:
                # 向量那边已经把现场留住了（补推就能收敛）。
                # ★ 把"落库"这一步也挂到**同一份**现场上 —— 否则补推之后
                #   库还是空的，重启时会被"节点与协调者不同步"拦下。
                #
                #   条件判断不能省：只有**真的**留下了现场才挂。
                #   若传输层没带 payloads（`_pending is None`），重推根本无从谈起，
                #   这时候再"补落库"就会造出一个**假的收敛** —— 库里有文件、
                #   向量里却没有对应分量，下一句 store.check() 就当场抽你。
                if store.pending_write() is not None:
                    self._pending_finish = _finish
                raise
            except ValueError as exc:
                msg = str(exc)
                if "超过全局上限" in msg or "已存在" in msg:
                    raise (Conflict(msg) if "已存在" in msg else OutOfRange(msg)) from exc
                raise OutOfRange(msg) from exc

            with stage("落库（文件与块登记）"):
                frow = _finish()
            # 记一下这次上传把全局向量推到了哪里，方便排查
            assert store.n == n0 + frow.block_count
            return frow

    # -------------------------------------------------------------------
    # 修改（改一块）
    # -------------------------------------------------------------------

    def modify_block(
        self,
        owner: str,
        file_key: str,
        block_idx: int,
        data: bytes,
    ) -> dict:
        """改**一块**：重新加密、用所有者公钥重新封装新密钥、让全网跟上新分量。

        为什么必须重新封装：块密钥是 :meth:`~core.store.VectorStore.modify`
        新生成的（旧密钥连旧密文一起作废），而密钥在整个系统里**只以密文
        形式存在**（``BlockRow.key_ct``），不重新封就等于新内容谁都解不开。

        **顺序是：先推给节点，再改库**，与 :meth:`upload` 一致。反过来的话，
        "库说改了、节点没收到"会让协调者比实际存储更乐观。因此这里
        **不能**把一个数据库事务从头开到尾 —— 中间那段是 CPU 密集的模幂，
        握着 SQLite 的写锁只会拖慢别人。
        """
        with self._lock:
            store = self._require()
            pk_owner = self.user_public_key(owner)
            rec = store.files.get((owner, file_key))
            if rec is None:
                raise NotFound(f"文件 {owner}/{file_key} 不存在")
            # 块序号先校：越界是"请求不对"（400），不是"找不到"（404）
            if not 0 <= block_idx < len(rec.indices):
                raise OutOfRange(
                    f"块号 {block_idx} 越界：{owner}/{file_key} 只有 "
                    f"{len(rec.indices)} 块（块序号从 0 开始）"
                )

            # ① 先读库：拿到这块的全局下标与所属文件
            with self.db.session() as db:
                brow = db.execute(
                    select(BlockRow).where(
                        BlockRow.owner == owner,
                        BlockRow.file_key == file_key,
                        BlockRow.block_idx == block_idx,
                    )
                ).scalar_one_or_none()
                if brow is None:  # pragma: no cover - 与上面的块数校重复，兼底
                    raise NotFound(f"文件 {owner}/{file_key} 没有第 {block_idx} 块的账目")
                gidx = brow.global_index
                file_id = brow.file_id

            # ② 真正改：新密钥在 sink 里就地包成密文，明文不留
            wrapped: dict[int, dict] = {}

            def sink(pos: int, key: bytes) -> None:
                with stage("封装块密钥（ECIES）"):
                    wrapped[pos] = wrap_key(pk_owner, key)

            def _finish() -> dict:
                """③ 再写库 —— 正常路径与补推路径共用（理由同 :meth:`upload`）。

                ★ 它**只读** ``store`` 当前状态：新 IV 与新长度也从中取，
                  所以补推路径不需要再传一遍那两个值。
                """
                cur = store.files[(owner, file_key)]
                iv = cur.ivs[block_idx]
                plain_len = cur.plain_lengths[block_idx]
                with self.db.session() as db:
                    row = db.get(BlockRow, gidx)
                    if row is None:  # pragma: no cover - 上面刚读到过
                        raise NotFound(f"块 {gidx} 的账目不见了")
                    row.element = str(store.values[gidx])
                    row.key_ct = json.dumps(wrapped[block_idx], ensure_ascii=False)
                    row.iv = iv
                    row.plain_len = plain_len
                    frow = db.get(FileRow, file_id)
                    assert frow is not None
                    # 版本号 +1 —— 防重放的那个序号
                    frow.version += 1
                    frow.total_bytes = cur.total_bytes
                    self._persist_globals(db)
                    # ★ 节点那一侧也要落库：新密文 + 它自己的 (δ, S_I, Λ_I)。
                    #   漏了这一步，进程内一切正常，**重启后**才会以
                    #   "块哈希对不上" 的形式炸出来（见 _persist_nodes 的注释）。
                    #   跨进程模式下它自动跳过 —— 密文在各节点自己的库里。
                    self._persist_nodes(db, [gidx])
                    db.commit()
                    version = frow.version
                return {
                    "global_index": gidx,
                    "block_idx": block_idx,
                    "plain_len": plain_len,
                    "version": version,
                }

            try:
                store.modify(owner, file_key, block_idx, data, key_sink=sink)
            except WriteError:
                # 改块比追加更不能"重来一次"（重来会换新密钥、新密文，
                # 已经跟上的节点会当场拒）—— 所以现场必须留住，只能补推。
                # 同上：只有真的留住了现场才把落库挂上去。
                if store.pending_write() is not None:
                    self._pending_finish = _finish
                raise
            except (KeyError, IndexError) as exc:
                raise NotFound(str(exc)) from exc
            except ValueError as exc:
                raise OutOfRange(str(exc)) from exc

            with stage("落库（新密文与块登记）"):
                return _finish()

    # -------------------------------------------------------------------
    # 追加
    # -------------------------------------------------------------------

    def append_to_file(
        self,
        owner: str,
        file_key: str,
        data: bytes,
    ) -> dict:
        """在文件**末尾**追加内容 —— 只新增块，已有的块一个字节都不动。

        与 :meth:`upload` / :meth:`modify_block` 同一套顺序：**先让全网跟上，
        再改自己的账**。反过来的话"库说加了、节点没收到"会让协调者比实际存储
        更乐观，而且这种不一致**不会当场报错**，要到下次验证时才以
        "块哈希对不上"的形式炸出来。
        """
        with self._lock:
            store = self._require()
            pk_owner = self.user_public_key(owner)
            rec = store.files.get((owner, file_key))
            if rec is None:
                raise NotFound(f"文件 {owner}/{file_key} 不存在（追加只能追加到已有文件）")

            # ① 先读库：拿到这份文件在库里的 id
            with self.db.session() as db:
                first = db.execute(
                    select(BlockRow).where(
                        BlockRow.owner == owner,
                        BlockRow.file_key == file_key,
                        BlockRow.block_idx == 0,
                    )
                ).scalar_one_or_none()
                if first is None:  # pragma: no cover - 与上面的存在性校重复，兜底
                    raise NotFound(f"文件 {owner}/{file_key} 没有块账目")
                file_id = first.file_id

            n_before = rec.block_count

            # ② 真正追加：新密钥在 sink 里就地包成密文，明文不留
            wrapped: dict[int, dict] = {}

            def sink(pos: int, key: bytes) -> None:
                with stage("封装块密钥（ECIES）"):
                    wrapped[pos] = wrap_key(pk_owner, key)

            def _finish() -> dict:
                """③ 再写库 —— 正常路径与补推路径共用（理由同 :meth:`upload`）。

                ★ 它**只读** ``store`` 当前状态：新增的下标也从当前的账目里重算，
                  所以补推路径不必再传一遍。
                """
                cur = store.files[(owner, file_key)]
                new_indices = cur.indices[n_before:]
                with self.db.session() as db:
                    for off, gidx in enumerate(new_indices):
                        pos = n_before + off
                        db.add(
                            BlockRow(
                                global_index=gidx,
                                file_id=file_id,
                                owner=owner,
                                file_key=file_key,
                                block_idx=pos,
                                element=str(store.values[gidx]),
                                key_ct=json.dumps(wrapped[pos], ensure_ascii=False),
                                iv=cur.ivs[pos],
                                plain_len=cur.plain_lengths[pos],
                                holder=store.holder_of(gidx),
                                replicas=json.dumps(list(store.replicas_of(gidx))),
                            )
                        )
                    frow = db.get(FileRow, file_id)
                    assert frow is not None
                    # 版本号 +1 —— 防重放的那个序号，同时让旧证据（含证据池里的卡）作废
                    frow.version += 1
                    frow.block_count = cur.block_count
                    frow.total_bytes = cur.total_bytes
                    self._persist_globals(db)
                    # ★ 新块的密文 + 节点自己的 (δ, S_I, Λ_I) 都要落库 ——
                    #   漏了这一步进程内一切正常、重启后才会以"块哈希对不上"炸出来。
                    #   跨进程模式下它自动跳过（密文在各节点自己的库里）。
                    self._persist_nodes(db, new_indices)
                    db.commit()
                    version = frow.version
                return {
                    "added_blocks": len(new_indices),
                    "indices": list(new_indices),
                    "first_block_idx": n_before,
                    "block_count": cur.block_count,
                    "total_bytes": cur.total_bytes,
                    "version": version,
                }

            try:
                store.append(owner, file_key, data, key_sink=sink)
            except WriteError:
                # 与上传/改块同理：向量那边留了现场，这里把落库也挂上去。
                if store.pending_write() is not None:
                    self._pending_finish = _finish
                raise
            except (KeyError, IndexError) as exc:
                raise NotFound(str(exc)) from exc
            except ValueError as exc:
                msg = str(exc)
                raise (Conflict(msg) if "已存在" in msg else OutOfRange(msg)) from exc

            with stage("落库（新块与块登记）"):
                return _finish()

    def truncate_file(self, owner: str, file_key: str, drop_blocks: int) -> dict:
        r"""删掉文件**末尾**的若干块（论文 §8.2 的 ``op = del``）。

        与 :meth:`append_to_file` 是同一条纪律的两个方向：**只动末尾那几块**，
        不重切、不重新加密任何保留下来的块、不动其它文件。

        .. important::

           **只有"当前向量末尾"的块删得动。** 这不是实现偷懒，是方案的限制：
           ``vds.updates._push_del`` 要求待删集合恰好是 ``range(n-k, n)``
           （新摘要用到 :math:`e_{[n]} = e_{[n-k]} \cdot e_K` 这个因式分解）。
           所以"删文件中间的块"在数学上就不是一次 ``del``。

           使用上的推论：**只有最后写进向量的那份文件才删得动尾巴**。别的文件
           就算排在末尾，它前面也可能压着别人后写的块 —— 那种情况
           :meth:`~core.store.VectorStore.truncate` 会报错并**说清是哪些下标卡住**。

        .. important::

           删之前，协调者会先让"与待删区间**部分相交**"的节点把那一部分
           交回去（论文的 ``RmvStorage``）。理由见 ``_apply_del``：它要求每个
           节点与 ``K``「要么全包含、要么全不相交」，而我们的分片是
           「轮转 + 每块 N 份副本」，跨台的末尾区间几乎必然部分相交。
           这些块本来就要被删掉，所以让它们先交出来最自然。

        :returns: ``{"dropped_blocks", "dropped_indices", "block_count",
            "total_bytes", "version"}``
        """
        with self._lock:
            store = self._require()
            rec = store.files.get((owner, file_key))
            if rec is None:
                raise NotFound(f"文件 {owner}/{file_key} 不存在（截断只能作用于已有文件）")

            # ① 先读库：拿 file_id（落库要按它删块行），顺便校验块数
            with self.db.session() as db:
                first = db.execute(
                    select(BlockRow).where(
                        BlockRow.owner == owner,
                        BlockRow.file_key == file_key,
                        BlockRow.block_idx == 0,
                    )
                ).scalar_one_or_none()
                if first is None:  # pragma: no cover - 与上面的存在性校重复，兜底
                    raise NotFound(f"文件 {owner}/{file_key} 没有块账目")
                file_id = first.file_id

            n_before = rec.block_count

            def _finish() -> dict:
                """② 再写库 —— 正常路径与补推路径共用（理由同 :meth:`upload`）。

                ★ 要删哪些块**从当前账目反推**（``block_idx >= 当前块数`` 的都是
                  被删掉的），而不是把下标从外面传进来 —— 这样补推路径不必
                  再传一遍，两条路也必然算出同一份结果。
                """
                cur = store.files[(owner, file_key)]
                with self.db.session() as db:
                    frow = db.get(FileRow, file_id)
                    assert frow is not None
                    doomed = list(
                        db.execute(
                            select(BlockRow.global_index).where(
                                BlockRow.file_id == file_id,
                                BlockRow.block_idx >= cur.block_count,
                            )
                        ).scalars()
                    )
                    if doomed:
                        db.execute(
                            delete(BlockRow).where(
                                BlockRow.global_index.in_(doomed)
                            )
                        )
                        # ★ 单进程模式下节点密文是协调者代存的，也要一起抹掉：
                        #   只删 blocks 会留下孤儿密文，重启后 import_states
                        #   会把它们读回节点 ⇒ 节点自检"下标与密文对不上"炸掉。
                        db.execute(
                            delete(NodeBlobRow).where(
                                NodeBlobRow.global_index.in_(doomed)
                            )
                        )
                    # 版本号 +1：与改块/追加同一语义 —— 旧证据（含证据池里的卡）作废
                    frow.version += 1
                    frow.block_count = cur.block_count
                    frow.total_bytes = cur.total_bytes
                    self._persist_globals(db)
                    # 节点状态整表重写（它们的 I 变小了）；密文不加不减
                    self._persist_nodes(db, [])
                    db.commit()
                    version = frow.version
                return {
                    "dropped_blocks": len(doomed),
                    "dropped_indices": sorted(int(i) for i in doomed),
                    "block_count": cur.block_count,
                    "total_bytes": cur.total_bytes,
                    "version": version,
                }

            try:
                store.truncate(owner, file_key, drop_blocks)
            except WriteError:
                if store.pending_write() is not None:
                    self._pending_finish = _finish
                raise
            except KeyError as exc:
                raise NotFound(str(exc)) from exc
            except ValueError as exc:
                msg = str(exc)
                raise (Conflict(msg) if "已存在" in msg else OutOfRange(msg)) from exc

            with stage("落库（删除登记）"):
                out = _finish()
            out["dropped_from"] = n_before
            return out

    # -------------------------------------------------------------------
    # 读
    # -------------------------------------------------------------------

    def query(self, indices: Sequence[int], *, allow_partial: bool = False) -> dict:
        """**验证不受限**：按全局下标取回内容与一份聚合证据，并验证。

        返回的是向量分量（密文摘要）与证据，**不是明文**。

        :param allow_partial: 有块**一份在线副本都没有**时：
            ``False`` 报错（报错里列清楚缺哪几个下标、各台各持多少）；
            ``True`` 只把拿得到的算进结论，缺的放进响应里的 ``missing``。

            .. important::

               开了它也只是“**结论只覆盖到手的那些块**”，不是“假装收到了”。
               前端必须把 ``missing`` 显著地显示出来。
        """
        with self._lock:
            store = self._require()
            try:
                r = store.query(indices, allow_partial=allow_partial)
            except ValueError as exc:
                raise OutOfRange(str(exc)) from exc
            return {
                #: 取这份证据时的全局块数。前端拿它判断"手里的旧证据是否已作废"：
                #: 上传会让 n 变，而 π_I 是 n 的函数 —— n 一变，旧证据全失效。
                "delta_n": store.delta.n,
                #: 而光看 n 不够：**改块不改 n，却改承诺 C**。所以再给一个 δ 指纹，
                #: 前端按它判"这张卡还行吗"。
                "delta_fp": self.delta_fingerprint(),
                #: ★ 这份结论**没覆盖**哪些块（只在开了 allow_partial 时才可能非空）。
                "missing": list(r.missing),
                "partial": r.partial,
                "indices": list(r.indices),
                "values": [str(v) for v in r.values],
                "proof": {
                    "S_I": str(r.proof.S_I),
                    "Lambda_I": str(r.proof.Lambda_I),
                    "I": list(r.proof.I),
                    "size_bytes": r.proof_size_bytes,
                },
                "verify": {
                    "ok": bool(r.report.ok),
                    "code": int(r.report.code),
                    #: ★ 失败**环节名**（``BAD_S_I`` / ``BAD_LAMBDA`` …）。
                    #: 光给整数，界面就只能说"失败了"，说不到"是哪一步没过"。
                    "code_name": verify_code_name(r.report.code),
                    "message": r.report.message,
                },
                "hash_layer_ok": r.hash_layer_ok,
                "ok": r.ok,
                "holders": {str(k): v for k, v in r.holders.items()},
                "refs": [
                    {
                        "global_index": store.registry.index_of(
                            ref.owner, ref.file_key, ref.block_idx
                        ),
                        "owner": ref.owner,
                        "file_key": ref.file_key,
                        "block_idx": ref.block_idx,
                    }
                    for ref in r.refs
                ],
                "cert_count": r.cert_count,
                "nodes_used": list(r.node_used),
            }

    def query_files(
        self, targets: Sequence[tuple[str, str]], block_indices: Sequence[int] | None = None
    ) -> dict:
        """一次查询**若干个文件** —— 设计 B 下这只需要一份证据。

        :param block_indices: 给定时只取这几个**块序号**（对每个文件都取一遍，
            越界的自动跳过），否则取整个文件。
        """
        with self._lock:
            store = self._require()
            picked: list[int] = []
            detail: list[dict] = []
            for owner, file_key in targets:
                got = store.file_indices(owner, file_key)
                if not got:
                    raise NotFound(f"文件 {owner}/{file_key} 不存在")
                if block_indices is None:
                    chosen = list(got)
                else:
                    chosen = [
                        got[k] for k in block_indices if 0 <= k < len(got)
                    ]
                picked.extend(chosen)
                detail.append(
                    {"owner": owner, "file_key": file_key, "indices": chosen}
                )
            if not picked:
                raise OutOfRange("没有选中任何块")
            out = self.query(sorted(set(picked)))
            out["files"] = detail
            return out

    # -------------------------------------------------------------------
    # 分解再聚合（需求 5）
    # -------------------------------------------------------------------

    def disagg(self, evidence: dict, K: Sequence[int]) -> dict:
        r"""**分解**：从一份已有证据里拆出子集的证据 —— **不向节点取数据**。

        论文 §5.2 的 ``VC.Disagg`` 把 :math:`\pi_I` 拆成 :math:`\pi_K`
        （要求 :math:`K \subseteq I`），代价只是
        :math:`O(\ell\,|I \setminus K|)` 次乘法 —— **一次网络都不发**。
        这是需求 5「分解再聚合」里"分解"的那一半。

        :param evidence: ``{"I": [...], "values": [...], "S_I": "...", "Lambda_I": "..."}``
            —— 就是 ``/api/query`` 响应里那几个字段，前端把池子里的卡片
            **原样传回来**即可，不必另造一份数据结构。

        .. important::

           **``K ⊆ I`` 是硬约束。** 想要一个含**新**下标的证据，光靠这一份
           永远拆不出来 —— 必须另外去取（规划 §3.2）。这里会明确报错并指出
           缺哪几个下标，而不是交出去一份错的。

        .. important::

           证据是 **n 的函数**（:math:`S_I = g^{e_{[n]}/e_I}`），所以
           **过时的证据拆出来的也是过时的**。本函数会用当前的 δ 验一遍再交出去
           —— 旧 ``n`` 上取的会直接报"请重新取"，而不是把一份验不过的证据
           塞给调用方（那种失败要等到用户拿去验证时才暴露，极难归因）。
        """
        with self._lock:
            store = self._require()
            I = as_index_set(evidence["I"])
            K_set = as_index_set(K)
            vals_I = [int(v) for v in evidence["values"]]

            if len(vals_I) != len(I):
                raise OutOfRange(
                    f"I 有 {len(I)} 个下标，但 values 有 {len(vals_I)} 个 —— 对不上"
                )
            missing = sorted(set(K_set) - set(I))
            if missing:
                raise OutOfRange(
                    f"分解只能取出「子集」：{missing} 不在原证据覆盖的 {list(I)} 里。\n"
                    "  含「新」下标的证据必须另外去取（向持有那些块的节点定向 fetch），"
                    "再与分解出来的部分聚合 —— 这正是证据池存在的理由。"
                )

            pi_I = Opening(int(evidence["S_I"]), int(evidence["Lambda_I"]), I)
            crs_n = store.session.crs_n_for(store.delta)
            with stage("拆出子集证据（svc.disagg）"):
                pi_K = svc_disagg(crs_n, I, vals_I, pi_I, K_set)

            val_of = dict(zip(I, vals_I))
            F_K = tuple(val_of[i] for i in K_set)
            with stage("验证拆出来的证据（VerRetrieve）"):
                report = ClientNode(store.session, store.delta).ver_retrieve(
                    list(K_set), list(F_K), pi_K
                )
            if not report.ok:
                # 同样：会原样弹给前端，不用 Markdown 星号。
                raise OutOfRange(
                    f"分解出来的证据验不过：{report.message}\n"
                    f"  最可能的原因是「这份证据是在旧的 δ 上取的」——\n"
                    f"  上传会让全局块数 n 变、改块会让承诺 C 变，两者都会让"
                    f"之前取到的证据失效。\n"
                    f"  请重新取一次（当前 n = {store.delta.n}）。"
                )

            # 响应与 /api/query **保持同构** —— 前端那张卡片能直接复用，
            # 不必为"分解出来的证据"写第二套渲染。
            #
            # 几个字段如实说明：
            #   * refs / holders —— 分解没向节点要东西，但协调者**本来就知道**
            #     谁持有哪一块，照填即可；
            #   * cert_count = 0、nodes_used = [] —— 分解确实一份凭证都没收，
            #     填假数字才是骗人；
            #   * hash_layer_ok —— 分解不涉及密文，没有"密文对不上分量"这回事，
            #     所以是 True（没有任何东西被检查出不一致）。
            return {
                "delta_n": store.delta.n,
                "delta_fp": self.delta_fingerprint(),
                "indices": list(K_set),
                "values": [str(v) for v in F_K],
                "proof": {
                    "S_I": str(pi_K.S_I),
                    "Lambda_I": str(pi_K.Lambda_I),
                    "I": list(pi_K.I),
                    "size_bytes": proof_bytes(pi_K.S_I, pi_K.Lambda_I),
                },
                "verify": {
                    "ok": bool(report.ok),
                    "code": int(report.code),
                    "code_name": verify_code_name(report.code),
                    "message": report.message,
                },
                "ok": bool(report.ok),
                "hash_layer_ok": True,
                "holders": {str(i): store.holder_of(i) for i in K_set},
                "refs": [
                    {
                        "global_index": store.registry.index_of(
                            ref.owner, ref.file_key, ref.block_idx
                        ),
                        "owner": ref.owner,
                        "file_key": ref.file_key,
                        "block_idx": ref.block_idx,
                    }
                    for ref in store.registry.blocks_of(K_set)
                ],
                "cert_count": 0,
                "nodes_used": [],
                # ↓ 分解特有的：界面用它们说清"这次从哪来、丢了哪些"
                "disaggregated": True,
                "source_indices": list(I),
                "dropped": sorted(set(I) - set(K_set)),
            }

    def verify_batch(
        self, cards: Sequence[dict], *, locate: bool = True, compare: bool = True
    ) -> dict:
        r"""**批量验证**：一次验池子里的多份证据（可跨文件、跨用户）。

        需求原句「选择若干证据验证、**聚合验证**」的后者。逐份验在
        ``/api/query`` 与 ``/api/evidence/disagg`` 里已经走过；这里是
        "**一次结论**"：挑随机系数把 m 条方程合成一条（:func:`svc.verify_batch`）。

        :param cards: 每张取 ``/api/query`` 响应里的
            ``indices`` / ``values`` / ``proof`` —— 前端把池子里的卡片原样传回来。
        :param compare: 同时跑一遍**逐份验**，把两个耗时都报出去。
            这不是为了显得快，恰恰相反：实测批量更慢（规划 §13.6），
            把两个数一起摆出来才不会让人误会。同时它能当场发现"两套结论不一致"
            —— 那说明实现有 bug，
            比給一个好看的数字重要得多。

        .. important::

           证据是 **n 的函数**，所以**必须都来自同一轮 δ** ——
           方程右边的 :math:`U_n` 与 :math:`C` 是所有份共享的。
           返回体里带上 ``delta_fp``，前端可以拿它与池子里的指纹比对，
           避免把过时证据当"被篡改"。
        """
        with self._lock:
            store = self._require()
            crs_n = store.session.crs_n_for(store.delta)
            C = store.delta.C

            cases = []
            for c in cards:
                proof = c["proof"]
                # ★ 以**卡片上的** indices 为准，而且**不预先规范化** ——
                #   预先 as_index_set 掉的话，卡片里的重复下标就被"静默修正"了，
                #   形状检查再也看不见它（实测踩过：重复下标被当成正常证据验过了）。
                #   proof.I 只用来跟它对账（对不上就是 BAD_SHAPE）。
                raw_I = [int(i) for i in c["indices"]]
                cases.append(
                    (
                        raw_I,
                        [int(v) for v in c["values"]],
                        Opening(
                            int(proof["S_I"]),
                            int(proof["Lambda_I"]),
                            as_index_set(proof.get("I") or raw_I),
                        ),
                    )
                )

            with stage("批量验证（一次结论）"):
                t0 = perf_counter()
                rep = svc_verify_batch(crs_n, C, cases, locate=locate)
                ms_batch = (perf_counter() - t0) * 1000.0

            ms_separate: float | None = None
            agree: bool | None = None
            if compare:
                with stage("逐份验证（对照）"):
                    t0 = perf_counter()
                    sep = [svc_verify(crs_n, C, I, v, p) for I, v, p in cases]
                    ms_separate = (perf_counter() - t0) * 1000.0
                    agree = all(bool(x.ok) for x in sep) == bool(rep.ok)

            return {
                "delta_n": store.delta.n,
                "delta_fp": self.delta_fingerprint(),
                "ok": bool(rep.ok),
                "code": int(rep.code),
                "code_name": verify_code_name(rep.code),
                "message": rep.message,
                "n": rep.n_cases,
                "bad": list(rep.bad),
                "ok_s": bool(rep.ok_s),
                "ok_lambda": bool(rep.ok_lambda),
                "ms_batch": round(ms_batch, 1),
                "ms_separate": None if ms_separate is None else round(ms_separate, 1),
                "agree": agree,
            }

    def pending_write(self) -> dict:
        """有没有“推失败了、还没补”的更新。

        界面拿它决定要不要报——注意这是**真故障**（全网不一致），
        不是“慢”：已经有节点按新 δ 前进了，另几台还在旧 δ 上。
        """
        with self._lock:
            return {
                "pending": self._require().pending_write(),
                #: 向量层已补推、但落库那一步还欠着（正常不会有；
                #: 它只在“补成功了、写库中途出了意外”时出现）。
                "persist_pending": self._pending_finish is not None,
                #: 自动补推的现状 —— 界面拿它告诉用户“别慌，还在自己试；
                #: 试完了还没成，就该你上了”。
                "auto": self.auto_retry_state(),
            }

    # -------------------------------------------------------------------
    # 写失败后的**自动补推**
    # -------------------------------------------------------------------
    #
    # ★ `_pending_finish` 故意做成**属性**：它一共有 4 个设置点
    #   （上传 / 改块 / 追加 / 截断各一处），而“留了现场就要踢一次自动补推”
    #   这件事**每处都必须做**。写成属性之后只有这一处定义 —— 漏一处
    #   在代码上就不可能发生了。

    @property
    def _pending_finish(self) -> Callable[[], object] | None:
        return self._finish_cb

    @_pending_finish.setter
    def _pending_finish(self, cb: Callable[[], object] | None) -> None:
        self._finish_cb = cb
        if cb is not None:
            self._kick_auto_retry()

    def auto_retry_state(self) -> dict:
        """自动补推的现状（给界面与测试看）。"""
        alive = self._auto_thread is not None and self._auto_thread.is_alive()
        return {
            "enabled": bool(getattr(self.settings, "retry_auto", False)),
            "running": bool(alive),
            "attempts": int(self._auto_attempts),
            "max_attempts": int(getattr(self.settings, "retry_max_attempts", 5) or 5),
            "interval_s": float(getattr(self.settings, "retry_interval_s", 5.0) or 5.0),
            "note": self._auto_note,
        }

    def _kick_auto_retry(self) -> None:
        """写失败留下现场之后，起一个**后台线程**定时补推（有次数上限）。

        为什么要它：写失败是**真故障**（全网不一致），只能靠“把没跟上的那几台
        补上”来收敛。人工在集群页点「补推」当然行，但**没人点的时候系统会一直
        卡在 409**（写路径被挡住），看起来像“坏了”。

        为什么**有上限**：无限重试会把“哪台一直不在线”这件事藏起来。次数用完
        就停在原地，并在待补推视图里明说“请人工处理” —— 「宁可吵不要哑」。

        为什么**不引任务队列**：规划 §2.10 明确不引新依赖，而这件事只需要
        “每隔几秒试一次、成功就退出”。线程是 daemon：进程退出不必等它。

        已经有一个在跑就不重复起（否则连续几次写失败会起一堆线程）。
        """
        if not getattr(self.settings, "retry_auto", False):
            return
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return
        self._auto_attempts = 0
        self._auto_note = f"已开始自动补推（每 {self.auto_retry_state()['interval_s']} 秒一次）"
        self._auto_thread = threading.Thread(
            target=self._auto_retry_loop, name="vds-auto-retry", daemon=True
        )
        self._auto_thread.start()

    def _auto_retry_loop(self) -> None:
        """自动补推线程的全部逻辑（跑在后台线程里，不能把异常抛出去）。"""
        state = self.auto_retry_state()
        limit = max(1, int(state["max_attempts"]))
        gap = max(0.05, float(state["interval_s"]))
        while True:
            time.sleep(gap)
            try:
                out = self.retry_push()
            except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常打死整个进程
                self._auto_note = f"第 {self._auto_attempts + 1} 次失败：{exc}"
                self._auto_attempts += 1
                if self._auto_attempts >= limit:
                    return
                continue
            if out.get("pending") is None and not out.get("persist_pending"):
                self._auto_note = f"已自动补齐（试了 {self._auto_attempts + 1} 次）"
                self._auto_attempts += 1
                return
            who = "、".join((out.get("pending") or {}).get("nodes") or []) or "未知节点"
            self._auto_attempts += 1
            self._auto_note = (
                f"自动补推第 {self._auto_attempts} 次仍未成功（{who}）"
                + ("：次数已用完，请人工处理" if self._auto_attempts >= limit else "，稍后再试")
            )
            if self._auto_attempts >= limit:
                return

    def retry_push(self, *, manual: bool = False) -> dict:
        """**补推**：把上次没推成功的那次更新，**只补推给没跟上的那几台**。

        ★ 为什么不能“重新传一次文件”：协调者的 δ 没推进，但**登记表已经分配过**
        那批下标 —— 再传一次会拿到新下标，而且会撞上“登记表游标与向量长度
        不一致”。所以补推是唯一的出路（见 ``core/transport.py::WriteError``）。

        ★ 补完向量**还要把那次操作欠下的落库补上**：库里如果一直是空的/
        旧的，重启时是从库重建向量的，于是会以“节点与协调者不同步”被拦下 ——
        看起来像“补推把系统修坏了”，实际上是账只记了一半。

        ``ok=False`` 不是“没救了”：它表示“补了，但还有几台没通”——
        把那儿台弄活再点一次即可（已经补上的不会被打扰）。
        """
        with self._lock:
            if manual:
                # 人工点了一次：把自动补推的计数**重新给满**（潜台词是
                # “我刚去把机器修好了，再自动试几轮”）。★ 只有人工传 manual=True
                # —— 后台线程自己不传，否则它会在自己的循环里反复给自己续命。
                self._auto_attempts = 0
            store = self._require()
            # ★ 先看"动手前到底有没有待补推"。不能只看 retry_pending 的 ok ——
            #   没有现场时它也返回 ok=True（"没有待补推的更新"），那时调
            #   `_finish()` 就会造出**假的收敛**：库里有文件、向量里没分量。
            had_pending = store.pending_write() is not None
            out = store.retry_pending()
            if had_pending and out.get("ok") and self._pending_finish is not None:
                finish, self._pending_finish = self._pending_finish, None
                with stage("落库（补推收尾）"):
                    finish()
                out["persisted"] = True
            out["pending"] = store.pending_write()
            out["persist_pending"] = self._pending_finish is not None
            return out

    def _blocks_of(self, owner: str, file_key: str) -> dict[int, dict]:
        """取这份文件每块的 ``{block_idx: {key_ct}}``。

        特意提取成纯 dict 而不是回传 ORM 对象 —— session 关掉之后再碰
        ORM 对象容易踩 detached 的坑，而这个数据我们要用很久。
        """
        with self.db.session() as db:
            rows = db.execute(
                select(BlockRow)
                .where(BlockRow.owner == owner, BlockRow.file_key == file_key)
                .order_by(BlockRow.block_idx)
            ).scalars()
            return {r.block_idx: {"key_ct": r.key_ct} for r in rows}

    def _read_with(self, owner: str, file_key: str, indices, sk: int) -> dict:
        """取明文：取密文段（节点）→ 解封块密钥（用 ``sk``）→ SM4 解。

        :param sk: 解密者的 SM2 私钥标量。**必传、没有默认值** ——
            有默认值就会有"忘了传"的调用点，而那会静默地变成
            "服务器万能解密"，把整条访问控制架空。
        """
        store = self._require()
        if (owner, file_key) not in store.files:
            raise NotFound(f"文件 {owner}/{file_key} 不存在")
        blocks = self._blocks_of(owner, file_key)

        def key_of(pos: int) -> bytes:
            row = blocks.get(pos)
            if row is None:
                raise NotFound(f"文件 {owner}/{file_key} 没有第 {pos} 块")
            try:
                with stage("解封块密钥（ECIES）"):
                    return unwrap_key(sk, json.loads(row["key_ct"]))
            except KeyWrapIntegrityError as exc:
                # ★ 这是**密码学那道门**的拒绝：这块的块密钥是用**所有者的
                #   公钥**封的，你的私钥对它无效。注意它不是"查表发现你没
                #   权限"，而是**算不出来** —— 这两件事在答辩时是重点。
                raise DecryptDenied(
                    "这块的块密钥解不开 —— 它不属于你（别人的私钥对它无效），"
                    "或者密文被篡改过"
                ) from exc
            except KeyWrapError as exc:
                raise DecryptDenied(f"块密钥密文无法解析：{exc}") from exc

        try:
            data = store.read(owner, file_key, indices, key_of=key_of)
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise OutOfRange(str(exc)) from exc
        return {
            "owner": owner,
            "file_key": file_key,
            "bytes": len(data),
            "data_hex": data.hex(),
        }

    def read_plain(self, owner: str, file_key: str, sk: int, indices=None) -> dict:
        """用**某个人的私钥**取回明文。解不开 → :class:`DecryptDenied`。

        :param sk: 解密者的 SM2 私钥标量。**必传、没有默认值**（理由见
            :meth:`_read_with`）。上层是 ``routers/files.py`` 从**本次登录
            解封出来的会话私钥**里取的 —— 不是从库里现查的。
        """
        with self._lock:
            return self._read_with(owner, file_key, indices, sk)

    # -------------------------------------------------------------------
    # 视图 / 状态
    # -------------------------------------------------------------------

    def status(self) -> dict:
        store = self._require()
        # 探活只做一次，下面两处都复用它（理由见 ``nodes`` / ``nodes_down`` 那两行）
        report = store.node_report()
        return {
            "crs": {
                "N_bits": store.session.crs.N.bit_length(),
                "l": store.session.l,
                "n_max": store.session.n_max,
                "prime_bits": store.session.l + 1,
            },
            "delta": {
                "n": store.delta.n,
                "U": str(store.delta.U),
                "C": str(store.delta.C),
            },
            "files": len(store.files),
            "blocks": store.n,
            #: 切块参数。前端做「按块数切」的预估与校验必须知道这三个值 ——
            #: 而它们属于**后端配置**，前端不该自己猜（猜错就是把一个魔数写死）。
            #: ``segment_bytes`` 是**部署默认值**，min/max 是允许范围。
            "segment_bytes": self.settings.segment_bytes,
            "segment_bytes_min": self.settings.segment_bytes_min,
            "segment_bytes_max": self.settings.segment_bytes_max,
            #: 每块存几份。**副本不占全局位置**，所以它不影响 n / n_max。
            "replica_factor": store.replica_factor,
            #: 副本数不足的块数 —— 通常是“开副本之前传的老数据”。
            #: 它是提示，不是错误（所以不拦 check）。重新传一次即可补齐。
            "under_replicated": len(store.under_replicated()),
            # ★★ 报告**只取一次**。
            #
            #   ``node_report()`` 会**逐台探活**：跨进程模式下是每台一次真网络请求
            #   （而且带密码学验证），一台机器掉线就要付一次连接超时的代价。
            #   这里原来把它调了**两次**（一次数 ``nodes``、一次数 ``nodes_down``）
            #   —— 于是同一份报告算了两遍，``/api/status`` 白花一倍时间。
            #
            #   实测：有一台节点掉线时 ``/api/status`` 要 **6.7 s**，而存储节点页与
            #   集群页都靠它 —— 偏偏"kill 一台看降级"就是要在这两页上演示的。
            #   改成复用同一份报告后降到 ~3.3 s（剩下的是那台死节点的 TCP 超时本身
            #   与三台活节点的密码学自检，那两项都是该付的）。
            "nodes": len([r for r in report if not r.get("fresh")]),
            "nodes_down": [r["node_id"] for r in report if r.get("unreachable")],
            "mode": "distributed" if self.settings.distributed else "single-process",
            "transport": type(store.transport).__name__,
            "keywrap": self.keywrap_status(),
            "warning": "|N| 仅为演示档，无安全强度；真实部署论文配置是 16λ = 2048 位",
        }

    def node_report(self) -> list[dict]:
        return self._require().node_report()

    def pos_audit(self, *, lambda_pos: int = DEFAULT_LAMBDA_POS) -> dict:
        """**存储证明（PoR）**：不下载内容，确认各节点确实还存着它那份。

        与 :meth:`query` 是两条不同的审计线（``query`` 问"对不对"，
        这里问"还在不在"），所以它**不碰任何密钥、也不写审计流水** ——
        "验证不受限"这条原则同样适用于它。

        :raises Conflict: 全局向量还是空的（没东西可挑战）
        """
        with self._lock:
            store = self._require()
            try:
                return store.pos_audit(lambda_pos=lambda_pos)
            except ValueError as exc:
                raise Conflict(str(exc)) from exc

    def registry_info(self, global_index: int) -> dict:
        """全局下标 → "这是谁的第几块、存在哪台"。"""
        store = self._require()
        ref = store.registry.describe(global_index)
        return {
            "global_index": global_index,
            "owner": ref.owner,
            "file_key": ref.file_key,
            "block_idx": ref.block_idx,
            "holder": store.holder_of(global_index),
            #: 全部持有者（主副本在前）。单副本时就是 ``[holder]``。
            "replicas": list(store.replicas_of(global_index)),
        }

    def list_files(self) -> list[dict]:
        """所有文件 —— **验证不受限**，所以列表不按所有者过滤。

        每行带上该文件的全局下标区间，前端据此选块验证。
        """
        store = self._require()
        out: list[dict] = []
        for (owner, file_key), rec in store.files.items():
            out.append(
                {
                    "owner": owner,
                    "file_key": file_key,
                    "total_bytes": rec.total_bytes,
                    "block_count": rec.block_count,
                    "segment_bytes": rec.segment_bytes,
                    "content_digest": rec.content_digest,
                    "indices": list(rec.indices),
                    # 只是**方便显示**的边界值。追加之后一个文件的下标可能带缺口
                    # （两次追加之间别的文件也占了位置），所以 first..last 之间
                    # 可能夹着别人的块 —— 要真集合只能用上面的 indices。
                    "first_index": rec.indices[0],
                    "last_index": rec.indices[-1],
                }
            )
        out.sort(key=lambda d: d["first_index"])
        return out

    def check(self) -> None:
        self._require().check()
    def _require(self) -> VectorStore:
        if self.store is None:
            raise RuntimeError("StoreManager 还没 bootstrap()")
        return self.store


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _file_record(first: BlockRow, rows: list[BlockRow], frow: FileRow | None):
    from core.store import FileRecord

    # 注意这里**没有 keys** —— 本类不保管密钥，密文密钥在 BlockRow.key_ct 里。
    return FileRecord(
        owner=first.owner,
        file_key=first.file_key,
        indices=tuple(r.global_index for r in rows),
        ivs=[bytes(r.iv) for r in rows],
        plain_lengths=[r.plain_len for r in rows],
        total_bytes=frow.total_bytes if frow else sum(r.plain_len for r in rows),
        segment_bytes=frow.segment_bytes if frow else 1024,
        content_digest=frow.content_digest if frow else "",
    )


# 说明：原先这里还有一整块"ABE 主密钥的读写"（``_read_msk`` / ``_write_msk``）——
# 它们维护一个**能解开任何密文**的全局 msk 文件。
#
# 现在没有这种东西了：每个用户一把自己的 SM2 私钥，用**他自己的登录口令**
# 包一层之后存进 ``users.sk_wrapped``。于是：
#
#   * 不再有"全库唯一的万能密钥"，也就没有"它丢了/被抄走了怎么办"那个问题；
#   * 口令不在库里（``pwd_hash`` 只是登录校验用的加盐哈希，推不回口令），
#     所以拿到数据库也解不开任何一份私钥，是解不开任何文件。
