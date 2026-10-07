"""SQLAlchemy 表定义。

设计原则
--------
**内存里的 :class:`~core.store.VectorStore` 是权威，数据库是写穿日志。**
重启时从库里重建向量，重建完必须能通过 ``store.check()``
（有测试钉住这一点）。这样做的好处是：密码学状态只有一份真源，
数据库不参与任何代数运算，出问题时边界清楚。

**安全红线**（规划 §6.4）：``crs`` 表只存 ``N``、``g``、``l``、``n_max`` 四个
**公开量**。``p``、``q``、``φ(N)`` 绝不落库 —— 知道 ``φ(N)`` 就能求 ``e`` 次根，
从而伪造任意证据。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

__all__ = [
    "CrsRow",
    "MetaRow",
    "FileDeltaRow",
    "UserRow",
    "FileRow",
    "BlockRow",
    "NodeBlobRow",
    "NodeStateRow",
    "NodeRegistryRow",
    "AuditRow",
    "RevokedTokenRow",
    "ReplayRow",
    "PendingWriteRow",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 公开参数与全局摘要 —— 全系统各一行
# ---------------------------------------------------------------------------

class CrsRow(Base):
    """公开参数 ``crs = (N, g, l, n_max)``。**全库只有一行**（``id=1``）。

    素数映射不必存：它只由 ``(n_max, l+1)`` 决定，可以用
    :func:`core.session.get_primegen` 逐位重建。
    """

    __tablename__ = "crs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    N: Mapped[str] = mapped_column(Text, nullable=False)
    g: Mapped[str] = mapped_column(Text, nullable=False)
    l: Mapped[int] = mapped_column(Integer, nullable=False)
    n_max: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_crs_dict(self) -> dict:
        return {"N": self.N, "g": self.g, "l": self.l, "n_max": self.n_max}


class MetaRow(Base):
    """零散元数据（键值对）。现在只放**分片偏移**这类全系统唯一的标量。

    ★ 摘要**不**在这里 —— 新方案里摘要是**逐文件**的，见 :class:`FileDeltaRow`。
    这张表保留下来是因为分片偏移（``node_offset``）确实只有一个：
    它决定“新块轮转从哪台开始”，与具体是哪份文件无关。
    """

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class PendingWriteRow(Base):
    """**待补推现场**——写失败之后留在盘上的那一份（全库只有一行，``id=1``）。

    为什么要有它（实测踩过）：现场原来只在**内存**里（
    :attr:`core.store.VectorStore._pending` 与 ``manager._finish_cb``）。
    一旦进程没了，现场就永久丢失 —— 而**节点上已经跟进的那几台不会回退**。
    于是重启时启动守卫看到"节点跑在前面"，报一句
    「存储节点与协调者不同步，拒绝启动」就把整个系统**锁死**，
    而唯一出路是人工去每台节点上清那半段。

    落库之后重启就能接着办：先恢复现场（守卫也知道"跑在前面"是预期的），
    再把那次更新补推给没跟上的几台。

    ★ 里面确实包含**密文**（``payloads`` 就是没送出去的那几份请求体）。
    这不是多出来的暴露面：同一份密文在存储节点上本来就有，而且
    它没送出去时这是**唯一**的一份 —— 不存下来，那次写就只能丢。
    块密钥依旧是密文（``blocks.key_ct`` 那条性质不变）。
    """

    __tablename__ = "pending_write"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FileDeltaRow(Base):
    """**一份文件一条**向量摘要 :math:`\\delta = ((U, C), n)`。

    ★ 新方案（一文件一向量）里，摘要不再是全系统一个，而是**逐文件**的：
    每份文件占一段全局位置（段起点就是 ``offset``，**永不回收**），
    自己的 :math:`U, C, n` 只随自己变。所以这里的主键是
    ``(owner, file_key)``，而不是以前那种“整个库只有一行”。

    :param chunks: 位置段列表 ``[[起点, 长度], ...]``（JSON）。
        一份文件的位置未必连续 —— 追加时原段末尾被后来的文件占住，
        就只能**另起一段**。节点侧的所有“局部块号 → 素数”换算都靠它，
        **落库时必须一起存上**，否则重启后位置视图会退化成“从 offset 起连续 n 个”。
        空列表 = 单段，等价于 ``[[offset, n]]``。

    大整数一律用**十进制字符串** —— 与 :func:`core.session.crs_to_dict`
    的理由一样：跨语言传输时 Python 的任意精度 ``int`` 与 JS 的 ``Number``
    不是一回事，统一走字符串最省心。
    """

    __tablename__ = "file_deltas"

    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: 本文件在全局素数表里的**段起点**（唯一且稳定，段永不回收）。
    offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delta_U: Mapped[str] = mapped_column(Text, nullable=False)
    delta_C: Mapped[str] = mapped_column(Text, nullable=False)
    delta_n: Mapped[int] = mapped_column(Integer, nullable=False)
    chunks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: ★ 本文件的**身份** ``"<owner>\x1f<file_key>"`` —— 新方案里
    #: 「第 ``i`` 块配哪个素数」完全由它决定（``H(owner‖file_key‖i)``）。
    #: 空串 = **老坐标**（查全局素数表第 ``offset+i`` 个）。
    #: 落库时必须存 —— 丢了它，重启后这份文件会退回老坐标，
    #: 而它已经用身份坐标算过 ``U/C``，验证必然失败。
    identity: Mapped[str] = mapped_column(Text, nullable=False, default="")


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------

class UserRow(Base):
    """用户。``role`` 只有 ``admin`` / ``user`` 两种。

    这里有两列是**解密权限的真正落点**：

    * ``pub_key`` —— 用户的 SM2 公钥（65 字节未压缩，十六进制）。
      别人上传不了他的文件、也封不了块密钥给他；这本来就是公开的。
    * ``sk_wrapped`` —— **用户的 SM2 私钥，但用他的登录口令包了一层**
      （:func:`core.keywrap.wrap_private_key`，PBKDF2-HMAC-SM3 + SM4）。

    .. important::

       **私钥绝不明文落库。** 这一列存的是口令派生的 KEK 加密后的密文，
       口令本身不在这里（``pwd_hash`` 是另一回事，只用于登录校验，
       从它推不回口令）。所以——**数据库被拿走、连同这个文件的备份一起
       被拿走，没有口令依然解不开任何文件**。

       这正是「只有自己才能解密」与「靠接口判断一下是不是你的文件」的
       分界线：后者只要谁拿到数据库就绕过了。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    pwd_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: SM2 公钥（65 字节未压缩的十六进制）。上传时用它封装块密钥。
    pub_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: SM2 私钥的**口令封装**（JSON 文本）。明文私钥从不落库。
    sk_wrapped: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 文件、块、节点状态
