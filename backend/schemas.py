"""请求体模型。

只对**入参**做 pydantic 校验；响应统一返回 dict —— 里面大量是十进制字符串
表示的群元素，为它们再定义一遍模型得不偿失，而且前端本来就只看指纹。
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

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
    "FaultDrillIn",
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
    """登录。

    :param server_key: **要不要让后端代管私钥**（默认 ``False``）。

        * ``False``（默认）：登录**不解封私钥** —— 后端只把私钥的**密文**
          （``users.sk_wrapped``）交给浏览器，解封由浏览器用同一个口令完成。
          此后后端**不掌握任何私钥**，"服务器不可信"这条前提才成立。
        * ``True``：旧路径（后端用口令解封并扣在内存里）。
          留着它只有一个用途：把两种安全模型并排对比。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    server_key: bool = False


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
    #: ★ 浏览器封好的**新私钥密文**（安全审计 I8）。
    #:
    #: 默认模型（私钥在客户端）下服务端**不持有私钥**，没法自己重封；
    #: 所以改口令时由**浏览器**在本地用新口令重封，再把**密文**交上来。
    #: 服务端只验两件事：解得开、而且里面的私钥**还是原来那把**
    #: （见 ``StoreManager.rekey_with_blob``）。
    sk_wrapped: dict | None = None


class AuditRemarkIn(BaseModel):
    """给一条审计流水写人工备注。

    传空串 = **清空备注**（见 ``admin.set_audit_remark``）——
    所以这里不能把 ``""`` 与"没传"当成同一件事，用默认值 ``""`` 表示
    "就是要清空"，语义上正好一致。
    """

    remark: str = Field(default="", max_length=2000)


#: 请求里「下标 / 值」列表的长度上限。
#:
#: ★ 它现在是一个纯粹的**请求体大小**限制，与密码学无关。
#:   改造前它还兼一职：必须 ≥ 系统的块数上限（``n_max``），
#:   否则会出现「**传得上去、验不了**」—— 一个 8 MB 文件的完整查询
#:   会被 pydantic 以 422 拒掉。改造后块数**没有上限**，
#:   所以这个数不再是"必须 ≥ 某个系统上限"，而是
#:   "一次请求多大了算过分"：8192 个下标 ≈ 每个下标一个十进制串，
#:   请求体在 100 KB 量级，JSON 解析与校验都很快。
#:
#: ★ 真遇到"一份文件好几万块、想一次全验"时，**提高它是对的**
#:   （而以前提高它还要先动 ``n_max``）—— 但更该做的是分批：
#:   超出时界面会建议分批验（见 ``stores/basket.js``）。
MAX_INDICES = 8192

#: 一个群元素 / 分量写成十进制串时的**最长**字符数。
#:
#: ★ 它拦的不是“大整数”本身，而是**把用户输入错报成 500**（安全审计 P6 / I4）：
#:   Python 3.12 对 >4300 位的十进制串 ``int()`` 会抛 ``ValueError``
#:   （``sys.set_int_max_str_digits`` 的默认值），而 4300 位远远超过本方案里
#:   任何合法值：
#:
#:   * ``N`` 是 1024 位 ⇒ 十进制最多 **309** 位；
#:   * ``U`` / ``C`` / ``S_I`` / ``Λ_I`` 都 < N ⇒ 同样 ≤ 309 位；
#:   * 分量是 SM3 摘要（256 位）⇒ ≤ 78 位。
#:
#:   取 512：既不误伤任何合法输入，又让“塞一个 5000 位数字串”在 **pydantic 层**
#:   变成 422，而不是一路走到 ``int()`` 才炸成 500（那是用户的错，却报成服务端的错）。
MAX_INT_CHARS = 512

