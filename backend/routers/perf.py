"""性能画像路由 —— 页面显示系统**真实运行**的延迟与吞吐。

数据来自 :mod:`backend.metrics`：各路由在每次操作结束后把耗时记进进程内
环形缓冲，这里把它们聚合成一份画像给前端：

* 各操作的延迟分位数（p50 / p95 / p99）—— 用分位数不用平均值，
  因为平均值会掩盖少数极慢的请求；
* 吞吐（最近 60 秒各操作的次数）；
* 延迟随时间的变化序列（趋势图数据源）；
* 当前规模（块数 / 文件数 / 节点数）。

★ 只读、不写审计 —— 与「验证不受限」同一条口径：这些是系统自己的
  运行画像，不涉及业务数据，登录即可看。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import metrics
from ..deps import current_user, get_manager
from ..manager import StoreManager
from ..models import UserRow

router = APIRouter(prefix="/api/perf", tags=["perf"])


@router.get("/summary")
def summary(
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    """返回性能画像：延迟分位数、吞吐、趋势序列、当前规模。"""
    st = mgr.status()
    # 把当前规模写进指标快照（同一份报告，别多探活一次）
    metrics.set_scale(
        blocks=st["blocks"],
        files=st["files"],
        nodes=st["nodes"],
    )
    snap = metrics.snapshot()
    # 把友好名和规模参数一起带上，前端不必自己映射
    return {
        **snap,
        "names": metrics.ACTION_NAMES,
        "crs": {
            "n_bits": st["crs"]["N_bits"],
            "l": st["crs"]["l"],
            "n_max": st["crs"]["n_max"],
        },
    }
