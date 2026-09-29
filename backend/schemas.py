"""请求体模型。

只对**入参**做 pydantic 校验；响应统一返回 dict —— 里面大量是十进制字符串
表示的群元素，为它们再定义一遍模型得不偿失，而且前端本来就只看指纹。
"""

from __future__ import annotations

import base64
import binascii

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "LoginIn",
    "UserCreateIn",
    "UserPatchIn",
    "QueryIn",
    "FileQueryIn",
    "DecryptIn",
    "DisaggIn",
    "FilePatchIn",
    "DeployIn",
    "PortsIn",
    "RestartIn",
]


class DeployIn(BaseModel):
    """改**服务器台数**（``PUT /api/admin/deploy``）。

    ★ 这里的两端刻意**放得很宽**（0..4096），而不是照抄
    :data:`~backend.config.MAX_NODE_COUNT`：真正的边界只该在
    ``backend.config`` 里写一份，让越界走到那条会明确说
    「必须在 1..32 之间」的分支上。
    pydantic 这里的作用只有一条 —— 拦住"根本不是个整数"的输入，
    免得它一路走到业务代码才炸成 500。
    """

    #: 想改成几台。**这是"下一次重启"要用的台数**，不是当前的。
    node_count: int = Field(ge=0, le=4096)
    #: 台数变小（或搬不动）时的**二次确认**。
    #:
    #: * 有块搬不动（``lost`` 非空）却没确认 → 409，因为那是唯一一条真会丢数据的路；
    #: * 只是要搬（``orphan`` 非空）却没确认 → 409，因为搬块会**动到布局**，
    #:   使用者该先看一眼"要搬多少块"再点头。
    confirm_shrink: bool = False


class RestartIn(BaseModel):
    """一键重启（``POST /api/admin/deploy/restart``）—— **请求体可以整个不给**。

    为什么不复用 :class:`DeployIn`：那个的 ``node_count`` 是必填的，而"重启"
    这个动作绝大多数时候**不需要**再指定台数 —— 台数已经在保存那一步写进
    部署配置了，重启只需要照着它起一遍。硬要求传一个台数，会让人以为
    "重启的时候还得再说一次起几台"，那是两件事。

    :param node_count: 给了就**顺带把台数也改了**（等价于先保存再重启）。
        界面上那个「立刻重启」按钮走的就是"不带台数"这一路。
    :param confirm_shrink: 与 :class:`DeployIn` 同义（缩容时如果发现还没搬块，
        这里会补搬一次；补搬之前同样要这道确认）。
    """

    node_count: int | None = Field(default=None, ge=0, le=4096)
    confirm_shrink: bool = False


class PortsIn(BaseModel):
    """改**端口**（``PUT /api/admin/ports``；``POST /api/admin/ports/plan`` 同构）。

    ★ 两端故意**放得很宽**（``0..70000``）：真正的边界只该在 ``backend.config``
    里写一份，让越界走到那条会明确说「端口必须在 1024..65535 之间」的分支上（400）。
    pydantic 在这里只负责一件事 —— 拦住"根本不是个整数"的输入，
    免得它一路走到业务代码才炸成 500（与 :class:`DeployIn` 同一个口径）。

    :param backend: 后端端口（重启后生效）
    :param frontend: 前端端口（重启后生效）
    :param nodes: 每台存储节点的端口，键是节点 id（``node-1``）。
        给 ``None`` = 这一轮不动节点端口（只改前后端）
    """

    backend: int = Field(ge=0, le=70000)
    frontend: int = Field(ge=0, le=70000)
    nodes: dict[str, int] | None = None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default="user", pattern=r"^(admin|user)$")
    display_name: str = Field(default="", max_length=64)


class UserPatchIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, pattern=r"^(admin|user)$")
    disabled: bool | None = None
    #: 换口令。**只能改自己的** —— 别人的私钥是用他自己的口令包的，
    #: 管理员改他的口令就把他手里的私钥永久弄丢了（见 ``admin.patch_user``）。
    password: str | None = Field(default=None, max_length=256)


#: 请求里「下标 / 值」列表的长度上限。
#:
#: ★ 它**必须 ≥ 系统的块数上限**，否则会出现「**传得上去、验不了**」：
#:   ``n_max`` 默认 1024（1 KB 块 ⇒ 1 MB 文件正好 1024 块），
#:   而这里原来写死 512 —— 一个 1 MB 文件的**完整查询**会被 pydantic 以
#:   422 拒掉（而且是一坨英文校验转储，界面上很难看）。
#:   这是做 Day 2 那条「传 1 MB」验收时发现的（见 ``tests/test_bigfile.py``）。
#:
#: **调大 ``Settings.n_max`` 时，这个数也要跟着调**。
#: （它写在 pydantic 里，静态声明、没法从配置读，但改一行就生效。）
MAX_INDICES = 1024


