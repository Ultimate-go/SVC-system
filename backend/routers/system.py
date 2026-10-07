"""系统状态路由：全局摘要、块分布、自检入口、存储证明。"""

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
    * ``finer_updates``：块更小 —— **改一块**的粒度更细（代价是块多、更慢）。
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
):
    """全局摘要 :math:`\\delta = ((U, C), n)` 与公开参数。

    ``n`` 是全系统**已用位置数**（不是某个文件的长度）—— 设计 B 下
    它就是"全局块数"。
    """
    return mgr.status()


@router.get("/nodes")
def nodes(
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    """各台存储服务器的现状：持有多少块、下标跨度、视图是否合法。

    这是界面上"块分布"那张表的数据源。**每台只知道自己的那一段**。
    """
    return mgr.node_report()


@router.get("/nodes/pending")
def nodes_pending(
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    r"""有没有"写推失败了、还没补"的更新。

    这是**真故障**（全网不一致），不是"慢"：已经有节点按新 δ 前进了，
    另几台还在旧 δ 上 —— 这时读取可能少拿到块，写入会被闸住。

    ★ **查它不设管理员门槛**，与"验证不受限"同一个理由：δ 本来就是公开的，
    "全网摘要齐不齐"因此也是公开信息 —— 谁都该能看见"现在不一致"。
    需要管理员的是**修**它（见下一个接口），那是写操作。

    .. note::

       读数与写入共用一把锁，所以这个接口看到的是一致的一瞬间的快照；
       它**不会**去打扰各节点，纯本地账。
    """
    return mgr.pending_write()


@router.post("/nodes/retry-push")
def nodes_retry_push(
    user: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**补推**：把上次没推成功的那次更新，只补推给没跟上的那几台。

    ⚠ 为什么不是"重新传一次文件"：协调者的 δ 没推进，但**登记表已经分配过**
    那批下标 —— 再传一次会拿到新下标，还会撞上"登记表游标与向量长度不一致"。
    补推是唯一的出路。

    ``ok=false`` 不是"没救"：把没通的机器弄活，再点一次即可
    （已经补上的不会被重复打扰）。
    """
    with collect() as sw:
        out = mgr.retry_push(manual=True)
    audit(
        db,
        user.username,
        "retry_push",
        f"{out.get('op', '') or '—'} / {len(out.get('nodes', []))} 台未通",
        ok=bool(out.get("ok")),
        detail=str(out.get("detail", "")),
    )
    # ★ 阶段耗时：重新算摘要 / 只补推给没跟上的那几台 / 落库收尾
    out["timings"] = sw.payload()
    metrics.record("retry_push", sw.total_ms(), sw.rows())
    return out

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
        # ★ 阶段耗时：生成挑战 / 按主副本拆挑战 / 各节点生成证明 / 聚合份额 / 验证证明
        "timings": sw.payload(),
        # 把两个数字提前摆好，前端不必自己数
        "asked_total": sum(s["asked"] for s in out["shares"]),
        "answered_total": sum(s["answered"] for s in out["shares"]),
        "nodes_down": [s["node_id"] for s in out["shares"] if s.get("unreachable")],
        "verdict": "通过" if audit_ok else "不通过",
    }