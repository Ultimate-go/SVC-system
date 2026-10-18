"""系统状态路由：逐文件摘要、块分布、自检入口、存储证明。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from .. import metrics
from ..deps import audit, current_user, get_db, get_manager, require_admin
from ..manager import Conflict, StoreManager
from ..models import UserRow
from ..advisor import recommend_plan
from core.timing import collect

router = APIRouter(prefix="/api", tags=["system"])


class PoRIn(BaseModel):
    """一次存储证明的挑战参数。

    挑战本身由**服务端**随机生成 —— 客户端只决定点多少个位置。
    让客户端报下标反而是个漏洞：它可以挑“节点一定存着的”那几个，
    于是什么都审不出来。

    ``lambda_pos`` = 论文里的 :math:`\\lambda_{pos}`：被丢掉的数据比例
    必须小于 :math:`1/\\lambda_{pos}` 才可能答对。点得越多越严格、也越贵
    （每台的代价与它被点到几个下标线性相关）。
    """

    lambda_pos: int = Field(default=8, ge=1, le=256)


class PlanIn(BaseModel):
    """一次「这个文件该切多大一块」的询问。

    ``prefer`` 只有两个取值，对应两种真实需求：

    * ``fewest_blocks``（默认）：块数最少 —— 上传与验证都最快，也最省全局位置；
    * ``finer_updates``：块更小 —— **分块更新**的粒度更细（代价是块多、更慢）。
    """

    size: int = Field(gt=0, le=1 << 40, description="文件字节数（明文）")
    prefer: str = Field(
        default="fewest_blocks", pattern="^(fewest_blocks|finer_updates)$"
    )


@router.post("/plan")
def plan(
    body: PlanIn,
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    """块大小顾问：给一个文件大小，回一种**有据可依**的切法。

    推荐依据不是拍脑袋，是 ``scripts/bench_blocksize.py`` 在本机的实测曲线
    （块越大、块数越少 ⇒ 每块固定开销摊得越薄 ⇒ 上传与验证都更快）。
    详见 :mod:`backend.advisor`。

    ★ 它**只读**：不写库、不动向量、不写审计 —— 说的是一句建议。
    ★ 登录即可（与“验证不受限”同一条口径：公开参数的算术，不算业务数据）。
    ★ 改造后块数**不再有上限**（素数按块身份派生，不查 ``[0, n_max)`` 那张表），
    所以不再有“放不下”这种结论 —— 顾问只比较**代价**（上传/验证耗时 vs 改块粒度）。
    """
    st = mgr.status()
    try:
        return recommend_plan(
            body.size,
            min_bytes=st["segment_bytes_min"],
            max_bytes=st["segment_bytes_max"],
            n_max=st["crs"]["n_max"],
            used=st["blocks"],
            prefer=body.prefer,
        )
    except ValueError as exc:
        # 参数错就是错（例如部署配置里块大小范围颠倒）—— 400 说清，不凑合
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/status")
def status(
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    probe: bool = True,
):
    """公开参数与逐文件摘要（每份文件一个 :math:`\\delta = ((U, C), n)`）。

    ``n`` 是全系统**已用位置数**（各文件块数之和，不含历史空洞），
    不是任何一份文件的长度；要按文件看就看响应里的 ``files``。

    :param probe: 是否**逐台探活**存储节点（影响响应里的 ``nodes`` /
        ``nodes_down``）。默认 ``True``：``node_report()`` 会让每台对每份文件
        做一遍密码学自检，代价随文件数增长（实测 7 份文件时 1.5~3 s）。
        只要部署参数与切块口径的页面（上传文件页）传 ``probe=0``：
        那时 ``nodes`` 为 ``null``、``nodes_probed`` 为 ``false`` ——
        “没问”与“问了没答”是两回事，不能都写 0。
    """
    return mgr.status(probe=probe)


@router.get("/nodes")
def nodes(
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    verify: bool = True,
):
    """各台存储服务器的现状：持有多少块、下标跨度、视图是否合法。

    这是界面上"块分布"那张表的数据源。**每台只知道自己的那一段**。

    :param verify: 是否让每台顺带做一遍**密码学自检**（``proved`` 字段）。
        默认 ``True`` —— 节点页/设备页靠它显示「视图校验通过」。
        只要 “谁在线、持有哪些块” 的页面（上传文件页）传 ``verify=0``：
        那时 ``proved`` 为 ``null``（没算过），``valid`` 照旧。
    """
    return mgr.node_report(verify=verify)


@router.post("/por")
def proof_of_storage(
    body: PoRIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    """**存储证明（PoR）** —— 不下载任何内容，确认各节点确实还存着它那份。

    和 ``POST /api/query`` 问的不是同一件事：

    * ``/api/query`` 验的是“你给我的这几块**对不对**”（检索 + 承诺验证），
      而且允许横跨多台、最后合成一份证据；
    * ``/api/por`` 验的是“你**还存着**吗”：服务端随机点名
      :math:`\\lambda_{pos}` 个位置，各节点只答自己沾到的那部分，
      合并成**一份**常数大小的证明，验一次。判据是 ``Q = r``（挑战收齐了）
      且内容没被换掉。

    **权限与“验证不受限”一致**：登录即可，不需要是所有者。
    理由与 ``/api/query`` 相同 —— 审计存储是任何人的权利，
    要读明文才需要**所有者的那把私钥**（块密钥是用他的公钥封的）。

    .. note::

       开了副本之后，本接口**只审计主副本那一份**（挑战按主副本归属拆开 ——
       副本会让多台持有同一下标，而合并算法要求各份额互不相交）。
       审计结果里的 ``shares`` 会逐台列出“问了几个、答了几个”。
    """
    try:
        with collect() as sw:
            out = mgr.pos_audit(lambda_pos=body.lambda_pos)
    except Conflict as exc:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit_ok = out["ok"]
    metrics.record("por", sw.total_ms(), sw.rows())
    return {
        **out,
        "checked_by": user.username,
        # ★ 阶段耗时：生成挑战 / 按主副本分派挑战 / 各节点生成证明份额 / 份额合成证据 / 配对检验
        "timings": sw.payload(),
        # 把两个数字提前摆好，前端不必自己数
        "asked_total": sum(s["asked"] for s in out["shares"]),
        "answered_total": sum(s["answered"] for s in out["shares"]),
        "nodes_down": [s["node_id"] for s in out["shares"] if s.get("unreachable")],
        "verdict": "通过" if audit_ok else "未通过",
    }