#: ``data_b64``（写块的 base64 内容）的字符数上限。
#:
#: ★ 安全审计 I3：这个字段原先**没有上界**，而它的校验器会做 ``b64decode`` ——
#:   传 1 GB 也会先解码。上限与 ``routers/files.py::MAX_UPLOAD_BYTES``（8 MiB）
#:   对齐，再留 base64 的 4/3 膨胀余量与 multipart 开销：12 MiB 字符。
#:   （两处都改时记得一起改。）
MAX_B64_CHARS = 12 * 1024 * 1024

#: “十进制群元素串”字段的类型 —— 长度至少 1、最多 :data:`MAX_INT_CHARS`。
DecStr = Annotated[str, StringConstraints(min_length=1, max_length=MAX_INT_CHARS)]


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

    #: 一次最多问多少份文件。
    #: ★ 加它是因为以前**没有上界**（审计 I4）：一份超长的 ``targets`` 会
    #:   在展开时把 CPU / 内存吃掉。512 份远超过任何真实用法。
    targets: list[tuple[str, str]] = Field(min_length=1, max_length=512)
    #: 块序号。**两种形态**（见 :meth:`StoreManager.query_files`）：
    #:
    #: * ``[0, 1, 2]`` —— 对**每个** target 都取这几个块号（**共用**；老语义，保留）；
    #: * ``[[0, 1], [5, 6]]`` —— **与 targets 一一对应**，第 i 组只作用于第 i 个 target。
    #:
    #: ★ 为什么必须支持第二种：**聚合**出来的那张卡，每份文件覆盖的块号是
    #:   **不同**的（例如「50KB 第 0-49 块 + 64KB 第 0-63 块」）。只有“共用块号”的话，
    #:   这种卡**根本重现不出来** —— 点“取回”只能拿到单份文件的证据，
    #:   看上去就像是“接口拿错了东西”。
    #:
    #: ★ 长度上界在 manager 里查（联合类型套不上 `max_length`）：
    #:   外层最多 `MAX_TARGETS` 组（与 targets 对齐），内层每组最多 `MAX_INDICES` 个。
    block_indices: list[int] | list[list[int]] | None = None


class ProofIn(BaseModel):
    r"""一份打开证据 :math:`\pi_I = (S_I, \Lambda_I)`（大整数写成十进制字符串）。

    两个群元素都用 :data:`DecStr`（有长度上限）—— 不限长的话，
    ``int()`` 会先撞上 Python 的十进制串限制，把“用户传太长”报成 500。
    """

    S_I: DecStr
    Lambda_I: DecStr
    I: list[int] | None = Field(default=None, max_length=MAX_INDICES)


class EvidenceCardIn(BaseModel):
    """证据池里的一张卡片 —— 与 ``/api/query`` 响应**同构**。

    字段名刻意与那个响应一致（``indices`` / ``values`` / ``proof``），
    这样前端把池子里的卡片**原样传回来**即可，不用另造一份数据结构。
    """

    indices: list[int] = Field(min_length=1, max_length=MAX_INDICES)
    values: list[DecStr] = Field(min_length=1, max_length=MAX_INDICES)
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

    indices: list[int] | None = Field(default=None, max_length=MAX_INDICES)


class ReplayIn(BaseModel):
    r"""**回滚演示**的开关（演示“服务器不可信”）。

    :param block_idx: 要对哪一块动手（**这份文件里的第几块**，0 起算）。
    :param on: ``True`` = 从这一刻起，``/cipher`` 对这块交回**存下来的旧版本**；
        ``False`` = 恢复正常。

    ★ 演示顺序：**先** ``on=true``（存下当前这版），**再**改块，**然后**解密
      —— 服务器交回的正好是“改之前那一版”，客户端验证不通过。
    """

    block_idx: int = Field(ge=0, le=MAX_INDICES)
    on: bool = True