# ---------------------------------------------------------------------------

class FileRow(Base):
    """一次上传的账目。``file_key`` 是同一所有者内的文件标识。"""

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("owner", "file_key", name="uq_owner_file"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_key: Mapped[str] = mapped_column(String(128), nullable=False)
    segment_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 这份文件现在第几版。上传时为 1，每改一块 +1。
    #:
    #: **它就是防重放用的那个序号。** 客户端只持摘要、看不到内容，
    #: 所以察觉不到「一份旧的修改被重新投递」（见
    #: ``vds.client_node.ClientNode.apply_update`` 的警告）；
    #: 节点能查出来，但需要有人把「现在该是第几版」记住 —— 就是这一列。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    blocks: Mapped[list["BlockRow"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class BlockRow(Base):
    """一个全局位置在**协调者**这边的账目。

    这张表同时承担两个角色：

    * **块索引登记表** —— ``(owner, file_key, block_idx)`` → ``global_index``
    * **分片决策** —— ``holder`` 记着这块归哪台节点

    .. note::

       **密文不在这里。** 密文是存储节点的东西，归 :class:`NodeBlobRow`
       （本地模式）或各节点自己的库（跨进程模式）。

    .. important::

       **这里也绝不明文存块密钥。** ``key_ct`` 是块密钥被**文件所有者的
       SM2 公钥**封装后的密文（见 :mod:`core.keywrap`）—— 只有拿得到所有者
       私钥的人才能把它解回那把钥匙。
       这一列是密钥在整个系统里的**唯一**存放处。

       注意它**不含任何策略**：以前这里还跟着一份"策略串"，现在是
       "一把公钥封的密文"。谁能解不再是一句话，而是**算不算得出来**。
    """

    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("owner", "file_key", "block_idx", name="uq_block_pos"),
    )

    global_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    file_key: Mapped[str] = mapped_column(String(128), nullable=False)
    block_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    element: Mapped[str] = mapped_column(Text, nullable=False)
    #: **块密钥密文**（JSON 文本）：用文件所有者的 SM2 公钥封装后的样子。
    key_ct: Mapped[str] = mapped_column(Text, nullable=False)
    iv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    plain_len: Mapped[int] = mapped_column(Integer, nullable=False)
    #: **主副本**（兼容旧字段：界面与“去哪里拿”的默认答案）。
    holder: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 这块的**全部**持有者，JSON 数组、主副本在前（含主副本）。
    #: 旧数据默认 ``"[]"``，读的时候退化成“只有 ``holder`` 一份” ——
    #: 所以开副本之前传的文件照样能起来、能读。
    replicas: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    file: Mapped[FileRow] = relationship(back_populates="blocks")


