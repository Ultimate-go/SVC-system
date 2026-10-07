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

from sqlalchemy import delete, func, select, update

from core import GlobalSession, VectorStore, crs_from_dict, new_session
from core.crypto import split_segments, vector_element
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
from core.sm2 import public_key_of
from core.timing import stage
from core.transport import NodeTransport, TransportError, WriteError
from svc import Opening, VerifyCode, as_index_set
from svc import disagg as svc_disagg
from svc import verify as svc_verify
from svc import verify_batch as svc_verify_batch
from vds.client_node import ClientNode
from vds.digest import Digest
from vds.pos import DEFAULT_LAMBDA_POS

from .config import Settings, read_deploy_node_count
from .security import decode_token, hash_password
from .db import Database
from .schemas import MAX_INDICES
from .models import (
    BlockRow,
    CrsRow,
    FileDeltaRow,
    FileRow,
    MetaRow,
    NodeBlobRow,
    NodeRegistryRow,
    NodeStateRow,
    ReplayRow,
    RevokedTokenRow,
    UserRow,
)

__all__ = [
    "StoreManager",
    "Conflict",
    "NotFound",
    "OutOfRange",
    "DecryptDenied",
]

#: 分片轮转的偏移量（模节点台数）。
#:
#: 必须落库：不存的话重启后偏移归零，之后再传小文件又永远从第 1、2 台
#: 开始堆，后几台长期空闲 —— 而 :meth:`VectorStore._plan` 的注释
#: 明确承诺了"连续多次小上传也能覆盖到所有服务器"。
G_NODE_OFFSET = "node_offset"

# ★ 摘要**不再**放在这里：新方案里摘要是逐文件的，见 ``FileDeltaRow``。
#   这张 ``meta`` 表只留“全系统唯一”的标量。


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