class DisaggIn(BaseModel):
    """**分解**一份已有证据：从 :math:`\\pi_I` 里拆出 :math:`\\pi_K`（要求 K ⊆ I）。

    前四个字段就是 ``/api/query`` 响应里的 ``indices`` / ``values`` / ``proof``
    —— 前端把池子里那张卡片**原样传回来**即可，不用另造一份数据结构。

    字段名用 ``I`` / ``K`` 而不是 snake_case，是刻意跟论文符号对齐：
    界面上也要显示它们（“从 {0,1,2} 拆出 {0,2}”），叫一样的名字少一层翻译。
    """

    I: list[int] = Field(min_length=1, max_length=MAX_INDICES)
    values: list[DecStr] = Field(min_length=1, max_length=MAX_INDICES)
    S_I: DecStr
    Lambda_I: DecStr
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

    四个变体用 ``op`` 区分，共同点是**已有的块一个都不动**（``modify`` / ``zero``
    只动它点名的那一块，``append`` 只在末尾加新块）：

    * ``modify``（默认，兼容旧请求体）：换某一**块**的内容，会换掉那一块的密钥；
    * ``zero``：把某一**块**换成**等长的全 0 字节** —— 它走的是 ``mod`` 而不是
      ``del``，所以块仍在（下标不变、仍占存储、``n`` 不变），只是内容换了。
      长度由服务端按这一块**原来的长度**填（请求里**不要**给 ``data_b64``）；
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

    #: ``modify`` / ``zero`` / ``append`` / ``truncate``。不传 = ``modify``，
    #: 这样早先只发 ``{block_idx, data_b64}`` 的调用方一字不用改。
    op: str = Field(default="modify", pattern="^(modify|zero|append|truncate)$")
    #: ``modify`` / ``zero`` 专用：要动第几块（**文件内的块序号**，从 0 开始）。
    block_idx: int | None = Field(default=None, ge=0)
    #: 新内容的 base64（``append`` 时是要追加在末尾的内容）。
    #:
    #: 用 base64 而不是 utf-8 字符串是必需的：块里完全可能是任意二进制，
    #: 而 JSON 装不下非法 UTF-8 的字节序列。
    data_b64: str | None = Field(default=None, max_length=MAX_B64_CHARS)
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
        if self.op == "zero":
            # 清零的内容由**服务端**按这一块原来的长度填全 0：让客户端传一遍等长
            # 的零字节没有意义，而且一旦传错（长度不对）就会多出一次长度变化。
            if self.data_b64 is not None:
                raise ValueError(
                    "zero 不该带 data_b64 —— 清零的内容由服务端按这一块原来的长度填全 0"
                )
            if self.block_idx is None:
                raise ValueError("zero 必须带 block_idx（要清零第几块）")
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


class FaultDrillIn(BaseModel):
    r"""★ **故障演练**的入参（``POST /api/admin/fault-drill``）。

    :param action: ``knock_out``（让节点出事）/ ``restore``（让节点回来）/
        ``status``（只看不改）。
    :param nodes: 哪几台。``restore`` 时给 ``None`` = 全部恢复；
        ``knock_out`` 时必须给。
    :param mode: 只在 ``knock_out`` 时有意义。

        * ``down`` —— **掉线**：机器联系不上，磁盘好端端的。
          对应"网线拔了 / 进程挂了 / 机房断电"。恢复之后数据**照样在**。
        * ``destroyed`` —— **永久损毁**：机器联系不上，**数据也没了**。
          对应"地震 / 海啸 / 机房烧了"。**不可逆** —— 恢复时它回来是空的，
          只能靠副本重建；某个块的每一份副本都在这张名单里 ⇒ 那块真丢了。

    .. note::

       它**只是模拟**：不改密码学、不改分片，只让指定节点不可达。
       所以接下来看到的失败，是真代码在"节点没了"时的真实行为。
       **只对管理员开放** —— 它会主动制造全网不一致，是破坏性操作。
    """

    action: str = Field(pattern="^(knock_out|restore|status)$")
    nodes: list[str] | None = Field(default=None, max_length=4096)
    mode: str = Field(default="down", pattern="^(down|destroyed)$")