class NodeBlobRow(Base):
    """**本地模式**下节点存着的密文段。

    单进程部署时，节点就是同进程的对象，它们的存储得有个地方落盘 ——
    就是这张表。**跨进程部署时这张表应该空着**，因为密文存在各节点
    自己的库里（见 ``node_service/``）。

    把它单列一张表而不是塞进 ``blocks``，正是为了让这个区别在数据模型上
    就看得见：“这条记录是节点的，不是协调者的”。
    """

    __tablename__ = "node_blobs"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    global_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class NodeStateRow(Base):
    """一台存储服务器**在某一份文件上**的本地视图 :math:`(\\delta, st, I, F_I)`。

    节点**不存完整文件** —— 只有它那一段的下标、值与证据。

    ★ 新方案里一台节点同时参与**好几份**文件，各自一段视图，所以要按
    ``(node_id, offset)`` 区分（旧表只有 ``node_id`` 一个主键，那是
    “全系统一条向量”时代的形状）。
    """

    __tablename__ = "node_state"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 这份文件的段起点（与 ``FileDeltaRow.offset`` 对齐）。
    offset: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    delta_U: Mapped[str] = mapped_column(Text, nullable=False)
    delta_C: Mapped[str] = mapped_column(Text, nullable=False)
    delta_n: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 位置段 ``[[起点, 长度], ...]``（JSON）。空 = 单段。
    delta_chunks: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: ★ 同上：节点侧也必须知道这份文件是哪个坐标，否则它算出的
    #: ``e_i`` 与协调者不同，下一次更新的 Υ∆ 校验会失败
    #: （报错长成「ShamirTrick 同源自检失败」，指不出真正原因）。
    delta_identity: Mapped[str] = mapped_column(Text, nullable=False, default="")
    S_I: Mapped[str] = mapped_column(Text, nullable=False)
    Lambda_I: Mapped[str] = mapped_column(Text, nullable=False)
    #: 下标集合与对应的值，JSON 数组
    I_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    FI_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    @property
    def I(self) -> list[int]:
        return json.loads(self.I_json)

    @property
    def FI(self) -> list[int]:
        return json.loads(self.FI_json)


class NodeRegistryRow(Base):
    """**节点登记表** —— 这个集群一共有过哪些存储节点、各是什么时候加入的。

    为什么要它（以前节点名单只活在环境变量里，库里查不到）：
    :meth:`~backend.manager.StoreManager._verify_nodes_at_startup` 要求
    "每台节点停在的 ``n`` 与协调者一致"。但**刚加入的新节点天然是空的**
    （``n = 0``）——它和"数据目录被删掉的老节点"在节点那一侧看起来一模一样。
    没有这张表就只能二选一：要么把新人一起拒掉（加机器必炸），要么把
    "丢了数据的老节点"也放过去（**静默丢数据**）。

    区分办法是 ``received_n``：**协调者记录"我上次成功推送到第几块"**。
      * 新节点：``received_n = 0``，而它自己也是 ``n = 0`` → 一致 ⇒ 放行；
      * 老节点丢数据：``received_n = 10``（推过 10 块），而它 ``n = 0`` → 不一致 ⇒ 拒绝。

    ``received_n`` 在**每次 bootstrap 通过闸门之后**刷新为当时的
    **全局位置总数**（``store.n`` = ``registry.total_blocks()``）。新方案里段
    永不回收，所以这个数**单调不减**，“推过 10 块却停在 n=0”仍然意味着丢数据。
    """

    __tablename__ = "node_registry"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: 协调者上次成功推送到第几块。0 = 从没收到过（新加入还没分到块）。
    received_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 已经从这个集群里摘掉了（缩容留下的历史记录，仅用于展示）。
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ---------------------------------------------------------------------------# 审计
# ---------------------------------------------------------------------------