class QueryIn(BaseModel):
    """按全局下标查询（**验证不受限**，谁都能查）。"""

    indices: list[int] = Field(min_length=1, max_length=MAX_INDICES)
    #: 「允许部分结果」：默认关。关的时候有块收不齐就报错（并说清缺哪些），
    #: 开着的时候只把拿得到的算进结论，缺的放在响应的 ``missing`` 里。
    #:
    #: 为什么默认关：一份**部分证据**如果被当成“整个文件验过了”，
    #: 那是比报错更坏的结果。所以这个开关得有人**主动**拧。
    allow_partial: bool = False

    @field_validator("indices")
    @classmethod
    def _non_negative(cls, v: list[int]) -> list[int]:
        if any(i < 0 for i in v):
            raise ValueError("下标不能为负")
        return sorted(set(v))


class FileQueryIn(BaseModel):
    """按文件查询：``(owner, file_key)`` 列表 + 可选的块序号。"""

    targets: list[tuple[str, str]] = Field(min_length=1)
    block_indices: list[int] | None = None


class ProofIn(BaseModel):
    r"""一份打开证据 :math:`\pi_I = (S_I, \Lambda_I)`（大整数写成十进制字符串）。"""

    S_I: str = Field(min_length=1)
    Lambda_I: str = Field(min_length=1)
    I: list[int] | None = None


class EvidenceCardIn(BaseModel):
    """证据池里的一张卡片 —— 与 ``/api/query`` 响应**同构**。

    字段名刻意与那个响应一致（``indices`` / ``values`` / ``proof``），
    这样前端把池子里的卡片**原样传回来**即可，不用另造一份数据结构。
    """

    indices: list[int] = Field(min_length=1, max_length=MAX_INDICES)
    values: list[str] = Field(min_length=1, max_length=MAX_INDICES)
    proof: ProofIn


class BatchVerifyIn(BaseModel):
    """**批量验证**：一次验池子里的多份证据（可跨文件、跨用户）。

    :param items: 要一起验的卡片
    :param locate: 失败时是否退回逐份验以**定位**坏的那几份。
        随机系数把 m 条方程合成一条之后，判失败只知道"里面有坏的"、
        说不出是哪一份 —— 要定位就得逐份再验一遍。
    :param compare: 是否**同时**跑一遍逐份验，并把两边的耗时都报回来。
        打开它才是诚实的对比：界面上会把"批量 vs 逐份"两个数一起显示。
        实测批量**更慢**（规划 §13.6），所以这个开关的意义就是**不让人误以为更快**。
    """

    #: 卡片**张数**上限。它与上面的下标上限是两回事：
    #: 一张卡最多能覆盖 ``MAX_INDICES`` 块，而池子里同时验 128 张已经很够了。
    items: list[EvidenceCardIn] = Field(min_length=1, max_length=128)
    locate: bool = True
    compare: bool = True


class DecryptIn(BaseModel):
    """解密（**受限**）。``indices`` 为 ``None`` 时取整个文件。"""

    indices: list[int] | None = None


class DisaggIn(BaseModel):
    """**分解**一份已有证据：从 :math:`\\pi_I` 里拆出 :math:`\\pi_K`（要求 K ⊆ I）。

    前四个字段就是 ``/api/query`` 响应里的 ``indices`` / ``values`` / ``proof``
    —— 前端把池子里那张卡片**原样传回来**即可，不用另造一份数据结构。

    字段名用 ``I`` / ``K`` 而不是 snake_case，是刻意跟论文符号对齐：
    界面上也要显示它们（“从 {0,1,2} 拆出 {0,2}”），叫一样的名字少一层翻译。
    """

    I: list[int] = Field(min_length=1, max_length=MAX_INDICES)
    values: list[str] = Field(min_length=1, max_length=MAX_INDICES)
    S_I: str = Field(min_length=1)
    Lambda_I: str = Field(min_length=1)
    K: list[int] = Field(min_length=1, max_length=MAX_INDICES)

    @field_validator("I", "K")
    @classmethod
    def _clean_indices(cls, v: list[int]) -> list[int]:
        if any(i < 0 for i in v):
            raise ValueError("下标不能为负")
        return sorted(set(v))

    @field_validator("values")
    @classmethod
    def _decimal_strings(cls, v: list[str]) -> list[str]:
        for x in v:
            if not x.isdigit():
                raise ValueError(f"values 必须是十进制字符串（群元素一律这样传）：{x!r}")
        return v

    @field_validator("S_I", "Lambda_I")
    @classmethod
    def _decimal_scalar(cls, v: str) -> str:
        # 不校验的话，非数字会一路走到 manager 的 int() 才炸成 500 ——
        # 那是"用户传错"却报成"服务端出错"。
        if not v.isdigit():
            raise ValueError(f"群元素必须是十进制字符串：{v!r}")
        return v

    @model_validator(mode="after")
    def _lengths_match(self) -> "DisaggIn":
        if len(self.I) != len(self.values):
            raise ValueError(
                f"I 有 {len(self.I)} 个下标，但 values 有 {len(self.values)} 个 —— 对不上"
            )
        return self