def _to_int(raw, *, what: str) -> int:
    """十进制字符串 → 整数；**把“太长”变成 400，而不是 500**（安全审计 P6）。

    ★ Python 3.12 起，``int("9" * 5000)`` 会抛 ``ValueError``
      （``sys.set_int_max_str_digits()`` 默认 4300）。那是**用户输入**造成的，
      而它以前会一路冒到 FastAPI 的默认处理器，表现为“服务端 500”——
      把用户的错报成了服务端的错。

    本方案的合法值最多 309 位十进制（``|N|`` = 1024 位），所以
    schema 层已用 :data:`~backend.schemas.MAX_INT_CHARS` 拦了第一道；
    这里是**第二道**：将来某个字段漏了约束，也不会变成 500。
    """
    try:
        return int(raw)
    except ValueError as exc:
        raise OutOfRange(
            f"{what} 不是合法的十进制整数（长度 {len(raw)}）：{exc}"
        ) from exc


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
        #: 已作废令牌的 ``jti -> exp``（见 :meth:`revoke_token`）。
        #: ★ 这只是**缓存**，真身在 ``revoked_tokens`` 表里（安全审计 I1）。
        self._revoked: dict[str, float] = {}
        #: “按人撤销”的签发时刻阈值：``用户名 -> before_ts``（见 :meth:`revoke_user_tokens`）。
        #: 同样是缓存；0 表示“查过了，这个人没被按人撤销过”。
        self._revoked_users: dict[str, int] = {}
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
        #: 最近一次「改台数 → 搬块」的交代（见 :meth:`redistribute`）。
        #:
        #: ★ 只在内存里：它要回答的是"刚刚那次操作搬了什么"，而重启之后
        #:   这个问题已经由 ``nodes/deploy.json`` 里那几行回答了
        #:   （台数从几改到几、给界面看的那句话）—— 两边分工不重叠。
        self._last_redistribute: dict | None = None
        #: **本次启动才加入**的机器（登记表里以前没有它们）。
        #:
        #: ★ 界面上"新节点一开始是空的"那句话靠它说 —— 而不是靠
        #:   "这个节点现在持有了几块"：一个老节点完全可能因为轮转而一块都没有，
        #:   那与"新加入"是两回事，混起来会给出一句错的解释。
        self._joined_now: list[str] = []

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
                self._joined_now = self._register_nodes(db)
                self._verify_nodes_at_startup(store, db)

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

    def rekey_with_blob(self, username: str, new_password: str, blob: dict) -> None:
        """用**浏览器封好的**私钥密文改口令（安全审计 I8）。

        ★ 与 :meth:`rewrap_user_key` 的分工：

        * ``rewrap_user_key``：调用方**手里有明文私钥**（旧的
          ``server_key=true`` 路径），由服务端自己重封 —— 代价是它必须
          先从内存里拿到私钥；
        * ``rekey_with_blob``：私钥**只在浏览器**，重封也在浏览器做，
          服务端只登记结果。默认模型走的是这一条。

        服务端仍要**验两件事**，否则“改口令”就成了一个可以任意写私钥的接口：

        ① 新密文能用**新口令**解开（形状 / 口令 / 密文三者自洽）；
        ② 解出来的私钥必须**就是这个人原来那把** —— 拿 ``users.pub_key``
           对拍（``public_key_of(sk) == pub_key``）。

        ★ ② 这一条不能省：少了它，攻击者可以把**自己的**私钥封装后交上来，
          从此这个账号名下的文件全部归他解 —— 而 ``pub_key`` 就是那把
          “当初封块密钥时用的公钥”，它绑定了唯一一把私钥。

        ★ 口令哈希与私钥密文必须**同生同死**：分开写会留下
          “新口令过不了哈希、旧口令解不开密文”的锁死账号。

        :raises NotFound: 账号还没有密钥对。
        :raises Conflict: 密文解不开，或解出来的私钥对不上公钥。
        """
        with self._lock, self.db.session() as db:
            row = self._user_row(db, username)
            if not row.pub_key:
                raise NotFound(f"用户 {username} 还没有密钥对")

            try:
                sk = unwrap_private_key(new_password, blob)
            except KeyWrapIntegrityError as exc:
                raise Conflict(
                    "新私钥密文与**新口令**不匹配（解不开）—— "
                    "浏览器那边的重封可能没完成，或者密文在转交中被改过"
                ) from exc
            except KeyWrapError as exc:
                raise Conflict(f"新私钥密文的格式不对：{exc}") from exc

            # ★★ 必须比**同一种格式**：``public_bytes`` 返回的是 ``bytes``，
            #   而库里 ``users.pub_key`` 存的是**十六进制字符串**（65 字节 → 130 字符）。
            #   直接拿两者比会**恒不相等** —— 于是"改口令"这条路对任何人都是
            #   400「新密文里的私钥与这个账号的公钥对不上」，看起来像密码学出了问题，
            #   其实只是漏了一个 ``.hex()``。（实测踩到：zhangsan 改口令 400。）
            if public_bytes(public_key_of(sk)).hex() != row.pub_key:
                raise Conflict(
                    "新密文里的私钥与这个账号的公钥**对不上** —— 拒绝替换。\n"
                    "  改口令只应换个‘外壳’，私钥本体必须原样；"
                    "对不上说明交上来的不是你自己的私钥。"
                )

            row.pwd_hash = hash_password(new_password)
            row.sk_wrapped = json.dumps(blob, ensure_ascii=False)
            db.commit()

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

    def user_key_blob(self, username: str) -> dict:
        """取用户的**私钥密文**（可以安全地交给浏览器）。

        ★ 把它交出去**不构成泄露**：这份密文是用口令派生的 KEK 封起来的，
          库连同它一起被拿走也解不开（这正是 ``core/keywrap`` 立这层的目的）。

          它成立的前提也只有一条：**服务端不参与解封**。
          所以默认登录路径（``server_key=False``）拿到它就直接交给前端，
          内存里什么也不留 —— 服务端手里没有任何能解开文件的东西。

        :raises NotFound: 这个账号还没有密钥对。
        """
        with self.db.session() as db:
            row = self._user_row(db, username)
            if not row.sk_wrapped:
                raise NotFound(f"用户 {username} 还没有密钥对")
            return json.loads(row.sk_wrapped)

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

    # -- 令牌撤销（安全审计 I1） -----------------------------------------
    #
    # ★ 为什么需要它：JWT 是**无状态**的 —— 签出去就收不回来。于是
    #   “退出登录”以前只能丢掉内存里的私钥（那挡住了**解密**），
    #   却挡不住**写操作**：上传 / 改块 / 追加 / 截断 / 删除只校验令牌，
    #   而令牌在 TTL（12 小时）内一直有效。令牌一旦泄露，
    #   受害者“退出登录”完全无法止损。
    #
    #   这里按**张**作废：`logout` 把这张令牌的 `jti` 记下来，
    #   `deps.current_user` 每次校验。
    #
    #   ★ 为什么**必须落库**（安全审计 I1 —— 原来那三条理由里第 ② 条站不住）：
    #     原注释写的是“重启后签名密钥会变，全部令牌本来就失效”。那句话只在
    #     **没设** ``VDS_SECRET_KEY`` 时成立（那种情况下密钥是随机生成的）。
    #     按生产惯例设了它之后，签名密钥**跨重启不变**、令牌照样有效，
    #     而撤销表却随进程消失 ⇒ 已经登出（或已经泄露）的令牌在重启后**复活**，
    #     最长能活到 ``token_ttl_minutes``。所以现在写进 ``revoked_tokens`` 表，
    #     内存这份只是一层缓存（少查库）。
    #
    #   ★ 两种撤销要分清：
    #     ① **按张**（登出）：点名 ``jti``；
    #     ② **按人**（改口令 / 停用 / 改角色）：JWT 是无状态的，服务端没有
    #        “这个人一共签过哪些令牌”的记录，所以只能退回**时间戳黑名单** ——
    #        记下“该用户在 T 时刻之前签发的全部作废”，校验时比 ``iat``。
    #        （安全审计 I2：改口令与停用是最典型的两条**止损**动作，
    #          原先它们只改库里的行、不碰已签出的令牌，被停用的用户在 TTL
    #          内照样能写数据。）

    def revoke_token(self, token: str) -> bool:
        """作废一张令牌。返回是否真的记下了（不可解析 / 已过期 / 没有 jti → False）。"""
        try:
            claims = decode_token(token, self.settings)
        except Exception:  # noqa: BLE001 - 解析不了就没什么可作废的
            return False
        return self.revoke_claims(claims)

    def revoke_claims(self, claims: dict) -> bool:
        """按 ``claims`` 记一条**按张**撤销。"""
        jti = claims.get("jti")
        if not jti:
            # 老令牌（签出时还没有 jti）没得按张作废 —— 只能等它自己过期。
            return False
        exp = int(float(claims.get("exp", 0) or 0))
        if exp and exp < time.time():
            return False  # 它已经过期了，记下来没有意义
        sub = str(claims.get("sub", "") or "")
        with self._lock, self.db.session() as db:
            if db.get(RevokedTokenRow, str(jti)) is None:
                db.add(RevokedTokenRow(jti=str(jti), sub=sub, exp_ts=exp))
                self._prune_revoked(db)
                db.commit()
            self._revoked[str(jti)] = float(exp)
        return True

    def revoke_user_tokens(self, username: str, *, reason: str = "") -> int:
        """**按人**撤销：把 ``username`` 在**此刻之前**签发的全部令牌作废。

        :returns: 本次记下的记录数（1 表示写了/更新了那条阈值记录）。

        ★ 实现是**时间戳黑名单**（理由见上面 ②）：不依赖“服务端记得签过哪些
          令牌” —— 它根本不记得。校验时拿令牌的 ``iat`` 与这条阈值比。

        ★ 为什么不拆成“只撤某些设备”：那要求在服务端维护“每个 jti 属于哪次
          登录”，等于把无状态令牌改造成有状态会话。**止损优先**：
          一次全撤，用户重登即可。
        """
        now = int(time.time())
        # 记录什么时候可以删：所有“此刻尚未签发”的令牌最多活到 now + TTL。
        horizon = now + int(self.settings.token_ttl_minutes) * 60
        key = f"*user*{username}"
        with self._lock, self.db.session() as db:
            row = db.get(RevokedTokenRow, key)
            if row is None:
                db.add(
                    RevokedTokenRow(
                        jti=key, sub=username, exp_ts=horizon, before_ts=now
                    )
                )
            else:
                row.exp_ts = horizon
                # ★ 取**更晚**的那个阈值：两次撤销之间签出的令牌也不能漏。
                row.before_ts = max(int(row.before_ts or 0), now)
            self._prune_revoked(db)
            db.commit()
            self._revoked_users[username] = now
        return 1

    def _user_revoked_before(self, sub: str) -> int:
        """该用户被“按人撤销”的签发时刻阈值（没有就 0）。缓存 + 查库兜底。"""
        if sub in self._revoked_users:
            return self._revoked_users[sub]
        with self.db.session() as db:
            row = db.get(RevokedTokenRow, f"*user*{sub}")
        ts = int(row.before_ts or 0) if row is not None else 0
        self._revoked_users[sub] = ts  # 0 也缓存（“查过了，没有”），免得每请求查库
        return ts

    def is_token_revoked(self, claims: dict) -> bool:
        """这张令牌是否已被作废（**按张** + **按人**两条路）。"""
        now = time.time()
        jti = claims.get("jti")
        if jti:
            exp = self._revoked.get(str(jti))
            if exp is None:
                with self.db.session() as db:
                    row = db.get(RevokedTokenRow, str(jti))
                exp = float(row.exp_ts) if row is not None else 0.0
                if exp:
                    self._revoked[str(jti)] = exp
            if exp:
                if exp < now:
                    # 它已经自然过期了 —— 顺手清掉，不必再拦
                    self._revoked.pop(str(jti), None)
                else:
                    return True
        sub = str(claims.get("sub", "") or "")
        if sub:
            before = self._user_revoked_before(sub)
            if before and int(float(claims.get("iat", 0) or 0)) <= before:
                return True
        return False

    def _prune_revoked(self, db) -> None:
        """清掉已过期的撤销记录（否则这张表会一直涨）。

        ★ 只管“什么时候可以删”：按张的看令牌自己的 ``exp_ts``，按人的看那条
          阈值记录的有效期。**不动**还没到期的记录。
        """
        now = int(time.time())
        rows = list(
            db.execute(select(RevokedTokenRow).where(RevokedTokenRow.exp_ts < now)).scalars()
        )
        for row in rows:
            db.delete(row)
            self._revoked.pop(row.jti, None)
            self._revoked_users.pop(row.sub, None)

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
            "session_key_location": (
                "★ 默认模型：服务端没有私钥 —— 登录不再解封，只把 "
                "users.sk_wrapped 的密文交给浏览器；解密与验证都在浏览器里完成"
                "（frontend/src/utils/crypto/）。"
            ),
            "note": (
                "块密钥用文件所有者的 SM2 公钥封装；用户私钥用他自己的口令"
                "包一层之后才入库。口令本身不落库、私钥明文从不落盘 ——"
                "所以“数据库被拿走”拿到的只有解不开的密文。\n"
                "  ★ 默认登录路径（server_key=false）**服务端不解封私钥**："
                "它只把私钥密文交给浏览器，解封在浏览器里用口令完成 ——"
                "此后服务端手里没有任何能解开文件的东西，"
                "它就算作恶也只能交出一份过不了浏览器验证的应答。\n"
                "  （显式传 server_key=true 才回到旧的服务端代管模型，仅供对比。）"
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

    def delta_fingerprint(self, file_id: tuple[str, str] | None = None) -> str:
        """当前 δ 的短指纹（12 位十六进制）: ``sha256("U:C:n")[:12]``。

        为什么必须有它、而不能只看 ``n``：**改块不改变 n，却会改变承诺 C**
        （见 :meth:`modify_block`）。证据是 δ 的函数，所以"n 没变"**不等于**
        "证据还有效"。界面要回答"这张卡作废没有"，只能按指纹判。

        它不是密码学承诺，只是给界面用的一把尺子。
        """
        store = self._require()
        if file_id is not None:
            d = store.delta_of(*file_id)
            return hashlib.sha256(f"{d.U}:{d.C}:{d.n}".encode()).hexdigest()[:12]
        each = sorted(
            hashlib.sha256(f"{d.U}:{d.C}:{d.n}".encode()).hexdigest()
            for d in store.deltas.values()
        )
        return hashlib.sha256("\n".join(each).encode()).hexdigest()[:12]

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

    def _register_nodes(self, db) -> list[str]:
        """把本次启动的节点名单补登进 ``node_registry``，返回**这次才加入的**。

        **新面孔**（表里没有的）= 这一次才加入的节点，``received_n`` 记 0 ——
        它天然是空的，闸门据此放行（见 :meth:`_verify_nodes_at_startup`）。
        以前登记过、后来被摘掉的，重新出现时当作重新加入（数据目录必须是干净的，
        否则下面的闸门会按 ``received_n`` 认出它并对不上）。

        .. important::

           ★ **摘掉一台机器时不会把它从登记表里删掉**（``retired`` 保持 False）。
           为什么：它的数据目录还在、``received_n`` 也还记着"我推到过第几块" ——
           过一会儿把它加回来（比如 4 → 2 → 6 这种来回调），它手里的东西
           仍然是有效的，闸门一比对就放行，**不用重传**。
           把它标成 retired/删掉反而会让那台机器回来时"被当成新的"，
           而它其实有数据 —— 那种状态会被闸门以"停在 n=4、协调者也是 n=4"放行，
           但登记表说它是新节点，界面上就会显示一句错的"新节点"。保持 False 更简单也更准。
        """
        joined: list[str] = []
        for nid in self.settings.node_ids:
            row = db.get(NodeRegistryRow, nid)
            if row is None:
                db.add(NodeRegistryRow(node_id=nid, received_n=0))
                joined.append(nid)
            elif row.retired:
                row.retired = False
                row.received_n = 0
                joined.append(nid)
        db.commit()
        return joined

    def joined_now(self) -> list[str]:
        """本次启动才加入的机器（见 :attr:`_joined_now`）。"""
        return list(self._joined_now)

    def _verify_nodes_at_startup(self, store: VectorStore, db) -> None:
        """启动时确认各节点与协调者停在同一个 ``n`` —— **两种模式都要查**。

        两种混用都必须拦住，而且**反方向那种更隐蔽**：

        * 跨进程模式读单进程的库 → 各节点的数据目录是空的；
        * 单进程模式读跨进程的库 → 协调者库里没有节点状态（跨进程模式下
          节点状态存在各节点自己的库里，``_persist_nodes`` 直接 return 了）。

        后者的表现是"能登录、能看到文件列表、一查询才 400" ——
        最难查的那种半死不活。宁可启动就拒，并且把原因说清楚。

        ★★ **新加入的空节点要放行**（这是"台数可调"的前提）：

        一台刚加入的节点天然停在 ``n = 0``，而一台**数据目录被删掉**的老节点
        也是 ``n = 0`` —— 在节点那一侧这两件事长得一模一样。区分它们靠登记表里的
        ``received_n``（协调者上次把它推到第几块）：

        * 新节点：``received_n = 0``，它自己也是 ``0`` ⇒ 一致，放行；
        * 老节点丢数据：``received_n = 10``，它却报 ``0`` ⇒ 不一致，**照旧拒绝**。

        所以放行不等于放宽：真正"少了数据"的机器仍然过不去。
        """
        if store.n == 0:
            return

        bad: list[str] = []
        down: list[str] = []
        #: 与协调者对齐的节点 —— 通过之后要把它们的 ``received_n`` 刷成当前
        #: **全局位置总数**（旧的“全系统一条向量”时它就是块的个数）。
        in_sync: list[str] = []
        # ★ 新方案里每份文件一条向量，所以闸门要比的是“**每份文件**的 n”，
        #   而不是一个全局的 n。节点侧 ``describe()`` 已经把每份文件各列一行
        #   （``vectors``），这里把两边的 ``offset -> n`` 对齐比。
        want_n = {
            int(store.delta_of(*fid).offset): int(store.delta_of(*fid).n)
            for fid in store.files
        }
        for row in store.transport.report():
            nid = row["node_id"]
            if row.get("unreachable"):
                down.append(nid)
                continue
            reg = db.get(NodeRegistryRow, nid)
            expect = reg.received_n if reg is not None else 0
            got = {int(v["offset"]): int(v["n"]) for v in row.get("vectors", [])}
            if got == want_n:
                in_sync.append(nid)
            elif not got and expect == 0:
                # 新加入、还没分到任何块 —— 合法。块照样只落在它加入之后写入的那些上。
                continue
            else:
                bad.append(
                    f"{nid} 停在 {sorted(got.items())}，协调者是 "
                    f"{sorted(want_n.items())}（登记表记的是全局位置数 {expect}）"
                )
                continue
            if not row.get("valid", True):
                bad.append(f"{nid} 声称持有的下标与实际密文对不上")

        if not bad and len(down) == len(store.node_ids):
            # 一台都没联系上 ⇒ 根本没东西可查，而且**令牌配错也是这个表现**
            # （每个请求都 401）。这种情况不能放过去。
            raise RuntimeError(
                "所有存储节点都联系不上（"
                + ", ".join(down)
                + "）—— 节点没起来？端口写错？或者 VDS_NODE_TOKEN 不一致？"
            )
        if not bad:
            # ★ 刷新"我推到第几块"。只刷**真正对齐**的那些：新加入的空节点
            #   保持 ``received_n = 0``，否则它下次启动就会被自己的登记表拒掉。
            for nid in in_sync:
                reg = db.get(NodeRegistryRow, nid)
                if reg is not None and reg.received_n != store.n:
                    reg.received_n = store.n
            db.commit()
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
        """从库里的行重建整个向量（**逐份文件**）。"""
        meta = {r.key: r.value for r in db.execute(select(MetaRow)).scalars()}
        # 分片偏移也要恢复。取模是为了容错：节点台数改过的话，
        # 旧偏移未必落在合法范围内，取模总比抛异常好（它只影响均匀度）。
        store._offset = int(meta.get(G_NODE_OFFSET, "0")) % max(1, len(store.node_ids))

        # ① 摘要：一份文件一行（含位置段）。
        for r in db.execute(select(FileDeltaRow)).scalars():
            fid = (r.owner, r.file_key)
            chunks = tuple(
                (int(a), int(b)) for a, b in json.loads(r.chunks_json or "[]")
            )
            store.deltas[fid] = Digest(
                U=int(r.delta_U),
                C=int(r.delta_C),
                n=int(r.delta_n),
                offset=int(r.offset),
                chunks=chunks,
            )

        # ② 位置段登记表 + 分量 / 主副本 / 副本表。
        #    ★ 必须按 (global_index 升序) 逐份文件喂给 registry：它靠“挨着上一段
        #      末尾就接着长”把原来的段形状复原回来（段永不回收，不会撞别人）。
        buckets: dict[int, list[BlockRow]] = {}
        for b in db.execute(select(BlockRow).order_by(BlockRow.global_index)).scalars():
            buckets.setdefault(b.file_id, []).append(b)
        for rows in buckets.values():
            rows.sort(key=lambda r: r.block_idx)
            first = rows[0]
            for r in rows:
                pos = int(r.global_index)
                store.registry.restore_position(first.owner, first.file_key, pos)
                store.values[pos] = int(r.element)
                store._holder[pos] = r.holder
                # 老行没写过 replicas（默认 "[]"）⇒ 退化成“只有主副本一份”。
                # 这样开副本之前传的文件照样能读，不会被新特性卡住。
                copies = tuple(json.loads(r.replicas or "[]")) or (r.holder,)
                if copies[0] != r.holder:
                    raise RuntimeError(
                        f"位置 {pos} 的副本列表 {list(copies)} "
                        f"与主副本 {r.holder!r} 不一致"
                    )
                store._replicas[pos] = copies

        # ③ 文件账目。
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
            entry = states.setdefault(r.node_id, {"blobs": {}, "vectors": []})
            entry["vectors"].append(
                {
                    "offset": int(r.offset),
                    "delta": (r.delta_U, r.delta_C, r.delta_n),
                    # ★ 位置段必须一起恢复（见 ``_persist_nodes``）。
                    "chunks": json.loads(r.delta_chunks or "[]"),
                    "st": (r.S_I, r.Lambda_I),
                    "I": r.I,
                    "FI": r.FI,
                    "blobs": {},
                }
            )
        # ★ 密文表（``node_blobs``）的键是**全局位置号**，而节点视图要的是
        #   **文件内局部块号** —— 这里用各文件的位置段反查一次。
        #   不做这一步的话，重启后节点会“声称持有下标 i、却查不到 i 的密文”，
        #   check 会当场判它不合法。
        pos_to_local: dict[int, tuple[int, int]] = {}
        for r in db.execute(select(FileDeltaRow)).scalars():
            d = Digest(
                U=int(r.delta_U),
                C=int(r.delta_C),
                n=int(r.delta_n),
                offset=int(r.offset),
                chunks=tuple(
                    (int(a), int(b)) for a, b in json.loads(r.chunks_json or "[]")
                ),
            )
            for li, g in enumerate(d.positions):
                pos_to_local[int(g)] = (int(r.offset), li)
        for b in db.execute(select(NodeBlobRow)).scalars():
            if b.node_id not in states:
                continue
            hit = pos_to_local.get(int(b.global_index))
            if hit is None:
                continue
            off, li = hit
            for v in states[b.node_id]["vectors"]:
                if int(v["offset"]) == off:
                    v["blobs"][li] = b.ciphertext
                    break

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
        """落盘**逐文件**摘要与位置段（名字保留：它管的是“账目”这一层）。

        ★ 新方案里摘要是逐文件的，所以这里不再是一行，而是每份文件一行。
        库里多出来、而 store 里已经没有的行（删过文件）会被删掉。
        """
        store = self._require()
        seen: set[tuple[str, str]] = set()
        for fid, delta in store.deltas.items():
            owner, file_key = fid
            seen.add(fid)
            chunks = json.dumps([[int(a), int(b)] for a, b in delta.chunks])
            row = db.get(FileDeltaRow, (owner, file_key))
            if row is None:
                db.add(
                    FileDeltaRow(
                        owner=owner,
                        file_key=file_key,
                        offset=int(delta.offset),
                        delta_U=str(delta.U),
                        delta_C=str(delta.C),
                        delta_n=int(delta.n),
                        chunks_json=chunks,
                    )
                )
            else:
                row.offset = int(delta.offset)
                row.delta_U = str(delta.U)
                row.delta_C = str(delta.C)
                row.delta_n = int(delta.n)
                row.chunks_json = chunks
        for row in db.execute(select(FileDeltaRow)).scalars():
            if (row.owner, row.file_key) not in seen:
                db.delete(row)
        # 分片偏移（全系统唯一，放 meta 表）
        mrow = db.get(MetaRow, G_NODE_OFFSET)
        if mrow is None:
            db.add(MetaRow(key=G_NODE_OFFSET, value=str(store._offset)))
        else:
            mrow.value = str(store._offset)

    def _persist_nodes(self, db, indices: Sequence[int]) -> None:
        """落盘节点状态（**仅本地模式**）；跨进程时节点自己管。

        ``indices`` 是**这次变更涉及的**下标 —— 只写这几行的密文，
        不然每次上传都要重写整张 ``node_blobs``，n=1024 时就是 1024 行。

        .. important::

           它是 **upsert**，不是纯插入。改块改的是**已有**下标，
           而 ``node_blobs`` 的主键是 ``(node_id, global_index)`` ——
           拿 ``db.add`` 去写就会直接撞主键。

           而且漏写这类问题**不会当场暴露**：进程内的状态是对的，
           要等重启后从库里重建，才会以「验证时本端算出的分量
           对不上承诺」这种极难归因的形式冒出来。
        """
        store = self._require()
        exporter = getattr(store.transport, "export_states", None)
        if exporter is None:
            return
        snap = exporter()

        db.execute(delete(NodeStateRow))
        for nid, blob in snap.items():
            for v in blob["vectors"]:
                U, C, n = v["delta"]
                S_I, Lam = v["st"]
                db.add(
                    NodeStateRow(
                        node_id=nid,
                        offset=int(v["offset"]),
                        delta_U=str(U),
                        delta_C=str(C),
                        delta_n=int(n),
                        # ★ 位置段必须落库：重启后重建节点视图就靠它，
                        #   丢一次，之后每一次更新都会因 e_i 取错而失败。
                        delta_chunks=json.dumps(
                            [[int(a), int(b)] for a, b in v.get("chunks", ())]
                        ),
                        S_I=str(S_I),
                        Lambda_I=str(Lam),
                        I_json=json.dumps(list(v["I"])),
                        FI_json=json.dumps(list(v["FI"])),
                    )
                )
        for i in indices:
            gidx = int(i)
            # ★ 每一份副本都要落库。只写主副本的话，重启后别的副本拿到的
            #   还是旧密文 —— 改块时尤其致命（验证会当场因本端算出的分量
            #   与承诺不符而失败）。
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
    # 改服务器台数 —— 缩容前的块重分发
    #
    # ★★ 时机是这条需求里最关键的一件事：**必须在重启之前搬**。
    #
    #    台数改小之后、重启之前，被摘掉的那几台**还活着**，它们手里的密文
    #    还能读出来，而它们持有的证据也还能拆出 π_Q。一旦重启，那些进程就没了
    #    —— 那时再想搬，源已经下线，"搬到哪儿"根本无从谈起，块就是真丢了。
    #
    #    所以在 save/restart 那两个路由里都是「**先搬块、再写配置**」。
    # -------------------------------------------------------------------

    def _live_node_ids(self, store: VectorStore) -> set[str]:
        """现在**真的答得上话**的机器（读过 ``report``，不重试）。"""
        return {
            row["node_id"]
            for row in store.transport.report()
            if not row.get("unreachable")
        }

    def plan_redistribute(self, keep: Sequence[str] | None = None) -> dict:
        """**只算不动**：台数变小时，哪些块要搬、哪些块搬不动。

        :param keep: 块最终要落在哪几台。``None`` = 当前全部机器
            （那是一次"原地重排"，正常用不到）。

        ``orphan`` 与 ``lost`` 的区别是这次需求的全部意义所在，别混：

        * ``orphan`` —— 留在 ``keep`` 里的副本不够 ``replica_factor`` 份，
          **但至少有一个持有者现在活着**，所以从它那儿取一份搬过去就好。
          块内容、全局下标、承诺 **一个字节都不变**；
        * ``lost`` —— 这些块**每一份副本都在联系不上的机器上**，
          搬不动。这时候正确的做法是**先把那几台起起来**，而不是硬收缩。

        两者都要如实报出来：``lost`` 非空时外壳会拦一次（除非使用者确认
        "我知道会丢"），因为那是唯一一条真会丢数据的路。
        """
        store = self._require()
        pool = tuple(keep) if keep else tuple(store.node_ids)
        pool_set = set(pool)
        alive = self._live_node_ids(store)
        #: 一台都不剩的话就没有"副本"可言 —— 退化成一个"能不能读到"。
        wanted = min(store.replica_factor, len(pool))

        orphan: list[int] = []
        lost: list[int] = []
        for i in range(store.n):
            holders = store.replicas_of(i)
            kept = [h for h in holders if h in pool_set]
            if len(kept) >= wanted:
                continue
            need = wanted - len(kept)
            sources = [h for h in holders if h in alive]
            cands = [x for x in pool if x in alive and x not in holders]
            if not sources or len(cands) < need:
                lost.append(i)
            else:
                orphan.append(i)

        return {
            "keep": list(pool),
            "remove": [n for n in store.node_ids if n not in pool_set],
            "alive": sorted(alive),
            "down": [n for n in store.node_ids if n not in alive],
            "replica_factor": store.replica_factor,
            "wanted": wanted,
            "blocks": store.n,
            "orphan": orphan,
            "lost": lost,
            "safe": not lost,
        }

    def redistribute(
        self,
        *,
        reason: str = "",
        keep: Sequence[str] | None = None,
    ) -> dict:
        """把块重新铺到 ``keep`` 那几台上 —— **一块都不丢**（除非真的搬不动）。

        走的原语链（每一步都是论文里现成的，没有新造算法）：

        1. 向**当前持有者**（它现在还活着）要 :math:`(F_Q, \\pi_Q, \\text{密文})`
           —— 就是一次普通的 :meth:`~core.transport.NodeTransport.retrieve`；
        2. 交给目标节点做 ``AddStorage``
           （:meth:`~core.transport.NodeTransport.adopt`）—— **由目标节点自己
           验一遍凭证再合并**，协调者只是搬运工。

        ★ 为什么不走 :meth:`~core.node_state.NodeState.absorb`：那一条要求
        ``assigned ⊆ K``（只收"本次追加刚产生的"位置），而这些位置早就承诺过了，
        旧摘要里根本没有它们 —— 两条路的**前置材料**不一样，不是实现上的偏好。

        ★ 这里**不动** ``δ``：``n`` 不变、``C`` 不变，变的只是"谁持有哪些下标"。
        所以全局承诺、所有已发出的证据、所有块的分量 **全部继续有效** ——
        这正是"搬块"与"重传文件"的根本区别。

        :returns: ``{"moved_blocks", "moved_copies", "lost", "keep", "plan", ...}``
        """
        store = self._require()
        pool = tuple(keep) if keep else tuple(store.node_ids)
        pool_set = set(pool)
        alive = self._live_node_ids(store)
        wanted = min(store.replica_factor, len(pool))

        # -- 1) 决定搬什么：按「来源 → 目标」分组，一次搬运一批 ------------
        groups: dict[tuple[str, str], list[int]] = {}
        lost: list[int] = []
        for i in range(store.n):
            holders = store.replicas_of(i)
            kept = [h for h in holders if h in pool_set]
            need = wanted - len(kept)
            if need <= 0:
                continue
            sources = [h for h in holders if h in alive]
            cands = [
                x for x in pool if x in alive and x not in holders and x not in kept
            ]
            if not sources or len(cands) < need:
                lost.append(i)
                continue
            src = sources[0]
            for tgt in cands[:need]:
                groups.setdefault((src, tgt), []).append(i)

        # -- 2) 真搬：取凭证 → 交给目标节点自己验、自己合并 ----------------
        moved_copies = 0
        failed: list[dict] = []
        for (src, tgt), idxs in sorted(groups.items()):
            Q = tuple(sorted(idxs))
            try:
                _claimed, proof, cts = store.transport.retrieve(src, Q)
                # ★ 值同样**不采信**来源节点声称的那份：搬过来的密文自己算。
                #   目标节点收到后会照常做一次承诺验证，用的就是这组推导值。
                values = [vector_element(ct) for ct in cts]
                store.transport.adopt(
                    tgt,
                    positions=Q,
                    values=values,
                    proof=proof,
                    blobs=dict(zip(Q, cts, strict=True)),
                )
            except (TransportError, ValueError) as exc:
                # 搬失败**不留补推现场**：δ 一个字节都没动，所以这不是
                # "全网不一致"，只是"这批块没搬成" —— 如实记进 lost 就好
                # （补推机制是给"δ 已推进、但有台没跟上"用的）。
                failed.append({"from": src, "to": tgt, "indices": list(Q), "why": str(exc)})
                lost.extend(Q)
                continue
            moved_copies += len(Q)
            for i in Q:
                store.set_replicas(i, tuple(store.replicas_of(i)) + (tgt,))

        # -- 3) 目的一侧清场：把已经不在集群里的名字从副本表里摘掉 ----------
        #    两种都做：① 搬成功但列表里还夹着旧名字；② 那份副本本来就够，
        #    但主人是要被摘掉的机器。摘完之后才知道"哪些块真的没救"。
        retargeted: list[int] = []
        for i in range(store.n):
            reps = tuple(store.replicas_of(i))
            pruned = tuple(h for h in reps if h in pool_set)
            if not pruned:
                # 一份都没留下 —— plan 里已经把它记进 lost 了，这里保持原样，
                # 免得把"还有一份在掉线机器上"抹成"谁都不持有"。
                continue
            if pruned != reps:
                store.set_replicas(i, pruned)
                retargeted.append(i)

        moved_blocks = sorted({i for idxs in groups.values() for i in idxs})
        result = {
            "reason": reason,
            "keep": list(pool),
            "remove": [n for n in store.node_ids if n not in pool_set],
            "wanted": wanted,
            "blocks": store.n,
            "moved_blocks": len(moved_blocks),
            "moved_copies": moved_copies,
            "retargeted": len(retargeted),
            "lost": sorted(set(lost)),
            "failed": failed,
            "indices": moved_blocks,
        }

        # -- 4) 落库：协调者这边的账必须与上面改的内存逐字一致 --------------
        if moved_blocks or retargeted:
            with self.db.session() as db:
                for b in db.execute(select(BlockRow)).scalars():
                    reps = tuple(store.replicas_of(b.global_index))
                    # ★ ``_reload`` 有一条不变式：``holder`` 必须等于
                    #   ``replicas`` 的**第一项**，否则下次启动直接抛
                    #   "副本列表与主副本不一致"。所以这两列必须一起写。
                    b.replicas = json.dumps(list(reps))
                    if reps:
                        b.holder = reps[0]
                # ★ 光改内存不够：**节点自己的状态也要落盘**。
                #   单进程模式里节点状态存在协调者库里（``node_states`` /
                #   ``node_blobs``），只改内存的话重启后目标节点又是空的，
                #   而协调者却以为它存着 —— 读的时候才会炸。
                self._persist_nodes(db, sorted(set(moved_blocks) | set(retargeted)))
                # ★★ ``commit`` 必须在 ``with`` **里面**。
                #    ``Database.session()`` 是个 ``sessionmaker``，不是
                #    ``@contextmanager`` —— 退出 ``with`` 时它会关闭 session，
                #    未提交的改动当场回滚。这个坑踩过一次，"搬到一半的账"
                #    就是这么静默丢掉的。
                db.commit()
        if reason:
            self._last_redistribute = result
        return result

    def last_redistribute(self) -> dict | None:
        """最近一次重分发的交代（进程内的；界面上显示一次就够）。"""
        return self._last_redistribute

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
                    # ★★ 兜底：这个 (owner, file_key) 上的回滚演示记录一律清掉。
                    #    正常路径上删文件时已经清了；但万一有窗口漏过来
                    #    （库被手工改过、或旧版本删文件时没清），
                    #    新上传的这份就会被塞进旧密文 —— 这里是最后一道闸。
                    db.execute(
                        delete(ReplayRow).where(
                            ReplayRow.owner == owner, ReplayRow.file_key == file_key
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
                frow = self._finish_or_pending(_finish)
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
                    #   "本端算出的分量对不上承诺" 的形式炸出来（见 _persist_nodes 的注释）。
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
                return self._finish_or_pending(_finish)

    def zero_block(self, owner: str, file_key: str, block_idx: int) -> dict:
        r"""把第 ``block_idx`` 块的内容换成**等长的全 0 字节**。

        它**不是删除**，是改块（论文 §8.2 的 ``op = mod``）：块仍在向量里、
        仍占原来那个下标、仍占节点存储，``n`` 不变；变的只有内容、
        该位置的承诺分量、以及版本号（+1）。

        ★ 为什么用**原来的长度**：明文长度是逐块记的（``plain_lengths``），
          保持等长之后连"这块被清过"也不从长度上泄露；而且块大小上限是
          上传时切好的，等长一定不会越界（``modify`` 里有"不超过单块上限"
          与"不能为空"两道校，等长全 0 两道都天然满足）。

        ★ 为什么**复用** :meth:`modify_block` 而不是另写一套：改块这条路
          （重新加密 → 算新分量 → 推给全网 → 重新封装密钥 → 写库 → 补推）
          只能有一份定义。两份的结果就是"一条把账收干净、另一条漏一半"，
          而那是最难查的一类偏差。零清与改块**唯一**的区别只是"写什么内容"。

        :returns: 与 :meth:`modify_block` 同构（``global_index`` / ``block_idx``
            / ``plain_len`` / ``version``）。
        """
        with self._lock:  # RLock：下面 modify_block 还会再取一次，可重入
            store = self._require()
            rec = store.files.get((owner, file_key))
            if rec is None:
                raise NotFound(f"文件 {owner}/{file_key} 不存在")
            if not 0 <= block_idx < len(rec.indices):
                raise OutOfRange(
                    f"块号 {block_idx} 越界：{owner}/{file_key} 只有 {len(rec.indices)} 块"
                )
            length = rec.plain_lengths[block_idx]
        return self.modify_block(owner, file_key, block_idx, b"\x00" * length)

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
        "本端算出的分量对不上承诺"的形式炸出来。
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
                    #   漏了这一步进程内一切正常、重启后才会以"本端算出的分量对不上承诺"炸出来。
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
                return self._finish_or_pending(_finish)

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
                out = self._finish_or_pending(_finish)
            out["dropped_from"] = n_before
            return out

    def delete_file(self, owner: str, file_key: str) -> dict:
        r"""删掉一份文件 —— **连同它之后写进向量的块**（只有所有者能做）。

        ★ 为什么"连同后面"：``del`` 只支持向量末尾的连续区间（见
          :meth:`core.store.VectorStore.truncate` 的说明）。而各文件在向量上
          占的是连续区间、顺序 = 上传顺序，所以"从这份文件的第一块删到向量
          末尾"恰好是一次合法的 ``del`` —— 代价是**它之后上传的文件也一起没了**。

        ★ 因此这里有两条硬检查：

        * **授权**：要连带删掉的必须**全是这位所有者自己的**文件。否则就是
          替别人删数据 —— 返回 409 并说清是哪几份挡着，让所有者自己来删；
        * **如实交代**：返回值里列出被连带删掉的文件名，界面必须显示出来，
          不做静默连带。

        :returns: ``{"deleted_files", "dropped_blocks", "dropped_indices",
            "blocks_after"}``
        """
        with self._lock:
            store = self._require()
            rec = store.files.get((owner, file_key))
            if rec is None:
                raise NotFound(f"文件 {owner}/{file_key} 不存在")
            n_before = store.n
            g0 = int(rec.indices[0])
            # ★ K 与"连带名单"都在删之前就算好：这样 _finish 在**补推路径**上
            #   也能用（那时 store 里的文件账目已经被摘掉了，已经推不出来）。
            #
            #   ⚠️ K **不能**写成 `tuple(range(g0, n_before))`（"从这份文件的第一块
            #   一直数到向量末尾"）：位置段**永不回收**，删过的位置会留下空洞。
            #   实测一个"删掉又重传"的探针文件 —— 它拿到位置 11、而块数也是 11，
            #   于是 range(11, 11) 是空集，自检当场报"连带名单不一致"。
            #   新方案下删一份文件就是删**它自己的全部位置**，核心层正是这么算的
            #   （见 core/store.py::delete_from），两边必须用同一个来源。
            K = tuple(rec.indices)
            doomed = ((owner, file_key),)
            foreign = sorted(k for k in doomed if k[0] != owner)
            if foreign:
                raise Conflict(
                    "方案只允许删「向量末尾」的连续区间，而这份文件后面还压着"
                    f"**别人的** {len(foreign)} 份文件："
                    + "、".join(f"{o}/{k}" for o, k in foreign[:5])
                    + "。要删这份文件，得请它们的所有者先删掉"
                    "（或者整库重置）。"
                )

            def _finish() -> dict:
                """落库：删块行、删文件行、抹掉单进程模式下的节点密文。"""
                with self.db.session() as db:
                    for o, k in doomed:
                        fid = db.execute(
                            select(FileRow.id).where(
                                FileRow.owner == o, FileRow.file_key == k
                            )
                        ).scalar_one_or_none()
                        if fid is None:  # pragma: no cover - 与账目一致的兜底
                            continue
                        db.execute(delete(BlockRow).where(BlockRow.file_id == fid))
                        db.execute(delete(FileRow).where(FileRow.id == fid))
                        # ★★ 回滚演示记录**必须一起删**：它按
                        #    `(owner, file_key, block_idx)` 索引，而 `file_key` 可复用 ——
                        #    留着的话，下一次用同一个标识上传另一份文件，那几块会被
                        #    塞回**这份旧文件**的密文，客户端当场报“本地验证不通过”
                        #    （实测踩过；数据库里能看到“files 里没有、replay_blocks 里
                        #    还躺着”的幽灵记录）。
                        db.execute(
                            delete(ReplayRow).where(
                                ReplayRow.owner == o, ReplayRow.file_key == k
                            )
                        )
                    # 兜底：这个区间里的块行一个都不许剩（含不属于任何文件账目的）
                    db.execute(delete(BlockRow).where(BlockRow.global_index.in_(K)))
                    # ★ 单进程模式下节点密文是协调者代存的，也要一起抹掉：
                    #   只删 blocks 会留下孤儿密文，重启后 import_states 会把它们
                    #   读回节点 ⇒ 节点自检"下标与密文对不上"炸掉。
                    db.execute(
                        delete(NodeBlobRow).where(NodeBlobRow.global_index.in_(K))
                    )
                    self._persist_globals(db)
                    # 节点状态整表重写（它们的 I 变小了，甚至全空）；密文不加不减
                    self._persist_nodes(db, [])
                    db.commit()
                return {
                    "deleted_files": [f"{o}/{k}" for o, k in doomed],
                    "dropped_blocks": len(K),
                    "dropped_indices": list(K),
                    "blocks_after": store.n,
                }

            try:
                got_K, got_doomed = store.delete_from(owner, file_key)
            except WriteError:
                if store.pending_write() is not None:
                    self._pending_finish = _finish
                raise
            except KeyError as exc:
                raise NotFound(str(exc)) from exc
            except ValueError as exc:
                raise OutOfRange(str(exc)) from exc
            if got_K != K or set(got_doomed) != set(doomed):  # pragma: no cover
                # ★ 只说"不一致"而不说**差在哪**，等于没说：这条自检第一次真的
                #   触发时，我盯着它只能猜（它连是哪两个集合都没打出来）。
                #   把两边的数和上下文一起摆出来。
                raise RuntimeError(
                    "删之前算出的连带名单与核心层报的不一致 —— 这是实现自检：\n"
                    f"  核心层：K={list(got_K)}"
                    f"  文件={sorted('/'.join(k) for k in got_doomed)}\n"
                    f"  这里算：K={list(K)}"
                    f"  文件={sorted('/'.join(k) for k in doomed)}\n"
                    f"  这份文件的第一块 g0={g0}，删前向量长度 n={n_before}，"
                    f"账目里的块数={len(rec.indices)}"
                )

            with stage("落库（删除文件与块）"):
                out = self._finish_or_pending(_finish)
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
            except KeyError as exc:
                # ★ 下标的**空洞**（从未分配 / 删文件留下的遗弃位）——
                #   `core/store.py::_group_by_file` 抛的是 `KeyError`。不接住它
                #   就会一路落到兜底处理器变成 500「服务器内部错误」，把后端
                #   自己写好的那句中文原因（“段永不回收”）丢掉，用户无法定位。
                #   同类输入在 `/api/query/verify-batch` 上返回的就是 400，口径要一致。
                #   ⚠ 用 `exc.args[0]`：`str(KeyError)` 会多带一对引号。
                raise OutOfRange(exc.args[0] if exc.args else str(exc)) from exc
            except ValueError as exc:
                raise OutOfRange(str(exc)) from exc
            return {
                #: 这份证据覆盖到的**存活块数**（= 各文件块数之和）。
                #:
                #: ⚠️ 它**不含**历史空洞，所以**不是**“合法下标的上界” ——
                #: 删过文件之后它可能**小于**某些仍然有效的下标（见
                #: `core/registry.py::total_blocks`：段永不回收、位置允许稀疏）。
                #: 要判“下标是不是用超了”，看 ``/api/status`` 的
                #: ``position_budget_used``（= ``registry.next_offset``）。
                #: 新方案里每份文件自己一条向量，所以这里只是一个总量；
                #: 每份文件自己的 ``n`` / 指纹在下面的 ``files`` 里。
                "delta_n": store.n,
                #: 整库账目的指纹 —— 任何一份文件的 (U, C, n) 变了它都会变。
                "delta_fp": self.delta_fingerprint(),
                #: ★ 这次查询覆盖到哪些文件、各自是什么状态（前端按
                #:   “文件 / 第几块”展示，并据此判断手里的旧证据作废没有）。
                "files": [
                    {
                        "owner": own,
                        "file_key": fk,
                        "offset": store.delta_of(own, fk).offset,
                        "n": store.delta_of(own, fk).n,
                        "delta_fp": self.delta_fingerprint((own, fk)),
                    }
                    for own, fk in sorted(
                        {(r.owner, r.file_key) for r in r.refs}
                    )
                ],
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
                # ★ 只有一个结论：``ok`` 就是承诺验证的结论。值是本端从取回的
                #   密文**自己算**出来的（见 ``core/store.py::_collect``），所以
                #   它已经把"交付的字节对不对得上承诺"一并盖住了。
                "ok": r.ok,
                "holders": {str(k): v for k, v in r.holders.items()},
                "refs": [
                    {
                        # ★ 全局位置 = 该文件的第 block_idx 块的位置。
                        #   新方案里“文件内块号”与“全局位置”是两套下标，
                        #   换算是 positions_of(...)[block_idx]。
                        "global_index": store.registry.positions_of(
                            ref.owner, ref.file_key
                        )[ref.block_idx],
                        "owner": ref.owner,
                        "file_key": ref.file_key,
                        "block_idx": ref.block_idx,
                    }
                    for ref in r.refs
                ],
                "cert_count": r.cert_count,
                "nodes_used": list(r.node_used),
            }

    @staticmethod
    def normalize_block_plan(plan, n_targets: int) -> list[list[int] | None]:
        """把 ``block_indices`` 的两种形态归一成“每个 target 一份块号清单”。

        * ``None`` / 空         → 每个 target 都是 ``None``（取整份文件）；
        * ``[0, 1]``            → 每个 target 都用 ``[0, 1]``（**共用**，老语义）；
        * ``[[0,1], [5,6]]``    → **一一对应**；长度必须等于 target 数，否则 400。

        ★ 为什么允许两种（而不是直接换成新形态）：这个接口已经在用，
          而“多份文件取**同样**的块号”是个真实且更省事的用法（一份文件时
          两种写法完全等价）。但**聚合出来的卡**只有新形态能表达 ——
          它每份文件覆盖的块号是**不同**的。
        """
        if not plan:
            return [None] * n_targets
        first = plan[0]
        if isinstance(first, (list, tuple)):
            groups = [list(g) for g in plan]
            if len(groups) != n_targets:
                raise OutOfRange(
                    f"block_indices 给了 {len(groups)} 组，但要查 {n_targets} 份文件 —— "
                    "这种写法必须与 targets **一一对应**"
                )
            for g in groups:
                if len(g) > MAX_INDICES:
                    raise OutOfRange(f"一组块号最多 {MAX_INDICES} 个（收到 {len(g)} 个）")
            return groups
        shared = list(plan)
        if len(shared) > MAX_INDICES:
            raise OutOfRange(f"块号最多 {MAX_INDICES} 个（收到 {len(shared)} 个）")
        return [shared] * n_targets

    def query_files(
        self,
        targets: Sequence[tuple[str, str]],
        block_indices: Sequence[int] | Sequence[Sequence[int]] | None = None,
    ) -> dict:
        """一次查询**若干个文件** —— 设计 B 下这只需要一份证据。

        :param block_indices: 块序号，两种形态（见 :meth:`normalize_block_plan`）：
            ``[0, 1]`` 是**共用**（每个文件都取这几块），
            ``[[0, 1], [5]]`` 是**与 ``targets`` 一一对应**。
            越界的块号自动跳过；给 ``None`` 就取整个文件。
        """
        plans = self.normalize_block_plan(block_indices, len(targets))
        with self._lock:
            store = self._require()
            picked: list[int] = []
            detail: list[dict] = []
            for (owner, file_key), plan in zip(targets, plans):
                got = store.file_indices(owner, file_key)
                if not got:
                    raise NotFound(f"文件 {owner}/{file_key} 不存在")
                if plan is None:
                    chosen = list(got)
                else:
                    chosen = [
                        got[k] for k in plan if 0 <= k < len(got)
                    ]
                picked.extend(chosen)
                d = store.delta_of(owner, file_key)
                detail.append(
                    {
                        "owner": owner,
                        "file_key": file_key,
                        # ★ 统一到「**文件 / 第几块**」这套话语 —— 前端不该看见
                        #   全局位置号（那是记账用的内部坐标）。全局位置仍然给出来，
                        #   供需要精确引用位置的人用。
                        "offset": int(d.offset),
                        "n": int(d.n),
                        "block_count": len(got),
                        "delta_fp": self.delta_fingerprint((owner, file_key)),
                        # ★ 这一份**实际取了哪些块** —— 用归一化后的 `plan`，
                        #   不是请求里那份笼统的 `block_indices`。
                        #   多份文件时请求可能是"一一对应"的形态（`[[0,1],[5,6]]`），
                        #   直接拿它算会把它当成**一组**块号，`int(k)` 收到 list ——
                        #   实测就是这里崩的（500: `'<=' not supported between
                        #   int and list`）。
                        "block_indices": [
                            int(k)
                            for k in (range(len(got)) if plan is None else plan)
                            if 0 <= k < len(got)
                        ],
                        "indices": [int(i) for i in chosen],
                    }
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
            vals_I = [_to_int(v, what="values 里的值") for v in evidence["values"]]

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

            # ★ 按文件取 crs_n：新方案里每份文件一条向量，素数视图必须与
            #   这批下标所属的那份文件一致。跨文件就先报错 ——
            #   拿另一份文件的视图去 disagg 会算出一个“看起来正常”的证据。
            try:
                _refs = [store.describe(g) for g in I]
            except KeyError as exc:
                # ★ 空洞下标（从未分配 / 删文件留下的遗弃位）—— `store.describe`
                #   抛的是 `KeyError`。不接住它会一路变成 500「服务器内部错误」，
                #   而 `/api/query` 上同类输入返回的是 400（口径要一致）。
                #   ⚠ 用 `exc.args[0]`：`str(KeyError)` 会多带一对引号。
                raise OutOfRange(exc.args[0] if exc.args else str(exc)) from exc
            _fids = sorted({(r.owner, r.file_key) for r in _refs})
            if len(_fids) != 1:
                raise OutOfRange(
                    f"这套接口一次只支持「一份文件」的下标，"
                    f"收到的证据覆盖了 {len(_fids)} 份文件：{_fids}"
                )
            # ★ 这份文件自己的摘要必须**接住**：它有三个用处 —— 取 crs_n、
            #   交给 ClientNode 去验、以及报「请重新取一次」时读它的 n。
            #   写成 `crs_n = ...crs_n_for(store.delta_of(*_fids[0]))` 看着等价，
            #   但下面构造 ClientNode 时会引用一个不存在的 _fd（NameError → 500）。
            _fd = store.delta_of(*_fids[0])
            crs_n = store.session.crs_n_for(_fd)

            # ★★★ 两套下标口径，别混：``/api/query`` 的 ``indices`` / ``values``
            #   是**全局位置号**（界面卡片上显示的就是它），而 ``proof.I`` 与
            #   CRS 的指数是**文件内局部块号**，两者靠
            #   ``位置 = _fd.positions[局部号]`` 换算。
            #
            #   ``svc.disagg`` 算的是 :math:`S_I^{e_K}`，指数必须走**局部**号 ——
            #   拿全局号进去，只有 ``offset = 0`` 的那份文件恰好相等，其余文件
            #   要么报 ``S_I 校验失败``、要么报 ``下标越界``。
            #   （实测踩过：池子里除第一份文件外「分解」全废，而报错还赖到
            #   “旧的 δ”头上 —— 卡片明明是刚取的。）
            loc = {g: i for i, g in enumerate(_fd.positions)}
            stray = sorted(set(I) - set(loc))
            if stray:
                raise OutOfRange(
                    f"证据里的下标 {stray[:8]} 不属于这份文件 {_fids[0]}："
                    f"它占的位置是 {list(_fd.positions)[:4]}…（共 {len(loc)} 块）。\n"
                    "  传进来的 I 必须是**全局位置号**（即 /api/query 响应里的 "
                    "indices），不是文件内局部块号。"
                )
            I_loc = [loc[g] for g in I]
            K_loc = [loc[g] for g in K_set]
            pi_I = Opening(
                _to_int(evidence["S_I"], what="S_I"),
                _to_int(evidence["Lambda_I"], what="Lambda_I"),
                as_index_set(I_loc),
            )
            with stage("拆出子集证据（svc.disagg）"):
                pi_K = svc_disagg(crs_n, I_loc, vals_I, pi_I, K_loc)

            val_of = dict(zip(I, vals_I))
            F_K = tuple(val_of[i] for i in K_set)
            with stage("验证拆出来的证据（VerRetrieve）"):
                report = ClientNode(store.session, _fd).ver_retrieve(
                    list(K_loc), list(F_K), pi_K
                )
            if not report.ok:
                # 同样：会原样弹给前端，不用 Markdown 星号。
                raise OutOfRange(
                    f"分解出来的证据验不过：{report.message}\n"
                    f"  最可能的原因是「这份证据是在旧的 δ 上取的」——\n"
                    f"  上传会让全局块数 n 变、改块会让承诺 C 变，两者都会让"
                    f"之前取到的证据失效。\n"
                    f"  另一个可能是这张卡的 S_I/Lambda_I 与 indices 不是"
                    f"同一次取的（或者被改过）。\n"
                    f"  请重新取一次（当前 n = {_fd.n}）。"
                )

            # 响应与 /api/query **保持同构** —— 前端那张卡片能直接复用，
            # 不必为"分解出来的证据"写第二套渲染。
            #
            # 几个字段如实说明：
            #   * refs / holders —— 分解没向节点要东西，但协调者**本来就知道**
            #     谁持有哪一块，照填即可；
            #   * cert_count = 0、nodes_used = [] —— 分解确实一份凭证都没收，
            #     填假数字才是骗人。
            return {
                #: 这份结论覆盖到的**全局位置总数**。
                "delta_n": store.n,
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
                "holders": {str(i): store.holder_of(i) for i in K_set},
                "refs": [
                    {
                        "global_index": store.registry.positions_of(
                            ref.owner, ref.file_key
                        )[ref.block_idx],
                        "owner": ref.owner,
                        "file_key": ref.file_key,
                        "block_idx": ref.block_idx,
                    }
                    for ref in [store.describe(g) for g in K_set]
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
        r"""**批量验证**：一次验池子里的多份证据 —— 判据是它们**共享同一个承诺**。

        是不是“同一份文件”不是关键，**哪条向量**才是：

        * 卡只涉及**同一份文件** → 用那份文件的 ``(U_f, C_f)``；
        * 卡涉及**多份文件**（即「聚合选中」归约出来的）→ 用**合并向量**的
          ``(U' = g^E, C' = ∏_f C_f^{E/E_f})``，``P`` = 这些文件全部位置的并集
          —— 与 :meth:`core.store.VectorStore._prove_merged` **同一套构造**
          （见 ``core/store.py::universe_for``）。一份文件的合并向量恰好退化成它自己。

        .. important::

           **不同宇宙的卡不能混**：方程右边的 ``U'``/``C'`` 是所有份共享的，
           学位论文 ``Def. 29`` 的聚合正确性原文就是「any commitment ``C`` …
           Ver(pp, ``C``, J, v_J, π_J) = 1」。
           所以这里按“卡自己涉及的文件集合”分组，发现多组时**明确报错**
           （说清是哪几组），而不是硬算出一条没人验得了的方程。

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
            # ---- ① 这批卡“在哪条向量上” ----
            #
            # ★★ 判据不是“几份文件”，而是“共享哪个 (U', C')”：
            #   * 只涉及同一份文件 → 那份文件自己的向量（快路，直接用 δ.U / δ.C）；
            #   * 涉及多份文件（「聚合选中」归约出来的）→ 合并向量。
            #   两者用的是同一段构造：core/store.py::universe_for。
            #
            #   不同宇宙的卡**不能混** —— 方程右边的 U'/C' 是所有份共享的
            #   （Def. 29 的前提就是同一个 C）。所以按“卡自己涉及的文件集合”
            #   分组，发现多组就明确报错，而不是硬算出一条没人验得了的方程。
            try:
                refs = {
                    int(i): store.describe(int(i))
                    for i in {int(i) for c in cards for i in c.get("indices", ())}
                }
            except Exception as exc:  # noqa: BLE001 —— 下标越界等一律当“用户传错”
                raise OutOfRange(f"卡片里有不存在的下标：{exc}") from exc
            groups2: dict[frozenset, list[dict]] = {}
            for c in cards:
                fids_c = frozenset(
                    (refs[int(i)].owner, refs[int(i)].file_key)
                    for i in c.get("indices", ())
                )
                groups2.setdefault(fids_c, []).append(c)
            if len(groups2) != 1:
                desc = "；".join(
                    "{" + "、".join(f"{o}/{k}" for o, k in sorted(fs)) + "}"
                    for fs in groups2
                )
                raise OutOfRange(
                    f"这些卡来自不同的合并向量，没法合成一条结论"
                    f"（一次结论要求它们共享同一个承诺 C）：{desc}。\n"
                    "  要么把它们分开验，要么先用「聚合选中」统一到一条向量上。"
                )
            fids2 = sorted(next(iter(groups2)))
            crs_n, C, P, loc = store.universe_for(fids2)
            merged = len(fids2) > 1

            cases = []
            for c in cards:
                proof = c["proof"]
                # ★ 以**卡片上的** indices 为准，而且**不预先规范化** ——
                #   预先 as_index_set 掉的话，卡片里的重复下标就被"静默修正"了，
                #   形状检查再也看不见它（实测踩过：重复下标被当成正常证据验过了）。
                #   proof.I 只用来跟它对账（对不上就是 BAD_SHAPE）。
                raw_I = [int(i) for i in c["indices"]]
                # ★ 卡片的 indices 是**全局位置号**，而凭证与 CRS 的指数用**局部块号**
                #   （``proof.I`` 本来就是这个宇宙里的局部号）。不换算的话，
                #   除第一份文件外一律 ``BAD_SHAPE``。
                stray2 = sorted({i for i in raw_I if i not in loc})
                if stray2:
                    raise OutOfRange(
                        f"卡片里的下标 {stray2[:8]} 不属于这条向量"
                        f"（涉及 {len(fids2)} 份文件、共 {len(P)} 个位置）"
                    )
                # 全局 → 局部；**保留重复**（形状检查要靠它才能报 BAD_SHAPE）。
                I_loc = [loc[i] for i in raw_I]
                cases.append(
                    (
                        I_loc,
                        [int(v) for v in c["values"]],
                        Opening(
                            int(proof["S_I"]),
                            int(proof["Lambda_I"]),
                            as_index_set(proof.get("I") or I_loc),
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
                #: 这份结论覆盖到的**全局位置总数**。
                "delta_n": store.n,
                "delta_fp": self.delta_fingerprint(),
                #: 这次是在**哪条向量**上给的结论（跨文件的卡落在合并向量上）。
                "universe_files": [f"{o}/{k}" for (o, k) in fids2],
                "universe_n": len(P),
                "merged_universe": merged,
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

    def _finish_or_pending(self, finish):
        """落库；**失败时把这一步挂成“待补落库”**，然后把异常抛出去。

        ★★ 为什么必须留这一手（安全审计 S3）：

        写路径的次序是“**内存与节点先前进**，落库在后”。如果 ``store`` 那边
        已经成功、而落库自己抛错（磁盘满 / SQLITE_BUSY / 唯一约束冲突），
        那一刻的状态是“向量已经动了、库里少一行” —— 而它
        **既不是 ``WriteError``（那条路有现场），也不是干净失败（无副作用）**。

        不留现场的后果：

        * ``_reject_if_pending`` 拦不住后续写 —— 它们会在**错位的账**上继续写；
        * 重启时 ``_reload`` 从库重建（**缺这一份**），而节点那边有 ⇒
          ``_verify_nodes_at_startup`` 以“节点与协调者不同步”**拒绝启动**，
          而这时**没有任何补推路径**能收敛（它既没有 pending，也没有干净回滚）。

        挂到 ``_pending_finish`` 之后：「补推」会在收尾时把它补上
        （见 :meth:`retry_push`），而 ``persist_pending`` 会让
        ``_reject_if_pending`` 拦住后续写 —— 两条路都通了。

        .. note::

           ``_finish`` 是 ``db.add`` 式的（**非幂等**）。补落库能安全重试的
           前提是“失败时事务没有提交”（``Database.session()`` 在异常时回滚）。
           若失败发生在 ``commit()`` **之后**（极少），重试会撞唯一约束 ——
           那种情况在 :meth:`retry_push` 里会被如实报成 ``persist_error``，
           不会被静默当成成功。
        """
        try:
            return finish()
        except Exception:
            self._pending_finish = finish
            raise

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
            out = store.retry_pending()
            # ★★ 只要还有“欠着的落库”就补上 —— **不能**再要求 had_pending
            #   （安全审计 S3）：落库失败那条路上，节点侧本来就是推成功的
            #   （没有 pending），卡在库里的是那一行；要求 had_pending
            #   会让它**永远补不上**，而重启时正是这种情况会被拒绝启动。
            #
            #   反向的假收敛这里不会发生：那种情况（**没有** _pending_finish
            #   却去调 _finish()）压根进不来这个分支。
            if out.get("ok") and self._pending_finish is not None:
                finish, self._pending_finish = self._pending_finish, None
                with stage("落库（补推收尾）"):
                    try:
                        finish()
                    except Exception as exc:  # noqa: BLE001 - 要如实报出来
                        # 补落库本身又失败了：把现场**放回去**（下次还能补），
                        # 并让这次补推如实标成失败 —— 不能静默当成功。
                        self._pending_finish = finish
                        out["ok"] = False
                        out["persisted"] = False
                        out["persist_error"] = f"{type(exc).__name__}: {exc}"
                        out["pending"] = store.pending_write()
                        out["persist_pending"] = True
                        return out
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

    def replay_arm(self, owner: str, file_key: str, block_idx: int, *, on: bool) -> dict:
        """打开 / 关闭「回滚演示」：让 ``/cipher`` 对这块交回**旧版本**。

        ★ 演示顺序（重要）：**先开开关，再改块**。
          开开关时存下来的是“**当时**那一块的密文”，而开关的含义就是
          “以后对这块都交回这一份”。所以：

          1. 开开关（存下现在这版）
          2. 改块（这一块变成新版）
          3. 解密 → 服务器交回第 1 步存的那份（= 改之前的）→ 验证不通过
          4. 关开关 → 恢复正常

        ★ 存的东西必须**成套**（密文 + IV + 块密钥密文 + 分量）：
          少一样都会变成另一种错误（“密钥解不开”或“声称的分量对不上密文”），
          而不是干净的“这就是改之前那一版”。

        :raises NotFound: 文件或这一块不存在。
        """
        with self._lock, self.db.session() as db:
            store = self._require()
            if (owner, file_key) not in store.files:
                raise NotFound(f"文件 {owner}/{file_key} 不存在")
            got = store.file_indices(owner, file_key)
            if not got or not (0 <= block_idx < len(got)):
                raise NotFound(f"这份文件没有第 {block_idx} 块")

            row = db.execute(
                select(ReplayRow).where(
                    ReplayRow.owner == owner,
                    ReplayRow.file_key == file_key,
                    ReplayRow.block_idx == block_idx,
                )
            ).scalar_one_or_none()

            if not on:
                if row is not None:
                    db.delete(row)
                    db.commit()
                return {"on": False, "block_idx": block_idx}

            # 存“当前”这一版（在还没改块之前调，它就等于“改之前那一版”）。
            pack = store.cipher_of(owner, file_key, [block_idx])
            blk = pack["blocks"][0]
            key_cts = self._blocks_of(owner, file_key)
            krow = key_cts.get(int(block_idx))
            payload = {
                "ciphertext_hex": blk["ciphertext_hex"],
                "iv_hex": blk["iv_hex"],
                "element": str(blk["element"]),
                "key_ct": krow["key_ct"] if krow else json.dumps({}),
                "global_index": int(blk.get("global_index", 0) or got[block_idx]),
            }
            if row is None:
                db.add(ReplayRow(owner=owner, file_key=file_key, block_idx=block_idx, **payload))
            else:
                for k, v in payload.items():
                    setattr(row, k, v)
            db.commit()
            return {"on": True, "block_idx": block_idx}

    def _replay_map(self, owner: str, file_key: str) -> dict[int, ReplayRow]:
        """这份文件上被“回滚开关”盯上的块 —— ``block_idx -> 那一行``。"""
        with self.db.session() as db:
            rows = db.execute(
                select(ReplayRow).where(
                    ReplayRow.owner == owner, ReplayRow.file_key == file_key
                )
            ).scalars()
            return {int(r.block_idx): r for r in rows}

    def replay_status(self, owner: str, file_key: str) -> list[dict]:
        """当前有哪些块正在“交回旧版本”（给界面显示开关状态）。"""
        with self.db.session() as db:
            rows = db.execute(
                select(ReplayRow).where(
                    ReplayRow.owner == owner, ReplayRow.file_key == file_key
                )
            ).scalars()
            return [
                {
                    "block_idx": int(r.block_idx),
                    "global_index": int(r.global_index),
                    "armed_at": r.armed_at.isoformat() if r.armed_at else None,
                }
                for r in rows
            ]

    def cipher_pack(
        self, owner: str, file_key: str, indices=None, *, with_primes: bool = True
    ) -> dict:
        """**客户端自解密**所需的全部材料：密文 + 证据 + 公开参数。

        ★ 与 :meth:`read_plain` 的分工，就是「服务器可不可信」这条分界线：

        * ``read_plain`` —— 服务端**替你把内容解开**，于是"服务器给你的东西"
          在原理上无法被客户端检验（它想给什么就给什么）；
        * ``cipher_pack`` —— 服务端只交出**可被独立校验**的材料：
          密文段（客户端自己 ``SM3`` 得分量）、一份证据（客户端对着
          **自己保存的** δ 验）、块密钥的 SM2 密文（只有所有者的私钥解得开）。

          服务端在这里**不掌握任何秘密**，也无法伪造一个能通过验证的应答
          —— 除非它能同时改掉客户端本地保存的 δ。

        所以这里刻意**不给**：任何明文、任何解开的块密钥。
        ``element``（分量）是服务端算的，客户端**必须自己重算**，
        它只在界面上做对照用。

        :param with_primes: 是否把整段素数表也带上（让客户端可以自己算
            :math:`U_n = g^{e_{[n]}}`）。默认带上 —— 响应会大一截，
            但"能自算"是这条路的价值所在；前端可以关掉它省流量。
        """
        with self._lock:
            store = self._require()
            if (owner, file_key) not in store.files:
                raise NotFound(f"文件 {owner}/{file_key} 不存在")
            try:
                out = store.cipher_of(owner, file_key, indices)
            except KeyError as exc:
                raise NotFound(str(exc)) from exc
            except (ValueError, IndexError) as exc:
                raise OutOfRange(str(exc)) from exc

            # ★ 块密钥密文（``key_ct``）只存在库里，不在 ``VectorStore`` 里 ——
            #   由这里补上。它是 SM2 封给所有者公钥的，别人拿到也解不开。
            key_cts = self._blocks_of(owner, file_key)
            for b in out["blocks"]:
                row = key_cts.get(int(b["block_idx"]))
                b["key_ct"] = json.loads(row["key_ct"]) if row else None

            # ★★ 回滚演示：被“回滚开关”盯上的那几块，改交回**存下来的旧版本**。
            #
            #    走的是**同一条真链路** —— 客户端拿到的确实是另一份密文，
            #    它自己算出的分量对不上当前基准，于是验证不通过。
            #    不是“前端假装”：演示要经得住追问。
            #
            #    放在补 `key_ct` **之后**：旧那块的块密钥密文也得用旧那一份 ——
            #    否则客户端拿旧密文配新密钥一解，解出来是垃圾，（也能发现不对，
            #    但那变成了“密钥不匹配”而不是“服务器交了旧版本”，性质不同）。
            replay = self._replay_map(owner, file_key)
            if replay:
                # ★★ 必须核对**位置段**：`replay_blocks` 的键是
                #    `(owner, file_key, block_idx)`，而 `file_key` 是**可复用**的 ——
                #    删掉一份文件、再用同一个标识上传另一份内容时，`files` 里
                #    是新账目，这条旧记录却还在。位置段（`global_index`）**永不回收**，
                #    是文件实例级的唯一标识：对不上就说明这条记录属于**上一次**
                #    那一份文件，当它不存在。
                #
                #    不核对会怎样（实测踩过）：新文件的那几块被塞回旧密文，
                #    客户端算出的分量对不上当前基准，报“本地验证未通过” ——
                #    而界面与操作序列都看不出任何异常，像玄学。
                cur_pos = store.file_indices(owner, file_key)
                for b in out["blocks"]:
                    bi = int(b["block_idx"])
                    r = replay.get(bi)
                    if r is None:
                        continue
                    if not (0 <= bi < len(cur_pos)) or int(r.global_index) != int(cur_pos[bi]):
                        # 属于上一份文件的那一块 —— 不是“这一份的这一块”。
                        continue
                    b["ciphertext_hex"] = r.ciphertext_hex
                    b["iv_hex"] = r.iv_hex
                    b["element"] = r.element
                    b["key_ct"] = json.loads(r.key_ct)
                    #: 让前端能如实标出“这一块是服务器交回的旧版”。
                    b["replayed"] = True

            delta = store.delta_of(owner, file_key)
            sess = store.session
            ell = int(sess.l)
            primegen = sess.primegen_for(delta)
            used_positions = [int(i) for i in out["proof"]["I"]]

            out["crs"] = {
                "N": str(sess.crs.N),
                "g": str(sess.crs.g),
                "l": ell,
                "prime_bits": ell + 1,
                "n_max": int(sess.n_max),
            }
            out["delta"] = {
                "U": str(delta.U),
                "C": str(delta.C),
                "n": int(delta.n),
                "offset": int(delta.offset),
                "fp": self.delta_fingerprint((owner, file_key)),
            }
            # ★ 证据只用到 ``I`` 里那几个下标的素数，先给这几个 ——
            #   客户端必须对它们**做素性检查**（塞合数会静默算错，见 primegen 的警告）。
            out["primes"] = {
                "bits": ell + 1,
                "start": str(1 << ell),
                "indices": used_positions,
                "values": [str(primegen.get(i)) for i in used_positions],
            }
            if with_primes:
                out["prime_table"] = {
                    "count": int(delta.n),
                    "values": [str(primegen.get(i)) for i in range(int(delta.n))],
                }
            return out

    # -------------------------------------------------------------------
    # 用户删除（墓碑改名）
    # -------------------------------------------------------------------

    def delete_user(self, user_id: int, *, actor: str) -> dict:
        """删号：把该用户名下的一切改挂到**墓碑名**，然后删掉用户行。

        ★ 三件事必须在**同一把写锁 + 同一个事务**里（审计 S1 / S2）：

        1. **内存**：``registry`` / ``files`` / ``deltas``（以及子类的块密钥表）一起改名；
        2. **库**：``blocks`` / ``files`` / **``file_deltas``** 三张表一起改名；
        3. 删 ``users`` 那一行。

        为什么不能少改任何一处 —— 后果不是"少改一行"，而是：

        * 漏 ``deltas`` / ``file_deltas`` ⇒ 运行期 ``delta_of(墓碑名)`` 直接
          ``KeyError``，而 ``/api/files`` 是**逐行序列化**的 ⇒
          **一份墓碑文件就能让所有用户的文件列表 500**；
        * 重启后 ``_reload`` 从 ``blocks``（墓碑名）与 ``file_deltas``（旧名）
          重建出**两套身份** —— 库自己就不一致，所以**不自愈**。

        为什么必须持锁：它改的是 ``registry`` / ``files`` / ``deltas`` 三个
        **共享结构**，与并发的上传 / 查询是竞争关系。全项目其他写路径都在
        ``_lock`` 保护下，只有它以前没有（审计 S2）——
        而它的操作又跨内存与库，一旦看到半更新状态，故障要等到重启才"坐实"。

        :param actor: 发起删除的管理员用户名（不能删自己）。
        :raises NotFound: 用户不存在。
        :raises Conflict: 想删自己，或改名会撞键。
        :returns: ``{"deleted", "files_left", "tomb_name"}``
        """
        with self._lock, self.db.session() as db:
            user = db.get(UserRow, user_id)
            if user is None:
                raise NotFound("用户不存在")
            if user.username == actor:
                raise Conflict("不能删除自己")

            username = user.username
            uid = user.id
            # ★ 墓碑名里带 ``#id`` 是为了唯一：``ghost`` 被删两次会有两个墓碑名，
            #   否则 (owner, file_key) 会撞唯一约束。
            #   （SQLite 不强制 VARCHAR 长度，所以名字超过 64 字符也不会被截断。）
            tomb = f"{username}（已删号#{uid}）"
            n_files = int(
                db.execute(
                    select(func.count())
                    .select_from(FileRow)
                    .where(FileRow.owner == username)
                ).scalar_one()
            )

            store = self._require()
            # 先改内存（它会先做冲突检查，抛了就不会动库），再改库，最后删用户。
            # 顺序反过来会留下"库已改、内存没改"的分叉 —— 而分叉只有重启后才看得出来。
            try:
                store.rename_owner(username, tomb)
            except ValueError as exc:
                raise Conflict(str(exc)) from exc

            try:
                for stmt in (
                    update(BlockRow).where(BlockRow.owner == username),
                    update(FileRow).where(FileRow.owner == username),
                    # ★★ S1 的修复点：``file_deltas`` 以前**没有被改**。
                    update(FileDeltaRow).where(FileDeltaRow.owner == username),
                    # ★★ 回滚演示记录也要跟着走：不跟的话它会留在**活人**名下，
                    #    而这个用户名一旦被重新注册，同一个
                    #    ``(owner, file_key)`` 就会被复用 —— 又是
                    #    “新文件被塞进旧密文”那条路（见 ``delete_file``）。
                    update(ReplayRow).where(ReplayRow.owner == username),
                ):
                    db.execute(stmt.values(owner=tomb))
                db.delete(user)
                db.commit()
            except Exception:
                db.rollback()
                # 库那半边失败就把内存**改回去**：分叉的后果是
                # "重启前后文件列表不一样"，那种故障最难查。
                # 这里宁可整体失败，也不留下一半。
                store.rename_owner(tomb, username)
                raise

            return {
                "deleted": username,
                "files_left": n_files,
                "tomb_name": tomb,
            }

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
                #: ★ 摘要现在是**逐文件**的，所以这里给的是“全库账目”的汇总：
                #:   总量 + 每份文件各自的 offset / n / 指纹。
                #:
                #: ``n`` 是**旧字段，保留兼容**：在新架构里它等于
                #: ``registry.total_blocks()``（= **各文件块数之和**，
                #: **不含**历史空洞 —— 与 ``core/registry.py`` 的自述一致）。
                #: 它**不是**“合法下标的上界”：删过文件之后可能小于某些有效下标。
                #: 旧代码里那句“当前向量长度”在单文件场景下数值不变，所以无需迁移；
                #: 要按文件看就看 ``files``。
                "n": store.n,
                "n_total": store.n,
                #: ★ “位置预算已用”= ``registry.next_offset``：**单调不减**，
                #:   把已废弃的空洞也算在内 —— 它才是“分配过的位置总量”。
                #:   前端判“下标越界 / 是不是排在向量末尾”应当用它，而不是 ``n``。
                "position_budget_used": int(store.registry.next_offset),
                "files": [
                    {
                        "owner": own,
                        "file_key": fk,
                        "offset": store.delta_of(own, fk).offset,
                        "n": store.delta_of(own, fk).n,
                        "delta_fp": self.delta_fingerprint((own, fk)),
                        "U": str(store.delta_of(own, fk).U),
                        "C": str(store.delta_of(own, fk).C),
                    }
                    for own, fk in sorted(store.deltas)
                ],
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
            # ★ 这里的 ``nodes`` 是「**在线台数**」（界面上的「N NODES / 个节点在线」），
            #   不是「参与存储的台数」。节点报的 ``fresh`` 意思是“这台机器上
            #   什么都没有”（见 `core/node_state.py::_describe_one`）—— 空库时
            #   **每一台**都是 fresh，照它数会得到 0 台在线，于是总览页会出现
            #   「0 NODES」配着「系统运行正常」这种自相矛盾。所以按“答上话了”数。
            "nodes": len([r for r in report if not r.get("unreachable")]),
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
        ref = store.describe(global_index)
        d = store.delta_of(ref.owner, ref.file_key)
        return {
            "global_index": global_index,
            "owner": ref.owner,
            "file_key": ref.file_key,
            "block_idx": ref.block_idx,
            #: ★ 新架构：把位置翻成「文件 / 第几块」所需的全部信息
            "offset": int(d.offset),
            "n": int(d.n),
            "delta_fp": self.delta_fingerprint((ref.owner, ref.file_key)),
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
            d = store.delta_of(owner, file_key)
            out.append(
                {
                    "owner": owner,
                    "file_key": file_key,
                    "total_bytes": rec.total_bytes,
                    "block_count": rec.block_count,
                    "segment_bytes": rec.segment_bytes,
                    "content_digest": rec.content_digest,
                    "indices": list(rec.indices),
                    #: ★ 新方案（一文件一向量）：段起点、块数、位置段、指纹。
                    #:   界面按「文件 / 第几块」展示，这几个就是它背后的依据；
                    #:   位置段还解释了“为什么块号与位置号对不上”。
                    "offset": int(d.offset),
                    "n": int(d.n),
                    "segments": [[int(a), int(b)] for a, b in d.segments],
                    "delta_fp": self.delta_fingerprint((owner, file_key)),
                    # 只是**方便显示**的边界值。追加之后一个文件的下标可能带缺口
                    # （两次追加之间别的文件也占了位置），所以 first..last 之间
                    # 可能夹着别人的块 —— 要真集合只能用上面的 indices。
                    "first_index": rec.indices[0],
                    "last_index": rec.indices[-1],
                }
            )
        out.sort(key=lambda x: x["offset"])
        return out

    def _leaving_nodes(self) -> tuple[str, ...]:
        """**配置里已经摘掉、但进程还在跑**的那几台（缩容保存后、重启前的窗口）。

        ``nodes/deploy.json`` 说的是“重启后按几台跑”，``settings.node_ids``
        说的是“现在跑着几台”。前者比后者小时，多出来的那几台就是正在收拾东西的
        机器：块已经搬到留下的机器上了，但它们**自己手里还留着一份**，
        而且那份删不掉（VDS 只能删向量末尾）—— 详见
        :meth:`core.store.VectorStore.check` 的 ``leaving``。

        口径必须与 ``routers/admin._deploy_keep`` 保持一致：**都取前 N 台**。
        配置读不到时（文件被删/写坏）当作“没有要退出的”，不去凭空怀疑。
        """
        ids = tuple(self.settings.node_ids)
        saved = read_deploy_node_count(default=len(ids))
        return ids[saved:] if saved < len(ids) else ()

    def check(self) -> list[str]:
        """全面自检。抛出 = 真的不对；返回的 **提示** 不是错误（见 :meth:`_leaving_nodes`）。"""
        return self._require().check(leaving=self._leaving_nodes())
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
