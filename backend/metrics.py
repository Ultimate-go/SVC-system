"""性能指标采集器 —— 让「性能」页显示系统**真实运行**的画像，而不是抄来的数字。

为什么要有这个模块
------------------
系统里每个写/查询操作本来就用 :func:`core.timing.collect` 量了耗时，但那些
``timings`` 是**请求响应里的临时字段**，用完即弃 —— 页面只能看到「上一次
操作的耗时」，看不到「过去 5 分钟上传是不是变慢了」。

本模块在路由层做一件轻量的事：每次操作结束后，把 ``(操作名, 总耗时, 阶段)``
记进**进程内环形缓冲**。于是「性能」页就能画出：

* 各操作的延迟分位数（p50 / p95 / p99）—— 用分位数不用平均值，
  因为平均值会掩盖少数极慢的请求（一次上传卡 10 秒，平均下来看不出）；
* 吞吐（每分钟多少次操作）；
* 延迟随时间的变化曲线（趋势，看「是不是在变慢」）。

设计取舍
--------
* **只存在内存里（环形缓冲），重启即清空**。演示规模下这是对的：
  性能页关心的是「现在快不快、最近稳不稳」，不是一年前的历史。
  要持久化历史另加一张表即可，但那是监控系统的范畴，不是本系统的卖点。
* **不碰业务库、不碰向量、不写审计**。它只是把本来就在量的数多留一份。
* **采集是零侵入的**：路由里只多一行 ``metrics.record(...)``；
  不采集时（没人调用 record）本模块不产生任何副作用。
"""

from __future__ import annotations

import threading
import time
from collections import deque

__all__ = ["record", "snapshot", "ACTION_NAMES"]


#: 每个操作类型保留的最近样本数。200 个样本足够算分位数、画一段趋势。
_MAX_SAMPLES = 200

#: 时间序列桶宽（秒）。趋势图按这个粒度聚合。
_BUCKET_SECONDS = 10

#: 时间序列保留的桶数（×10 秒 ≈ 半小时）。
_MAX_BUCKETS = 180


class _MetricsStore:
    """进程内指标存储。线程安全（FastAPI 线程池并发）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: 操作名 -> deque({ts, total_ms, stages})
        self._samples: dict[str, deque] = {}
        #: 时间序列桶：bucket_ts(秒，向下取整到桶宽) -> {操作名 -> [样本列表]}
        #: 用列表是为了算每桶的均值/次数，不另存分位。
        self._buckets: dict[int, dict[str, list[float]]] = {}
        #: 当前规模快照（由 perf 路由在取数时更新）
        self._scale: dict = {}

    def record(self, action: str, total_ms: float, stages: list | None = None) -> None:
        """记一次操作。``stages`` 是 ``[{stage, ms, count}, ...]``。"""
        now = time.time()
        sample = {
            "ts": now,
            "total_ms": round(float(total_ms or 0), 3),
            "stages": stages or [],
        }
        with self._lock:
            q = self._samples.get(action)
            if q is None:
                q = deque(maxlen=_MAX_SAMPLES)
                self._samples[action] = q
            q.append(sample)

            bucket = int(now // _BUCKET_SECONDS)
            bmap = self._buckets.get(bucket)
            if bmap is None:
                bmap = {}
                self._buckets[bucket] = bmap
            blist = bmap.get(action)
            if blist is None:
                blist = []
                bmap[action] = blist
            blist.append(float(total_ms or 0))
            # 淘汰过老的桶
            if len(self._buckets) > _MAX_BUCKETS:
                oldest = sorted(self._buckets)[0]
                del self._buckets[oldest]

    def set_scale(self, blocks: int, files: int, nodes: int) -> None:
        """由 perf 路由在取数时更新当前规模。"""
        with self._lock:
            self._scale = {"blocks": blocks, "files": files, "nodes": nodes}

    def snapshot(self) -> dict:
        """给前端的那份完整画像。"""
        with self._lock:
            actions = list(self._samples.keys())
            # 各操作的延迟分位数
            latency = {}
            for a in actions:
                ms_list = sorted(s["total_ms"] for s in self._samples[a])
                latency[a] = {
                    "count": len(ms_list),
                    "p50": _pct(ms_list, 0.50),
                    "p95": _pct(ms_list, 0.95),
                    "p99": _pct(ms_list, 0.99),
                }

            # 时间序列（延迟均值 + 次数，按桶）
            buckets = sorted(self._buckets.keys())
            series = []
            for b in buckets:
                bmap = self._buckets[b]
                point = {"t": b * _BUCKET_SECONDS}
                for a, blist in bmap.items():
                    if not blist:
                        continue
                    point[a] = round(sum(blist) / len(blist), 2)
                    point[f"{a}_n"] = len(blist)
                series.append(point)

            # 吞吐（最近 60 秒各操作次数）
            now = time.time()
            throughput = {}
            for a in actions:
                n = sum(1 for s in self._samples[a] if now - s["ts"] <= 60)
                throughput[a] = n

            return {
                "actions": actions,
                "latency": latency,
                "throughput": throughput,  # 最近 60 秒次数
                "series": series,  # 趋势时间序列
                "scale": dict(self._scale),
                "bucket_seconds": _BUCKET_SECONDS,
                "snapshot_at": now,
            }


def _pct(sorted_ms: list[float], q: float) -> float:
    """第 q 分位数（就近取整，不做插值 —— 演示够用且易读）。"""
    if not sorted_ms:
        return 0.0
    idx = min(len(sorted_ms) - 1, int(q * len(sorted_ms)))
    return round(sorted_ms[idx], 1)


_store = _MetricsStore()


def record(action: str, total_ms: float, stages: list | None = None) -> None:
    """记录一次操作（路由层调用）。"""
    _store.record(action, total_ms, stages)


def snapshot() -> dict:
    """返回完整画像（perf 路由调用）。"""
    return _store.snapshot()


def set_scale(blocks: int, files: int, nodes: int) -> None:
    _store.set_scale(blocks, files, nodes)


#: 操作名 -> 友好中文名。路由里记录时用它统一命名。
ACTION_NAMES = {
    "upload": "上传",
    "modify": "改块",
    "append": "追加",
    "truncate": "截断",
    "query": "查询",
    "query_files": "多文件查询",
    "verify_batch": "批量验证",
    "disagg": "证据分解",
    "por": "存储证明",
    "check": "全面自检",
    "retry_push": "补推",
    "login": "登录",
    "create_user": "建用户",
    "rewrap": "改口令",
}