class FilePatchIn(BaseModel):
    """改**已上传**的文件（``PATCH /api/files/{id}``）。

    三个变体用 ``op`` 区分，共同点是**已有的块一个都不动**（``modify`` 只动它
    点名的那一块，``append`` 只在末尾加新块）：

    * ``modify``（默认，兼容旧请求体）：换某一**块**的内容，会换掉那一块的密钥；
    * ``append``：在**末尾新增**若干块，已有块的密文/密钥/IV 一个字都不动；
    * ``truncate``：**删掉末尾**若干块（``drop_blocks`` = 删几块）。

    .. important::

       ``truncate`` 有一个**方案本身带来的前提**：待删的那几块必须正好是
       **全局向量的末尾**（底层 ``del`` 只能删末尾连续区间）。所以实际上
       **只有最后写进向量的那份文件才删得动尾巴**；别的文件就算排在末尾，
       前面也可能压着之后的块 —— 那种情况会返回 400 并**说清是哪些下标卡住**。

       另外删之前，协调者会先让“与待删区间**部分相交**”的节点把它那一部分
       交回去（论文的 ``RmvStorage``）—— 理由见 ``vds.updates._apply_del``。

    请求体的字段按 ``op`` 取用：``modify`` 要 ``block_idx``；``append`` 不要
    （新块的块序号由服务端接着排），两者都要 ``data_b64``；``truncate`` 只要
    ``drop_blocks``。
    """

    #: ``modify`` / ``append`` / ``truncate``。不传 = ``modify``，
    #: 这样早先只发 ``{block_idx, data_b64}`` 的调用方一字不用改。
    op: str = Field(default="modify", pattern="^(modify|append|truncate)$")
    #: ``modify`` 专用：要换第几块（**文件内的块序号**，从 0 开始）。
    block_idx: int | None = Field(default=None, ge=0)
    #: 新内容的 base64（``append`` 时是要追加在末尾的内容）。
    #:
    #: 用 base64 而不是 utf-8 字符串是必需的：块里完全可能是任意二进制，
    #: 而 JSON 装不下非法 UTF-8 的字节序列。
    data_b64: str | None = None
    #: ``truncate`` 专用：删掉**末尾**几块（从这份文件的最后一块往前数）。
    #:
    #: 上界用 :data:`MAX_INDICES`（= 全局块数上限）——真正的校验在
    #: :meth:`~core.store.VectorStore.truncate` 里：它还要确认那几块
    #: **正好是全局向量的末尾**，而且要留至少一块（不允许删到一块不剩）。
    drop_blocks: int | None = Field(default=None, ge=1, le=MAX_INDICES)

    @field_validator("data_b64")
    @classmethod
    def _valid_base64(cls, v: str | None) -> str | None:
        # 不校验的话，错的 base64 会一路走到 manager 的 b64decode 才炸成 500 ——
        # 那是"用户传错"却报成"服务端出错"。
        if v is None:
            return v
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"data_b64 不是合法的 base64：{exc}") from exc
        if not raw:
            raise ValueError("块内容不能为空（空块没有意义）")
        return v

    @model_validator(mode="after")
    def _required_fields(self) -> "FilePatchIn":
        """按 ``op`` 校必填项。

        放在这里（而不是让字段必填）是为了**旧的 ``modify`` 请求体一字不改**：
        以前直接 422 的两种坏请求（缺 ``block_idx``、缺 ``data_b64``）
        现在仍然 422，只是错误信息更准。
        """
        if self.op == "truncate":
            if self.data_b64 is not None:
                raise ValueError("truncate 不该带 data_b64（删除不提供新内容）")
            return self
        if self.data_b64 is None:
            raise ValueError(f"{self.op} 必须带 data_b64（要写进去的内容）")
        if self.op == "modify" and self.block_idx is None:
            raise ValueError("modify 必须带 block_idx（要改第几块）")
        if self.op == "append" and self.block_idx is not None:
            raise ValueError(
                "append 不该带 block_idx —— 新块的块序号由服务端接着最后一块往下排"
            )
        return self

    def data(self) -> bytes:
        """解出原始字节。校验已在 :meth:`_valid_base64` 里做过。"""
        assert self.data_b64 is not None  # 由 _required_fields 保证
        return base64.b64decode(self.data_b64)