class AuditRow(Base):
    """审计流水。发放 / 拒绝 / 验证 / 解密全记 —— 答辩时能当"操作回放"用。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 管理员对这条流水的**人工批注**（可空）。
    #:
    #: ★ 单独一列，**不改** ``detail``：``detail`` 是系统当时记下的事实
    #:   （"不是所有者"），这里是人后来的解释（"演示用的，不是故障"）。
    #:   两者混在一列里，事后就分不清哪句是机器说的、哪句是人补的 ——
    #:   审计记录最忌讳这个。
    remark: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 最后写备注的人与时间（空 = 从没人批注过）。留痕是为了批注本身也可追溯。
    remark_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    remark_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RevokedTokenRow(Base):
    """已作废的令牌 / 已按人撤销的账号（安全审计 I1、I2）。

    ★ 为什么**必须落库**：撤销表放内存时，“登出 / 改口令 / 停用”的效力与
      **进程寿命**一样长。而按生产惯例设了 ``VDS_SECRET_KEY`` 之后，
      签名密钥跨重启**不变**、令牌本身仍然有效 —— 于是一旦重启，
      已经登出（或已经泄露）的令牌就**复活**了，最长能活到
      ``token_ttl_minutes``。

    ★ 两种记录同住一张表（由 ``jti`` 前缀区分）：

    * **按张**撤销（登出）：``jti`` 就是令牌的 jti，只看 ``exp_ts``；
    * **按人**撤销（改口令 / 停用 / 改角色）：``jti = "*user*<用户名>"``，
      这时 ``before_ts`` 才有意义 —— “**此刻之前**签发的令牌一律作废”。
      服务端根本不知道某个人签过多少张令牌（JWT 本来就是无状态的），
      所以只能退回时间戳黑名单。
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 令牌属于谁 / 被撤销的那个账号。
    sub: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 这条记录**什么时候可以删**（Unix 秒）—— 清理只看它。
    exp_ts: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: 按人撤销时的**签发时刻阈值**（Unix 秒）；按张撤销时为 0（不看它）。
    before_ts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReplayRow(Base):
    """**回滚演示**：让服务器对某一块交回“旧版本”（演示“服务器不可信”）。

    ★ 它是什么：拥有者在**改块之前**把“回滚开关”打开，这一块**当时**的密文
      就存在这行里；此后 ``/cipher`` 对这一块交回的就是**这一份**，而不是
      当前那份。客户端拿到的是**真的旧密文** —— 走的是同一条真链路，
      于是它自己算出的分量对不上**当前**基准，验证不通过。

    ★ 为什么不“前端假装”：演示要经得住追问。“服务器真的交回了旧数据”
      与“前端自己造了个假的”是两回事 —— 前者才叫“服务器不可信”。

    ★ 关掉开关就把这行删掉，行为立刻恢复正常；开关本身会进审计流水。
    """

    __tablename__ = "replay_blocks"
    __table_args__ = (
        UniqueConstraint("owner", "file_key", "block_idx", name="uq_replay_pos"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_key: Mapped[str] = mapped_column(String(128), nullable=False)
    block_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 存下来那一版的密文（十六进制）。
    ciphertext_hex: Mapped[str] = mapped_column(Text, nullable=False)
    iv_hex: Mapped[str] = mapped_column(Text, nullable=False)
    #: 块密钥密文（JSON 文本）—— 与 ``blocks.key_ct`` 同形，否则客户端解不开。
    key_ct: Mapped[str] = mapped_column(Text, nullable=False)
    #: 服务端声称的分量。**要一并存**：不存的话“声称的分量”与旧密文不符，
    #: 会额外暴露“这一块被动过脚”，演示反而失真。
    element: Mapped[str] = mapped_column(Text, nullable=False)
    #: 存下来那一刻的全局位置（只供审计/显示）。
    global_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    armed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
