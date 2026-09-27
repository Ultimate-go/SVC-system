"""阶段秒表 —— 给前端报"每一步**真实**花了多少毫秒"。

为什么要有这个模块
------------------
后端一次请求里其实做了很多步（切块 → 逐块加密 → 算向量分量 → 承诺 →
分发 → 落库），而前端只看到一个"正在转"的动画。用户既不知道等的是 3 秒
还是 3 分钟，也不知道时间花在哪一步 —— 而"时间花在哪一步"恰恰是这套系统
最值得看的东西（承诺要算模幂、封装要做椭圆曲线标量乘、分发是网络往返，
三者的量级完全不同）。

所以这里提供一个**可以插在任何地方**的秒表：在 ``core/store.py`` 那些
真正干活的位置埋上 ``with stage("逐块加密（SM4）"):``，前端就能拿到一份
真实的阶段耗时表。

★ 设计上最重要的一条：**默认零开销、零行为变化**
------------------------------------------------
秒表不是当参数一层层传下去的（那要改一堆函数签名，还得给每一层加默认值），
而是放在 :class:`contextvars.ContextVar` 里。于是：

* 埋点那一行**永远**是 ``with stage("名字"):``，不管有没有人在计时；
* 没有开启收集时，``stage()`` 返回 :data:`contextlib.nullcontext` ——
  纯粹是个空上下文，**一次计时调用都不会发生**；
* 因此现有测试、以及任何没有显式 ``collect()`` 的调用路径，
  **行为与加这个模块之前逐位相同**。

★ 为什么用 ContextVar 而不是 threading.local
-------------------------------------------
FastAPI 的同步路由是丢进线程池跑的（一个请求一个工作线程）。``threading.local``
在这里也能work，但只要哪天把某个路由改成 ``async def``，跨 ``await`` 就会
串味：同一个线程会在等待期间去处理**别的**请求，于是 A 请求的埋点会被记到
B 请求的秒表上。``ContextVar`` 是按"任务上下文"隔离的，两种写法都对。

用法
----
生产者（``backend/routers/*.py``）::

    with collect() as sw:
        result = mgr.some_heavy_thing(...)
    payload["timings"] = sw.rows()

消费者（``core/store.py``、``backend/manager.py``）::

    with stage("承诺（commit）"):
        pushed = push_update(...)

同名阶段会被**累加**成一个条目（逐块加密会调用 N 次），并记下次数，
前端就能显示成"逐块加密（SM4） 12.4 ms ×12"。

★ 一条纪律：**阶段不要嵌套**
--------------------------
外层的 ``with`` 已经把内层的耗时算进去了，两个都记就是**记重**
（于是"各段之和"会大于"总时长" —— 那是一条会撒谎的进度条，比没有更糟）。
所以埋点时保持**平铺**：一个阶段结束、下一个才开始。

本仓库现有的埋点都是平铺的；``tests/test_timing.py`` 里
:meth:`Test秒表本体.test_各阶段之和不超过总时长` 钉住了这条不变式。
真需要看更细一层时，**把外层那个换成内层**，而不是两层都留。
"""

from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

__all__ = ["Stopwatch", "collect", "stage", "current"]


#: 当前正在收集的秒表。``None`` = 没人在计时（埋点全部是空操作）。
_ACTIVE: ContextVar["Stopwatch | None"] = ContextVar("vds_stopwatch", default=None)


class Stopwatch:
    """累积各阶段耗时的小账本。

    只用 :func:`time.perf_counter` —— 它是单调时钟，**不受系统时间被改**
    影响（用 ``time.time()`` 的话，一次 NTP 校时就能让耗时变成负数）。
    """

    __slots__ = ("label", "t0", "_rows", "_order")

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.t0 = time.perf_counter()
        self._rows: dict[str, dict] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------
    def add(self, name: str, seconds: float, *, count: int = 1) -> None:
        """把一个**已经量好**的时长记进 ``name`` 这一栏（同名累加）。"""
        row = self._rows.get(name)
        if row is None:
            row = {"stage": name, "ms": 0.0, "count": 0}
            self._rows[name] = row
            # 顺序按**第一次出现**的位置排 —— 所以阶段条的从左到右
            # 就是真实执行顺序，不是字典的哈希顺序。
            self._order.append(name)
        row["ms"] += seconds * 1000.0
        row["count"] += count

    @contextmanager
    def stage(self, name: str, *, count: int = 1):
        """``with sw.stage("名字"):`` —— 退出时（含抛异常）记下这一段。"""
        t = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - t, count=count)

    # ------------------------------------------------------------------
    def rows(self) -> list[dict]:
        """给前端的那份表：``[{"stage", "ms", "count"}, ...]``，顺序＝执行顺序。"""
        return [
            {
                "stage": n,
                "ms": round(self._rows[n]["ms"], 3),
                "count": self._rows[n]["count"],
            }
            for n in self._order
        ]

    def total_ms(self) -> float:
        """秒表自己的挂钟总时长。

        ★ 它会**略大于**各阶段之和 —— 差额就是埋点没覆盖到的那些零碎
        （取值、判空、构造响应…）。这个差额是真实的，不该被人为抹平：
        所以前端把"合计"和"各段之和"分开摆，而不是把差额摊到某一段里。
        """
        return round((time.perf_counter() - self.t0) * 1000.0, 3)

    def payload(self) -> dict:
        """直接塞进接口响应的那种形状。"""
        return {"total_ms": self.total_ms(), "stages": self.rows()}

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<Stopwatch {self.label!r} {self.total_ms()}ms over {len(self._rows)} stages>"


# ---------------------------------------------------------------------------
# 平铺的进出接口（埋在算法核里的都是这两个，不直接碰 Stopwatch）
# ---------------------------------------------------------------------------


@contextmanager
def collect(label: str = ""):
    """开启收集：``with collect() as sw: ...`` 之后用 ``sw.rows()`` 取结果。

    嵌套调用会各拿各的秒表（内层的阶段不会漏进外层）——
    这正是我们要的：一个路由一次收集，别把整台服务器的账混在一起。
    """
    sw = Stopwatch(label)
    token = _ACTIVE.set(sw)
    try:
        yield sw
    finally:
        # ★ 必须 reset 而不是 set(None)：工作线程是被复用的，
        #   漏掉这一步会让**下一个**请求莫名其妙地记到上一个的秒表里。
        _ACTIVE.reset(token)


def current() -> Stopwatch | None:
    """当前正在收集的秒表；没人在收集时返回 ``None``。"""
    return _ACTIVE.get()


def stage(name: str, *, count: int = 1):
    """埋点。没人收集时返回 :data:`~contextlib.nullcontext`（**零开销**）。

    :param count: 这一步代表几件事（例：逐块加密调了 N 次就传 ``count=块数``）。
        这样前端能显示"12.4 ms ×12"，而不是让人以为这一栏只跑了一次。
    """
    sw = _ACTIVE.get()
    if sw is None:
        return nullcontext()
    return sw.stage(name, count=count)
