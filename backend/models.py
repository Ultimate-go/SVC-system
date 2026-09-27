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
    "GlobalRow",
    "UserRow",
    "FileRow",
    "BlockRow",
    "NodeBlobRow",
    "NodeStateRow",
    "AuditRow",
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


class GlobalRow(Base):
    """全局摘要 :math:`\\delta = ((U, C), n)`。键值对表，三行。

    大整数一律用**十进制字符串** —— 与 :func:`core.session.crs_to_dict`
    的理由一样：跨语言传输时 Python 的任意精度 ``int`` 与 JS 的 ``Number``
    不是一回事，统一走字符串最省心。
    """

    __tablename__ = "globals"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


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
    """一台存储服务器的本地视图 :math:`(\\delta, st, I, F_I)`。

    节点**不存完整文件** —— 只有它那一段的下标、值与证据。
    """

    __tablename__ = "node_state"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    delta_U: Mapped[str] = mapped_column(Text, nullable=False)
    delta_C: Mapped[str] = mapped_column(Text, nullable=False)
    delta_n: Mapped[int] = mapped_column(Integer, nullable=False)
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
