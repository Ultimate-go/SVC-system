"""控制台输出的小工具 —— 让**脚本**在 GBK 控制台上不会因为一个符号崩掉。

★ 为什么需要这个文件（真实踩坑记录）

Windows 的控制台默认是 **GBK**（代码页 936），而这类符号**不在 GBK 里**：

``✓`` ``✗`` ``⚠`` ``⇒`` ``ℓ`` ``∅`` …

于是任何一句 ``print("⇒ 批量慢 2.15 倍")`` 都会在 Windows 上抛
::

    UnicodeEncodeError: 'gbk' codec can't encode character '\u21d2' ...

后果比"输出乱码"严重得多：**报错本身变成了 traceback**，真正要看的
失败原因被埋在中间。实测踩过两次：

* ``scripts/start_all.py`` 打印 vite 启动日志里的 ``➜``（U+279C）；
* ``scripts/bench_batch.py`` 打印结论里的 ``⇒``（U+21D2）。

修法就是下面这个函数：把本进程的 stdout/stderr **改成 UTF-8**，
并且 ``errors="replace"`` —— 万一日志里还有编不出来的字符，
显示成 ``?`` 也比整个脚本崩掉强。

.. important::

   **只在脚本 / 命令行入口里调用它，不要在库模块里调用。**
   在库模块导入时改别人的流是副作用（测试抓取输出、嵌入调用都会受影响）。
   所以本模块只被 ``scripts/*.py`` 与 ``demo.py`` 使用。

.. note::

   ``scripts/start_all.py`` 里有一份**等价的本地实现**（先写的，带着那次
   ``➜`` 事故的原话）。语义一样，新脚本一律用这里这个共享的。
"""

from __future__ import annotations

import sys

__all__ = ["force_utf8_console", "safe_print"]


def force_utf8_console() -> None:
    """把本进程的 stdout/stderr 切成 UTF-8，编不出来的字符替换而不是抛异常。

    幂等：重复调用没有副作用（改不了就静默跳过 —— 比如输出被重定向到
    不支持 ``reconfigure`` 的对象上）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - 老 Python / 被换成了别的流对象
            pass


def safe_print(msg: str = "") -> None:
    """打印一行，**任何情况下都不因为这个崩掉**（最后一道兜底）。"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 控制台不给我们改编码时的底线
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
