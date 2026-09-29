r"""一键启动：若干台存储节点 + 后端 + 前端，然后打开浏览器。

为什么要这个脚本
----------------
跨进程模式要在**两个终端**里各做一件事，而且那串环境变量必须和后端写在
**同一个终端**里 —— 漏了就静默变成单进程模式，接着被启动闸（
``_verify_nodes_at_startup``）以"节点与协调者不同步"拒绝启动。
那个报错看起来像数据坏了，其实只是环境变量没传进去。所以这里把它收成一条命令。

跑法::

    python scripts/start_all.py              # 起（保留已有数据；没有库就只建账号）
    python scripts/start_all.py --reset      # 清空库与节点数据，只建账号，再起
    python scripts/start_all.py --demo       # 顺带灌 5 个演示文件（会真的推到节点上）
    python scripts/start_all.py --stop       # 把这几样全停掉
    python scripts/start_all.py --no-browser # 不自动开浏览器
    python scripts/start_all.py --nodes 6    # 明确按 6 台起（并写回部署配置）

**默认不带演示文件** —— 文件、切成几块、怎么切，都由你在界面上自己选。

台数从哪来
----------
**这里不再问使用者**（以前双击 .cmd 会弹一句"起几台"）。规则就三条：

============================  ==========================================
``--nodes N``                 用 N，**并且写回部署配置**（命令行是显式覆盖）
``--reset``（没给 ``--nodes``）  台数**复位成 4**，再按 4 台起
以上都没有                    读部署配置 ``nodes/deploy.json``
============================  ==========================================

所以"改台数"这件事只有一个入口：**管理员界面**。它写的就是那个 JSON 文件，
于是「改它 → 提示重启 → 重启时按新台数起节点和后端」这条链路里，
那个"改"和这个"读"是同一份数据，不存在两处配置对不上的可能。

``--nodes`` 会写回配置，是为了不让它变成一个"只在这一次生效、界面却看不出来"
的隐形开关 —— 命令行改过之后，管理员界面显示的就是你刚起的那几台。

幂等
----
已经在跑的东西**不会再起一个**：脚本先探活（各存储节点 / 后端 / 前端，端口都
取自部署配置 ``nodes/deploy.json``），活的就跳过。所以"点两下"是安全的。

端口从哪里来
------------
与台数**同一个文件、同一个模型**（``nodes/deploy.json`` 的 ``ports``）：
后端、前端、以及**每一台存储节点各自的端口**都可以在管理员界面里改，
改完点「立刻重启」生效。默认分别是 8000 / 5173 / 9101,9102,…（连号）。
``scripts/start_all.py`` 自己**不写死任何端口** —— 它是这套配置的读取方。

令牌
----
节点一律要求 ``X-Node-Token``，而且必须与后端用同一个值。这个脚本把令牌
存在 ``nodes/token.txt``（第一次自动生成），以后每次都用它 —— 不需要你抄来抄去。
若发现节点在跑但**令牌不是这一份**（探活回 401），脚本会把那几台重启成
新令牌（数据目录不动，所以数据不丢）。

日志
----
后端与前端的输出分别写到 ``logs/backend.log`` 与 ``logs/frontend.log``。
后端起不来时脚本会把日志尾巴打出来 —— 不然"窗口一闪就没了"最难查。

``--reset`` 的边界（说清，别误用）
----------------------------------
它删的是：``vds.db``（协调者库）、``nodes/node-*/``（各节点库）、
``logs/*.log``。**不动** ``nodes/token.txt``。

★ 它还会把**服务器台数复位成默认值**（``nodes/deploy.json`` → 4 台）——
这是"重置"该有的样子：一切回到出厂状态。想保留台数就别加 ``--reset``，
或者显式 ``--reset --nodes 6``。

★ 清理节点目录用的是 **``nodes/node-*`` 通配**，而不是"当前这几台"。
为什么：上一次可能是 ``--nodes 6`` 起的，那 6 个目录都在；按当前台数
（比如 2）去删就会漏掉 4 个 —— 而漏掉的那些**会让下次启动被启动闸拒掉**
（协调者库被清了、节点库却还留着旧状态），报错看起来像"数据坏了"，
其实只是没删干净。

``--reset`` 之后节点是空的、协调者也是空的（n = 0），两边一致，所以启动闸放行。
反过来，**只清库不清节点**一定会被启动闸拒掉 —— 这就是为什么它俩必须一起清。

★ **顺序**：节点 → 账号（可能含演示文件）→ 后端 → 前端。
“账号”那一步必须排在节点**之后**，而且带着与后端同一套环境变量跑：
``scripts/seed.py`` 是按 ``VDS_NODE_URLS`` 决定单进程/跨进程的，
不带它就是单进程 —— 于是节点状态被写进**协调者库**，跨进程的后端一启动就拒：
“库里还留着节点状态，但当前跑的是跨进程模式”。
（这是真踩到的：``--reset --demo`` 会稳稳地炸在后端启动那一步。）
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = sys.executable

#: ★★ 台数的**唯一真源**是 ``backend/config.py``（落在 ``nodes/deploy.json``）。
#: 这个脚本不再自己写死一个 4，也不再问使用者 —— 规则见文件头「台数从哪来」。
from backend.config import (  # noqa: E402
    DEFAULT_BACKEND_PORT,
    DEFAULT_FRONTEND_PORT,
    DEFAULT_NODE_BASE_PORT,
    DEFAULT_NODE_COUNT,
    MAX_NODE_COUNT,
    MIN_NODE_COUNT,
    DeployPorts,
    clamp_node_count,
    effective_ports,
    read_deploy_node_count,
    read_deploy_ports,
    read_deploy_stop_ports,
    reset_deploy_node_count,
    reset_deploy_ports,
    write_deploy_node_count,
)

#: 默认节点名。``--nodes N`` 或部署配置会把它换成 node-1..node-N（全脚本共用这一份）。
#: ★ 后端与前端**不写死台数** —— 后端从 ``VDS_NODE_URLS`` 读，前端从
#:   ``/api/admin/deploy``（管理员）与 ``/api/nodes`` 取，所以改这里就够了。
NODES: tuple[str, ...] = tuple(
    f"node-{i}" for i in range(1, DEFAULT_NODE_COUNT + 1)
)

#: 三个端口**不再写死在这里**：真源是 ``nodes/deploy.json``（管理员界面写的就是它），
#: 由 :func:`backend.config.effective_ports` 算出来。下面这三个只当
#: "配置文件坏了 / 没见过这个文件"时的兜底，而在 :func:`stop_port_list` 里
#: 那一整段默认区间也始终会被扫到 —— 老的残留靠它收尾。
BASE_PORT = DEFAULT_NODE_BASE_PORT
BACKEND_PORT = DEFAULT_BACKEND_PORT
FRONTEND_PORT = DEFAULT_FRONTEND_PORT

#: ★★ 本次要用的那一套端口。
#:
#: 为什么用模块级变量而不是一路传参：这里十几个函数都要问端口
#: （起节点、探活、停止、banner、轮询、收尾），逐个改签名会把它们搞乱；
#: 而这个脚本是**一次性进程**，全局态没有第二个使用者。
#:
#: ★ 导入时就先算一遍（不是留个空字典）：``scripts/restart.py`` 会 import 本模块
#: 并直接调 :func:`stop_port_list`，而那个函数要按**配置里**的端口去扫 ——
#: 如果这里只是个空壳，它就只能靠默认那一段，改过端口的残留就收不着了。
#: :func:`main` 里还会按**这一次的**台数再算一遍（``--nodes`` 会改台数）。
PORTS: DeployPorts = effective_ports()[0]
TOKEN_FILE = ROOT / "nodes" / "token.txt"
LOG_DIR = ROOT / "logs"

#: 「立刻重启」用的**交接标记**。
#:
#: ★★ 为什么非有它不可：启动器除了起东西，还有一个职责 —— 盯着自己的子进程，
#:   谁退出了就收尾（**按端口扫一遍**，把节点收干净）。
#:   而「立刻重启」做的恰好是“先把后端杀掉、再按新台数起一遍”：
#:   后端一死，旧启动器就会去扫 9101..9164 ——
#:   而重启器这时已经把**新节点**起在那个区间上了，于是被一起收走。
#:   表现就是“点了立刻重启，然后新的起不来 / 起来又没了”。
#:
#:   所以重启器在**停任何东西之前**先写下这个标记；启动器看到它就
#:   **主动交出控制权**：不报错、不扫端口、退出码 0 ——
#:   ``一键启动.cmd`` 收到 0 会直接把它那个窗口关掉（不用人去关）。
#:   标记的生死由重启器管：它收完尾、起新栈之前删掉（同时兜底地由本脚本
#:   启动时清一次，免得重启器半路挂了留下一个过期标记）。
RESTART_MARKER = LOG_DIR / "restart.pending"

#: 与 :mod:`scripts.seed` 里那份保持一致。这里是**只读引用**，
#: 改口令请改 ``scripts/seed.py`` 的 ``DEMO_PASSWORD``（它才是权威）。
from scripts.seed import DEMO_PASSWORD, USERS  # noqa: E402


# ---------------------------------------------------------------- 小工具


def _force_utf8_console() -> None:
    """把本进程的输出改成 UTF-8，编不出来的字符直接替换而不是抛异常。

    ★ 为什么非做不可：Windows 默认控制台是 **GBK**，而 vite 的启动日志里有
    ``➜``（U+279C）这种字符。子进程一退出，本脚本就会把前端日志尾巴打印出来
    —— 那一瞬间会抛 ``UnicodeEncodeError: 'gbk' codec can't encode character
    '\\u279c'``，于是“报错”本身变成了一堆 traceback，根本看不到真正的失败原因。
    实测踩过：``--nodes 2`` 那一轮就是这样收场的。

    ``errors="replace"`` 是第二道防线：万一日志里还有编不出来的字符，
    显示成 ``?`` 也比整个脚本崩掉强。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - 老 Python / 被重定向到不支持的对象
            pass


def say(msg: str = "") -> None:
    """打印一行。**任何情况下都不能因为这个崩掉**。"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 控制台不给我们改编码时的底线
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)


def alive(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """端口上有人听就算活。**不关心对方是谁** —— 只用来判断"要不要再起一个"。"""
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def listening_map() -> dict[int, list[int]]:
    """**一次**问出所有正在监听的端口 → 是谁在听。

    ★★ 为什么要成批问（这条坑是扫描范围放大之后才露出来的）：
    每次起一个 PowerShell 进程在 Windows 上要 0.5--1 s。
    :func:`stop_everything` 要扫一整段端口（见 :data:`STOP_PORT_SPAN`）——
    若按端口**逐个**问，65 个端口就是 65 次进程启动，实测会静默卡上**好几分钟**，
    表现和“卡死了”一模一样（卡在 ``[0/4] 重置`` 那行，连 ``vds.db`` 都还没删）。
    一次问完全部只要一次进程启动。
    """
    ps = (
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.LocalPort) $($_.OwningProcess)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return {}
    table: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        table.setdefault(int(parts[0]), []).append(int(parts[1]))
    return {p: sorted(set(pids)) for p, pids in table.items()}


def listener_pids(port: int) -> list[int]:
    """谁在这个端口上听。

    走 PowerShell 的 ``Get-NetTCPConnection -State Listen`` 而不是 ``netstat`` ——
    ``netstat`` 的状态列在中文系统上是**本地化**的（"LISTENING" 会变），
    按它过滤会在别的机器上静默失效；而这个 cmdlet 的 ``-State`` 是参数枚举，
    不受界面语言影响。
    """
    return listening_map().get(int(port), [])


def kill_port(port: int, *, tree: bool = True) -> list[int]:
    """停掉占用该端口的进程。

    :param tree: 是否连子进程一起（``/T``）。默认是 —— 前端的 npm 底下那个 node、
        节点托管进程底下那几台，不连根拔掉就会留着端口。

        ★ ``scripts/restart.py`` 对**后端**那一发传 ``False``：它自己就是后端
        进程的子进程（"立刻重启"按钮由后端拉起的），``/T`` 会把整棵树杀掉 ——
        包括它自己，于是"重启完成之后"的步骤永远跑不到，表现是
        **点了立刻重启，然后什么都没发生**。后端没有别的子进程
        （uvicorn 没开 ``--reload``），所以只杀它自己是安全的。
    """
    killed = []
    for pid in listener_pids(port):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else []),
            capture_output=True,
            text=True,
        )
        killed.append(pid)
    return killed


def process_table(name_like: str | None = "%python%") -> list[tuple[int, str]]:
    """**一次**问出进程的 ``(PID, 命令行)``。

    :param name_like: 进程名过滤（WQL 的 ``like``）。``None`` = **全部进程**。
        ★ 为什么得能关掉：:func:`ours_like` 判"这是不是本系统的进程"时
        **必须看得见 node.exe** —— 前端（vite）就是一个 node 进程，
        而它不在 ``%python%`` 里。只查 python 的后果是：停止流程拿不到它的
        命令行，于是把前端当成"别人的程序"**静默跳过** —— 表现为
        「停止」/「重置并启动」停不掉前端（而它还占着 ``logs/frontend.log``）。

    ★ 成批问的理由与 :func:`listening_map` 一模一样：每起一个 PowerShell 进程
      在 Windows 上要 0.5--1 s，逐个问就会卡成"像是死了"。

    它用来找**不在端口上听**的监护进程：节点托管进程 ``run_nodes.py``、
    上一次的启动器 ``start_all.py``。这两个按端口扫**扫不到**，
    而它们不死，端口就还会被重新占上 —— 或者更糟，
    旧启动器会去扫端口，把新起的那套一并收走（见 :data:`RESTART_MARKER`）。
    """
    if name_like:
        ps = (
            f"Get-CimInstance Win32_Process -Filter \"Name like '{name_like}'\" | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
    else:
        ps = (
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        head, _, cmd = line.partition("\t")
        if head.strip().isdigit() and cmd.strip():
            rows.append((int(head), cmd.strip()))
    return rows


def find_by_cmdline(needle: str, *, exclude: set[int] | frozenset[int] = frozenset()) -> list[int]:
    """命令行里含 ``needle`` 的进程 PID（跳过 ``exclude``）。大小写不敏感。"""
    low = needle.lower()
    return [
        pid
        for pid, cmd in process_table()
        if low in cmd.lower() and pid not in exclude
    ]


def kill_by_cmdline(
    needle: str,
    *,
    tree: bool = False,
    exclude: set[int] | frozenset[int] = frozenset(),
) -> list[int]:
    """按命令行关键字收进程。**默认不带 ``/T``** —— 要不要连树由调用方说清楚。

    ★ 为什么不能只用 :func:`kill_port`：它只能收"正在监听某个端口"的进程，
      而 ``run_nodes.py`` / ``start_all.py`` 都不监听任何端口
      （它们只是"看着"子进程）。要收干净这两类，必须按命令行找。
    """
    killed: list[int] = []
    for pid in find_by_cmdline(needle, exclude=exclude):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else []),
            capture_output=True,
            text=True,
        )
        killed.append(pid)
    return killed


def pid_alive(pid: int) -> bool:
    """这个 PID 还活着吗。**微秒级，不起任何子进程** —— 轮询用它。

    ★★ 为什么非有它不可（实测踩到）：``find_by_cmdline`` / ``listening_map``
      每次都要起一个 PowerShell 进程（本机实测 **1.2--2.7 秒**）。把它们写进
      "每隔 0.3 秒看一眼"的循环里，一轮就是一秒多，几十轮下来就是
      **76 秒的静默** —— 窗口停在 ``[1/3]`` 一动不动，看跟卡死一模一样。
      而"某个 PID 还在不在"只要一次 ``OpenProcess``。

    判据用 ``GetExitCodeProcess == STILL_ACTIVE(259)``：
    拿不到句柄（进程没了 / 权限不够）= 当作它已经走了。
    """
    if os.name != "nt":  # pragma: no cover - 本脚本只在 Windows 上用
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False

    import ctypes  # noqa: PLC0415 - Windows 专用，放在这里就不弄脏非 Windows 路径

    process_query_limited_information = 0x1000
    still_active = 259
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = k32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        k32.CloseHandle(handle)


def http_code(url: str, token: str | None = None, timeout: float = 1.5) -> int | None:
    """只取状态码。**401 也算"服务活着"** —— 这一条很关键，后面靠它区分
    "没起来"和"起来了但令牌不对"（混成一句会把排查方向带偏）。"""
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Node-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def node_state(port: int, token: str) -> str:
    """``up`` / ``401`` / ``down`` 三态。"""
    code = http_code(f"http://127.0.0.1:{port}/node/health", token)
    if code == 200:
        return "up"
    if code == 401:
        return "401"
    return "down"


def tail(path: Path, lines: int = 25) -> str:
    """读日志尾巴。

    ★ 编码要**多试一种**。子进程是按它自己那套环境变量决定输出编码的
      （``PYTHONIOENCODING``）；万一有一路没带上它，Python 就按**系统代码页**
      （中文 Windows 是 GBK）写盘，而这里按 UTF-8 读就会读出一片乱码 ——
      有乱码比没日志更坏：它看起来像"日志本身坏了"，而不是"编码不对"。
      所以：先按 UTF-8 严读，不行再退回 GBK。
    """
    if not path.exists():
        return f"（{path.name} 还没有内容）"
    raw = path.read_bytes()
    text: list[str] = []
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc).splitlines()
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace").splitlines()
    return "\n".join(text[-lines:]) if text else f"（{path.name} 是空的）"


def load_token() -> str:
    """令牌：环境变量 > ``nodes/token.txt`` > 现场生成并存下来。"""
    env = os.environ.get("VDS_NODE_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        got = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if got:
            return got
    fresh = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(fresh, encoding="utf-8")
    say(f"  生成节点令牌并记到 {TOKEN_FILE.relative_to(ROOT)}（以后每次都用它）")
    return fresh


# ---------------------------------------------------------------- 各步骤


#: ``--stop`` 扫的端口区间有多宽。
#:
#: ★ 不能用 ``len(NODES)``：``--stop`` 走的是“不提问、用默认台数”那条分支，
#:   而你**上一次可能是** ``--nodes 6`` 起的 —— “起过几台”这件事不写在
#:   任何地方，只写在还活着的进程上。扫一段最省事，
#:   也顺带治住“以为停了、其实 9105/9106 还在跑”（它们占着端口，
#:   还锁着各自的 ``node.db``，紧接着的 ``--reset`` 也动不了）。
STOP_PORT_SPAN = 64


def process_cmdlines() -> dict[int, str]:
    """**一次**问出**全部**进程的 ``PID → 命令行``。

    ★★ 必须包含非 python 进程（所以传 ``None``）：本系统三个角色里，前端是
      **node.exe**（vite），而 :func:`process_table` 默认只查 python ——
      漏了它，:func:`ours_like` 就认不出前端，于是"停止"会**静默跳过我自己的前端**
      （它占着端口与 ``logs/frontend.log``，紧接着的重置就会栽在"文件被占用"上）。
      这一条是实测踩出来的：前端的进程表查不到 ⇒ 停止停不掉 ⇒ 重置报
      ``PermissionError: [WinError 32] logs/frontend.log``。
    """
    return {pid: cmd for pid, cmd in process_table(None)}


#: 本系统进程在命令行里的特征。**认不出来就当不是本系统的，一律不动。**
#:
#: ★ 刻意**不**收 "vite" / "npm" 这种通用词：别的项目在前面那个 5173 上跑一个
#:   vite 是完全可能的事（5173 就是 vite 的默认端口），把它们当自己人就会误杀
#:   —— 而"端口可配"这个功能本来就是冲着"与别人的程序撞上"来的。
#:   前端的进程靠上面那句"命令行里带着本工程路径"认出来就够了：vite 跑的是
#:   ``node <本工程>\frontend\node_modules\vite\bin\vite.js``（npm 那条路也是
#:   跑 ``<本工程>\frontend\node_modules\.bin\vite`` 这个夹子）。
OUR_CMDLINE_MARKERS = (
    "backend.main",  # 后端：python -m uvicorn backend.main:app --port N
    "node_service",  # 存储节点：python -m node_service --node-id node-1 ...
    "start_all.py",  # 启动器（不监听端口，但按命令行收进程时用得上）
    "restart.py",  # 重启器
)


def ours_like(cmdline: str) -> bool:
    """这个进程是不是**本系统**的（按命令行判）。

    ★★ 为什么必须判：端口号人人都可能用（9101 也一样）。不判就 ``taskkill /F``
      会把别人正在跑的东西打掉 —— 而"把端口做成可配"这件事的意义恰恰是
      与别人的程序错开；一边提供这个能力、一边在停止时误杀别人的进程，
      是自相矛盾的（使用者担心的正是"跟别的程序撞上"这件事）。

    ★ 宁可留下一个孤儿进程（看得见、能手动收），也不要错杀一个陌生的进程
      （赔不起）。认不出来时 :func:`stop_everything` 会在控制台说一声。
    """
    low = (cmdline or "").lower()
    if not low:
        return False
    if str(ROOT).lower() in low:  # 命令行里带着本工程路径的，一定是我们的
        return True
    return any(m.lower() in low for m in OUR_CMDLINE_MARKERS)


def node_port(i: int) -> int:
    """第 ``i`` 台（0 基，对应 ``NODES[i]``）的端口。"""
    return int(PORTS.nodes[NODES[i]])


def node_urls_arg() -> str:
    """``node-1=http://127.0.0.1:9101,node-2=http://127.0.0.1:9200``。

    ★ 两个地方要用同一份：给后端/种数据的环境变量 ``VDS_NODE_URLS``，
      以及 ``run_nodes.py --urls``。写成一个函数就不会两边分叉。
    """
    return ",".join(f"{nid}=http://127.0.0.1:{PORTS.nodes[nid]}" for nid in NODES)


def frontend_url() -> str:
    """前端地址（跟着配置走，不再是写死的 5173）。"""
    return f"http://127.0.0.1:{PORTS.frontend}"


def stop_port_list() -> list[int]:
    """停止/等释放时要扫的**全部**端口（去重、升序）。

    三处来源，缺一不可：

    * **部署配置里那一套**（:func:`~backend.config.effective_ports`）：下一轮要用的；
    * **配置里记过的所有节点端口**（含已经被摘掉的那几台留下的记录）：
      台数调小之后，旧那几台还在它们各自的端口上跑着；
    * **``stop_ports``（历史上用过的全部端口）与默认那一段**（8000 / 5173 / 9101-9164）：
      "配置改过了，但跑着的还是上一套"这条路上唯一的线索。

    ★ 为什么不直接扫一段：每台节点的端口现在可以**各自不同**，没有"一段"可扫。
      而默认那一段仍然留着当兜底 —— 老配置（或配置文件被删）时全靠它。
    ★ 真正要不要杀，还要过一遍 :func:`ours_like`（见那里的理由）。
    """
    ports: set[int] = {PORTS.backend, PORTS.frontend, *PORTS.nodes.values()}
    eff, _ = effective_ports()  # 从**部署配置**再读一遍（重启器也会调本函数）
    ports |= {eff.backend, eff.frontend, *eff.nodes.values()}
    ports |= set(read_deploy_ports().all_ports())
    ports |= read_deploy_stop_ports()
    ports |= {BACKEND_PORT, FRONTEND_PORT}
    ports |= {BASE_PORT + i for i in range(STOP_PORT_SPAN)}
    return sorted(p for p in ports if 0 < p <= 65535)


def stop_everything(
    quiet: bool = False,
    *,
    backend_tree: bool = True,
    self_ports: Iterable[int] = (),
) -> None:
    """把后端、前端、以及所有本系统的节点进程停掉。

    :param backend_tree: 后端那一发是否连子进程一起杀。见 :func:`kill_port` ——
        ``scripts/restart.py`` 传 ``False``（它自己挂在后端下面）。
        节点与前端**照旧**用 ``/T``：它们底下确实有子进程要一起收走，
        而且它们都不是"调用者自己的祖先"。
    :param self_ports: **调用者自己所在的那棵进程树**正在听的端口。这些端口一律
        **不**带 ``/T``。

        ★★ 为什么非有它不可（这是"改端口之后重启就没了"的根因，真踩到过）：
          改端口时，配置里写的已经是**新**端口，而正在跑的后端还在**旧**端口上
          （就是调用者自己所在的那棵树！）。于是"按端口号判断要不要 ``/T``"这条
          规则失效 —— 旧端口 != 配置里的后端端口 ⇒ 被当成"别人的端口" ⇒ 带上
          ``/T`` ⇒ 把 ``restart.py`` 自己一起杀掉。表现是
          **点了「立刻重启」，旧的那套停了，新的那套再也没起来**
          （只剩一个窗口一闪而过）。
          所以判据必须与端口号无关：**调用者自己所在的那棵树，绝不 ``/T``**。
    """
    self_set = {int(p) for p in self_ports}
    # ★ 端口表**只查一次**（见 :func:`listening_map` 里的理由）。
    #   原来是 `for p in ports: kill_port(p)`，每个端口都起一个 PowerShell ——
    #   4 个端口时看不出来，扫 65 个端口时就变成卡几分钟。
    table = listening_map()
    # ★★ 只收**本系统的**进程（:func:`ours_like`）：端口号人人都可能用，
    #    不区分就把别人正在跑的东西打掉了 —— 而"端口可配"这个功能的意义
    #    正是与别人的程序错开。进程表**只在真需要判定时才问**（它要起一个
    #    PowerShell，实测 1-2 秒）；一旦问了就复用（见下面那个 ``cmds`` 缓存）。
    cmds: dict[int, str] | None = None
    skipped: list[int] = []
    for p in stop_port_list():
        got = sorted(set(table.get(p, ())))
        if not got:
            continue
        # 规则就两条，顺序不能反：① 调用者自己那棵树绝不 /T；② 其余按旧规则。
        tree = p not in self_set and (backend_tree or p != PORTS.backend)
        killed: list[int] = []
        for pid in got:
            if cmds is None:
                cmds = process_cmdlines()
            if not ours_like(cmds.get(pid, "")):
                # 命令行里认不出本系统的特征 —— 不动它。宁可留个孤儿，
                # 也不要错杀一个陌生进程（那个赔不起）。
                skipped.append(p)
                continue
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else []),
                capture_output=True,
                text=True,
            )
            killed.append(pid)
        if killed and not quiet:
            say(f"  停掉 {p}（PID {', '.join(map(str, killed))}）")
    if skipped and not quiet:
        uniq = ", ".join(str(x) for x in sorted(set(skipped)))
        say(f"  {uniq} 上有别的程序在占着 —— 没有动它（要停它请自行处理）。")
        say("     如果那确实是你以前留下的 VDS 进程，就去任务管理器里结束它，")
        say("     或者换成别的端口（管理员界面 → 设备页 → 端口）。")


def my_tree_ports() -> list[int]:
    """**当前进程自己所在的那棵树**可能正在听的端口（给 :func:`stop_everything` 用）。

    ★ 谁需要它：``scripts/restart.py`` —— 它是**后端进程的子进程**（「立刻重启」
      按钮由后端拉起来的），而它要停的东西里就包括后端自己。判断"哪几个端口上
      的进程是我爹"，不能只看配置：改端口时配置里写的是新端口，跑着的却是旧的。

    三个来源都收进来（宁可多算，绝不 ``/T`` 到自己的祖先）：
    ``VDS_BACKEND_PORT`` / ``VDS_FRONTEND_PORT``（启动器注入给后端、本进程继承的
    **真值**）、配置里那个、以及默认值。
    """
    ports: set[int] = set()
    for raw in (os.environ.get("VDS_BACKEND_PORT", ""), os.environ.get("VDS_FRONTEND_PORT", "")):
        try:
            ports.add(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    ports.add(int(PORTS.backend))
    ports.add(int(read_deploy_ports().backend))
    ports.add(BACKEND_PORT)
    return sorted(p for p in ports if 0 < p <= 65535)


def reset_data() -> list[str]:
    """清协调者库 + 各节点库 + 日志。**令牌文件与部署配置不动**。

    返回**删不掉**的那几个文件名（空列表 = 全清干净了）。

    ★ 节点目录按 ``nodes/node-*`` **通配**清，不是按"当前这几台"。
    理由见文件头 ``--reset 的边界``：上一次可能是 6 台起的，
    按当前台数删就会漏下几个旧目录，而它们会让**下一次启动被启动闸拒掉**。

    ★★ 删不掉时**不能抛异常**：这个函数跑在 ``--reset`` 的中间，一个被占用的
      文件（某个进程还没死透，或者用户自己拿编辑器开着日志）不该让整次重置
      半途而废 —— 那会留下"库清了、节点还在"（或反过来）的分叉，
      而分叉正是启动闸要拦的状态。能删的删，删不掉的报出来由调用方处置。
    """
    stuck: list[str] = []
    for name in ("vds.db", "vds.db-wal", "vds.db-shm"):
        f = ROOT / name
        if not f.exists():
            continue
        try:
            f.unlink()
        except OSError:
            stuck.append(name)
    n_dirs = 0
    nodes_dir = ROOT / "nodes"
    for d in sorted(nodes_dir.glob("node-*")):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            n_dirs += 1
    if LOG_DIR.exists():
        for f in LOG_DIR.glob("*.log"):
            try:
                f.unlink()
            except OSError:
                stuck.append(f.name)
    say(f"  已清空：vds.db、nodes/node-*（{n_dirs} 个节点目录）、logs/*.log")
    if stuck:
        say(f"  这几个没删掉（还被别的进程占着）：{'、'.join(stuck)}")
    return stuck


#: 回环地址一律绕过 HTTP 代理（给子进程用的 ``no_proxy``）。
#:
#: ★ 为什么需要（实机踩到，故障现象极具误导性）：这台机器上开着 Windows
#:   **系统代理**（``HKCU\...\Internet Settings``，被 ProxyBridge / Clash 那类
#:   工具改的就是它）。此时：
#:
#:   * ``urllib``（本脚本探活用它）会查 ``ProxyOverride`` 并**绕过回环** → 一切正常；
#:   * ``httpx``（``node_service/client.py`` 用它）**不做这一步** → 把
#:     ``http://127.0.0.1:9101/node/crs`` 送去那个代理端口 →
#:     ``[WinError 10061] 目标计算机积极拒绝``，而报错里写的是 node-1。
#:
#:   于是一边报“4 台节点就绪”、另一边报“node-1 通信失败”，看起来像启动顺序
#:   或数据坏了，其实全是代理。客户端那一侧已经用 ``trust_env=False`` 关掉了
#:   这条路（那才是正解）；这里再显式设一遍 ``no_proxy``，是为了让子进程里
#:   **任何**别的 HTTP 客户端（含 urllib）也一致绕过 —— 有些机器的
#:   ``ProxyOverride`` 并不包含 127.0.0.1，那时 urllib 也会中招。
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1,0.0.0.0"


def add_loopback_no_proxy(env: dict) -> dict:
    """把回环地址**并进** ``no_proxy``（大小写各一份），保留调用方已有的值。

    ★ 是"并进去"而不是"覆盖"：使用者自己设的 ``no_proxy`` 里可能有别的内网段，
      抹掉它属于越权。这里只做加法 —— 保证回环一定会被绕过，别的原样保留。

    ★ 大小写各写一份：``urllib`` 只认小写那份（``getproxies_environment`` 用小写
      键做 ``no_proxy`` 匹配），``httpx`` 两份都认。干脆都写上，省得记这种细节。
    """
    for key in ("no_proxy", "NO_PROXY"):
        items = [x.strip() for x in str(env.get(key, "") or "").split(",") if x.strip()]
        for host in LOOPBACK_NO_PROXY.split(","):
            if host not in items:
                items.append(host)
        env[key] = ",".join(items)
    return env


def node_env(token: str) -> dict:
    """跨进程模式的环境变量。**两个变量必须一起设**（下面那段理由要看完）。

    ★ 为什么必须在**启动/种数据的时候**就设好：设晚了（或设在别的终端里），
    对方就变成**单进程**模式，而两种模式的库**不能混用** —— 启动闸会拒。
    ★ 为什么 URLS 与 IDS 必须**一起**设、而且列出同样的节点：后端启动时会校
    这一致性（``Settings.check_node_config``）。只设 URLS 不设 IDS 的话，IDS
    会退回它自己的默认值（node-1..node-4），于是 ``--nodes 2`` 会在启动闸
    那里被拒：“node_ids 多出 ['node-3', 'node-4']”。这个坑我实机踩过一次。
    """
    env = os.environ.copy()
    env["VDS_NODE_URLS"] = node_urls_arg()
    env["VDS_NODE_IDS"] = ",".join(NODES)
    env["VDS_NODE_TOKEN"] = token
    # ★★ 告诉后端"你现在跑在哪个端口上"：uvicorn 的 ``--port`` 是命令行参数，
    #    应用里看不到，而管理员界面必须回答得了「现在跑的是 X / 配置是 Y」。
    #    同理把前端端口也告诉它（界面上"现在跑的是"那一行要用）。
    env["VDS_BACKEND_PORT"] = str(PORTS.backend)
    env["VDS_FRONTEND_PORT"] = str(PORTS.frontend)
    # ★ 回环不走代理（理由见 LOOPBACK_NO_PROXY）。
    add_loopback_no_proxy(env)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONWARNINGS"] = "ignore"
    return env


def seed_accounts(with_demo: bool, token: str) -> int:
    """建账号（可选：连 5 个演示文件一起）。**默认不带演示文件** —— 文件由你自己选。

    ★★ 为什么要把 ``token`` 传进来、而且必须**在节点起来之后**才跑：
    ``scripts/seed.py`` 是按 ``VDS_NODE_URLS`` 决定单进程还是跨进程的。
    不给它这套环境变量时它是**单进程** —— 节点状态会被写进**协调者库**；
    而一键启动起的是跨进程模式，两者不能混用，后端会直接拒绝启动：
    “库里还留着节点状态，但当前跑的是跨进程模式”。所以这里必须把与后端
    **同一套**环境变量传过去，让演示文件真的推到那几个节点进程上。
    （这不是考据 —— 是真的踩过：``--reset --demo`` 必炸在后端启动那一步。）
    """
    cmd = [PY, str(ROOT / "scripts" / "seed.py"), "--reset"]
    if with_demo:
        cmd.append("--demo")
    r = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=node_env(token),
        capture_output=True,
        text=True,
        # ★ 必须显式指定 utf-8：子进程是按 ``PYTHONIOENCODING=utf-8`` 跑的
        #   （node_env 里设的），而父进程默认按**系统代码页**（中文 Windows 是 GBK）
        #   解码 —— 子进程一打印中文，捕获线程就抛 UnicodeDecodeError。
        #   那个异常在后台线程里，**不会**影响 returncode，所以只在日志里留一段
        #   吓人的栈；但它会让人以为启动失败了（我这次就被它误导了一轮）。
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if r.returncode != 0:
        say("  建账号失败：")
        say("  " + (r.stdout or "").strip()[-1500:])
        say("  " + (r.stderr or "").strip()[-1500:])
    return r.returncode


def node_ports_hint() -> str:
    """节点端口的紧凑写法：连号时是 ``9101-9104``，散着时逐个列。

    ★ 端口现在可以每台都不一样，所以不能再假定"一段区间"。
    """
    vals = [int(PORTS.nodes[n]) for n in NODES]
    if len(vals) > 1 and vals == list(range(vals[0], vals[0] + len(vals))):
        return f"{vals[0]}-{vals[-1]}"
    return "、".join(str(v) for v in vals)


def ensure_nodes(token: str) -> subprocess.Popen | None:
    """确保所有节点活着且用**这一份**令牌。已就绲就返回 None。"""
    states = {i: node_state(node_port(i), token) for i in range(len(NODES))}
    if all(s == "up" for s in states.values()):
        say(f"  {len(NODES)} 台节点都在（端口 {node_ports_hint()}），跳过")
        return None

    # 不是“全好”就**全部重启**（数据目录不动，所以数据不丢）：
    # 只补缺的那几台的话，还活着的台会占着端口，新起的进程绑不上 ——
    # 而 os 报“端口被占”时看起来像代码 bug，不像“上次没停干净”。
    bad = [i for i, s in states.items() if s == "401"]
    if bad:
        say(
            "  有节点活着但令牌不对（端口 "
            + "、".join(str(node_port(i)) for i in bad)
            + "）—— 重启成这份令牌（数据保留）"
        )
    else:
        say("  节点没起齐 —— 全部重启（数据保留）")
    for i in range(len(NODES)):
        kill_port(node_port(i))
    time.sleep(1.0)

    # 用 run_nodes.py 起，而不是在这里手写 4 次 Popen —— 等活、收子进程那套逻辑
    # 已经在那儿了，重复写一遍迟早会分叉。
    # ★ 传 ``--urls`` 而不是 ``--nodes`` + ``--base-port``：
    #   端口可以每台都不一样（管理员界面上就是一个个填的），而 run_nodes 起进程时
    #   用的端口本来就是从 URL 里取出来的（见那边的 ``url.rsplit``）。
    cmd = [
        PY,
        str(ROOT / "scripts" / "run_nodes.py"),
        "--urls",
        node_urls_arg(),
        "--token",
        token,
    ]
    say(f"  起 {len(NODES)} 台节点…")
    # ★ 把节点组的输出**落到文件**而不是丢进 DEVNULL：
    #   丢了的话，“某一台为什么死了”就永远找不回来了。
    #   （那个子进程平时很安静，出问题时它的报错就是唯一线索。）
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    node_log = (LOG_DIR / "nodes.log").open("wb")
    # ★ 必须把**与后端同一套**环境变量传下去（特别是 PYTHONIOENCODING=utf-8）：
    #   不传的话子进程按系统代码页（中文 Windows 是 GBK）写日志，
    #   而 tail() 默认按 UTF-8 读 —— 报出来就是一片乱码。实测踩到过。
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=node_env(token),
        stdout=node_log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        if all(node_state(node_port(i), token) == "up" for i in range(len(NODES))):
            say(f"  {len(NODES)} 台节点就绪（端口 {node_ports_hint()}）")
            return proc
        if proc.poll() is not None:
            say("  节点进程提前退了 —— 单独跑一次看看：")
            say(f"    python scripts/run_nodes.py --token {token}")
            return None
        time.sleep(0.4)
    say("  40 秒内没起齐，放弃。")
    proc.terminate()
    return None


def start_backend(token: str) -> subprocess.Popen | None:
    if alive(PORTS.backend):
        say(f"  后端已经在 {PORTS.backend} 上，跳过")
        return None
    env = node_env(token)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / "backend.log").open("wb")
    say("  起后端…（日志 logs/backend.log）")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.main:app", "--port", str(PORTS.backend)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        # 200 或 401 都算活着（401 = 服务起来了，只是没带令牌）
        if http_code(f"http://127.0.0.1:{PORTS.backend}/api/status") in (200, 401):
            say(f"  后端就绪（{PORTS.backend}，跨进程模式）")
            return proc
        if proc.poll() is not None:
            say("  后端启动失败，日志尾巴：")
            say(tail(LOG_DIR / "backend.log", 20))
            return None
        time.sleep(0.4)
    say("  后端 90 秒内没就绪，日志尾巴：")
    say(tail(LOG_DIR / "backend.log", 20))
    return proc


def frontend_command() -> list[str] | None:
    """优先直接跑 vite（这样只剩一个 node 进程，好管也好停）；
    没有 node_modules/vite 才退回 ``npm run dev``。

    ★ 端口一律用 ``--port`` **显式传**：vite.config.js 里那个 5173 只是
      "手动跑 npm run dev"时的默认值，而以本脚本起的一律按部署配置来。
    """
    vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    node = shutil.which("node")
    if vite.exists() and node:
        return [node, str(vite), "--port", str(PORTS.frontend)]
    npm = shutil.which("npm")
    if npm:
        # npm 那条路要用 ``--`` 才能把参数转给 vite。
        return ["cmd", "/c", npm, "run", "dev", "--", "--port", str(PORTS.frontend)]
    return None


def start_frontend() -> subprocess.Popen | None:
    if alive(PORTS.frontend):
        say(f"  前端已经在 {PORTS.frontend} 上，跳过")
        return None
    frontend = ROOT / "frontend"
    if not (frontend / "node_modules").exists():
        npm = shutil.which("npm")
        if not npm:
            say("  找不到 npm —— 前端起不来。装好 Node 后先跑一次 `npm install`。")
            return None
        say("  第一次跑，先 npm install（可能要一两分钟）…")
        r = subprocess.run(
            ["cmd", "/c", npm, "install"], cwd=str(frontend), capture_output=True
        )
        if r.returncode != 0:
            say("  npm install 失败，日志尾巴：")
            say((r.stdout or b"").decode("utf-8", "replace")[-1200:])
            return None

    cmd = frontend_command()
    if cmd is None:
        say("  找不到 node/npm —— 前端起不来。")
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / "frontend.log").open("wb")
    say("  起前端…（日志 logs/frontend.log）")
    # ★★ 前端到后端的地址必须**跟着配置走**：vite 的 proxy 默认打 8000
    #    （见 frontend/vite.config.js 里的 VDS_API_TARGET），后端端口一改，
    #    不把新地址告诉它就会变成"页面能开、每个请求都失败"——
    #    而那个现象看起来像后端坏了，不像前端 proxy 指错地方了。
    env = os.environ.copy()
    env["VDS_API_TARGET"] = f"http://127.0.0.1:{PORTS.backend}"
    env["VDS_FRONTEND_PORT"] = str(PORTS.frontend)
    proc = subprocess.Popen(
        cmd, cwd=str(frontend), env=env, stdout=log, stderr=subprocess.STDOUT
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if alive(PORTS.frontend):
            say(f"  前端就绪（{frontend_url()}）")
            return proc
        if proc.poll() is not None:
            say("  前端启动失败，日志尾巴：")
            say(tail(LOG_DIR / "frontend.log", 20))
            return None
        time.sleep(0.4)
    say("  前端 60 秒内没就绪，日志尾巴：")
    say(tail(LOG_DIR / "frontend.log", 20))
    return proc


def banner() -> None:
    say()
    say("=" * 62)
    say(f"  打开： {frontend_url()}")
    say()
    say(f"  存储节点 {len(NODES)} 台（端口 {node_ports_hint()}）")
    say(f"  后端 {PORTS.backend} / 前端 {PORTS.frontend}")
    say("  改台数与端口：管理员界面 → 设备（存储节点）→ 服务器台数 / 端口 → 「立刻重启」")
    say()
    say(f"  演示账号（口令统一 {DEMO_PASSWORD}）：")
    for username, role, display in USERS:
        tag = "管理员（能看审计与用户管理）" if role == "admin" else "普通用户"
        say(f"    {username:10s} {display:6s} {tag}")
    say()
    say("  登录页不列演示账号：用上面这些账号 + 统一口令登录。")
    say("  默认没有任何预设文件 —— 「文件与块」页里自己选文件、自己决定切成几块。")
    say()
    say("  日志： logs/backend.log、logs/frontend.log")
    say("  停止： 在这个窗口按 Ctrl+C（或者 python scripts/start_all.py --stop）")
    say("=" * 62)
    say()


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    global NODES, PORTS  # noqa: PLW0603 - 全脚本只此两处改它，见文件头注释

    _force_utf8_console()
    # ★ 回环不走代理。这一句管的是**本进程**的探活（``node_state`` / ``http_code``
    #   走 urllib，靠 Windows 的 ``ProxyOverride`` 绕回环；但那项设置是**机器上
    #   可变的**，有的机器不含 127.0.0.1）。:func:`node_env` 那一份管子进程。
    #   两边都要设 —— 这正是那次故障的教训：一边绕过、一边没绕过，
    #   于是"一边报节点就绪、一边报 node-1 连不上"，看着像启动顺序的问题。
    add_loopback_no_proxy(os.environ)
    ap = argparse.ArgumentParser(description="一键启动 VDS 演示环境")
    ap.add_argument("--reset", action="store_true", help="清空库与节点数据（账号保留重建）")
    ap.add_argument("--demo", action="store_true", help="顺带灌 5 个演示文件（默认不灌）")
    ap.add_argument("--stop", action="store_true", help="只把节点/后端/前端停掉")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="只要有任何一样退出就整体停下（默认：节点掉线只在控制台报警，"
        "后端与前端继续跑 —— 这样才演得了“kill 一台看降级”）",
    )
    ap.add_argument(
        "--nodes",
        type=int,
        default=None,
        help=f"起几台存储节点（{MIN_NODE_COUNT}..{MAX_NODE_COUNT}）。"
        "不传就读部署配置 nodes/deploy.json（管理员界面上改的就是它），"
        "文件不存在时用默认值。传了会**写回**配置 —— 免得命令行改完界面还显示旧值",
    )
    args = ap.parse_args(argv)

    # 清理上一次「立刻重启」可能留下的交接标记（见 :data:`RESTART_MARKER`）。
    # 正常路径上重启器在起新栈之前已经删过了；这里兵底是为了
    # “重启器半路挂了”那种情况 —— 否则一个过期标记会让**下一次**启动
    # 在子进程退出时报“交棒”，把真正的原因（后端崩了）盖掉。
    if RESTART_MARKER.exists():
        RESTART_MARKER.unlink(missing_ok=True)
        say("  清掉上一次留下的重启交接标记（重启器没走完？）")

    # ★★ 台数从哪来（需求 #2）。**这里不再提问** —— 一键启动与重置并启动
    #   都是"直接启动"，台数只有管理员界面一个改动入口（写的就是下面这个文件）。
    #
    #   三条规则（文件头「台数从哪来」那张表）：
    #     * --nodes N          → 用 N，并写回配置；
    #     * --reset（没给 N）   → 台数**复位成 4**，再按 4 台起；
    #     * 都没有             → 读配置。
    #
    #   ★ 顺序：复位必须发生在"读配置"**之前**，否则重置之后还是按旧台数起。
    if args.reset and args.nodes is None:
        was = read_deploy_node_count()
        reset_deploy_node_count()
        if was != DEFAULT_NODE_COUNT:
            say(
                f"  重置：服务器台数 {was} → {DEFAULT_NODE_COUNT} 台"
                f"（部署配置已复位）"
            )

    explicit_nodes = args.nodes is not None
    count = args.nodes if explicit_nodes else read_deploy_node_count()
    count = clamp_node_count(count)
    if explicit_nodes and count != args.nodes:
        say(
            f"  --nodes {args.nodes} 超出台数范围 —— 取 {count} 台"
            f"（{MIN_NODE_COUNT}..{MAX_NODE_COUNT}）"
        )
    if count != len(NODES):
        NODES = tuple(f"node-{i}" for i in range(1, count + 1))
    if explicit_nodes and count != read_deploy_node_count(default=-1):
        # 命令行是显式覆盖：写回配置，让管理员界面显示的就是你刚起的那几台。
        #
        # ★ 台数**没变**时一个字都不写。``write_deploy_node_count(..., prev=None)``
        #   的语义是“这次不是一次改动”，它会顺手把上一次的记录（``prev_count``
        #   与那句“服务器数量减少，文件块已重新分配”）一起抹掉。
        #   而「立刻重启」正是用 ``--nodes N`` 把控制权交回本脚本的 ——
        #   重启一完成就把那句话抹掉，恰好抹在用户最想看它的时刻（重启之后）。
        write_deploy_node_count(count, prev=None)

    # ★★ 端口从哪里来（与台数同一个文件、同一个模型）：
    #    读 ``nodes/deploy.json`` 的 ``ports``，缺的按规则补全。
    #    ``--nodes`` 改了台数时，新增的那几台也会在这里拿到各自默认端口。
    #    ★ 改了就必须说出来：手改坏过的配置会被"修好"，但静默改配置比报错更难查。
    PORTS, port_notes = effective_ports(NODES)
    for note in port_notes:
        say(f"  [端口] {note}（部署配置里那个值用不了）")

    if args.stop:
        say("停掉节点 / 后端 / 前端：")
        stop_everything()
        say("完事。")
        return 0

    if os.name != "nt":
        say("这个脚本是按 Windows 写的（用 PowerShell 找监听进程）。")
        return 2

    say("=" * 62)
    say("  VDS 一键启动")
    say("=" * 62)

    token = load_token()

    if args.reset:
        say("\n[0/4] 重置")
        # ★★ 顺序不能反：**先**按当前配置（也就是还在跑的那一套）停干净，
        #    **再**把端口复位。反过来停的就是默认端口，而进程还在自定义端口上
        #    —— 一个都停不掉，全变成占着端口的孤儿进程。
        stop_everything(quiet=True)
        time.sleep(1.0)
        reset_deploy_ports(NODES)
        PORTS = effective_ports(NODES)[0]
        say(f"  端口已复位成默认值（后端 {PORTS.backend} / 前端 {PORTS.frontend} / "
            f"节点 {node_ports_hint()}）")
        reset_data()
        # ★ 库没清掉就别起了：那会拿着旧数据接着跑，与"重置"不是一个意思
        #   （而且节点目录已被清，两边正好对不上，启动闸一定会拒）。
        #   停下来把原因说清楚，好过去撞一个看不懂的启动错误。
        if (ROOT / "vds.db").exists():
            say("  库还在（被占用，没删掉）—— 先把占着它的进程停掉，再跑一次重置。")
            say("  这次不启动：否则会拿着旧数据接着跑，而节点数据已经清过了，两边对不上。")
            return 1

    say("\n[1/5] 库的清理判定")
    got_db = (ROOT / "vds.db").exists()
    need_seed = args.reset or not got_db
    if got_db and not args.reset:
        say("  vds.db 已存在 —— 账号不动（要重来加 --reset）")
    elif not got_db and not args.reset:
        # 库没了、节点库还在 —— 那种状态两边不可能对得上（登记表在协调者这边）。
        # 所以一并清掉，否则启动闸会拒掉（“节点与协调者不同步”）。
        # （走了 --reset 的话上面已经清过了，不必再清一次、也不必再报一次。）
        say("  没有 vds.db —— 节点数据也一并清掉（否则两边对不上）")
        reset_data()

    say(f"\n[2/5] 存储节点（{len(NODES)} 台）")
    nodes_proc = ensure_nodes(token)
    if nodes_proc is None and not all(
        node_state(node_port(i), token) == "up" for i in range(len(NODES))
    ):
        return 1

    say("\n[3/5] 账号")
    if not need_seed:
        say("  账号不动（库还在）")
    elif seed_accounts(args.demo, token) != 0:
        return 1
    else:
        suffix = "（含 5 个演示文件，已经真的推到节点上）" if args.demo else "（没有预设文件）"
        say("  账号已建" + suffix)

    say("\n[4/5] 后端")
    backend_proc = start_backend(token)
    if backend_proc is None and not alive(PORTS.backend):
        return 1

    say("\n[5/5] 前端")
    frontend_proc = start_frontend()

    if not args.no_browser and alive(PORTS.frontend):
        webbrowser.open(frontend_url())

    banner()

    kids = [p for p in (nodes_proc, backend_proc, frontend_proc) if p is not None]
    if not kids:
        say("三样都已经在跑了 —— 这个窗口可以直接关掉。")
        return 0

    # ★★ 谁死了才算“整体必须停”，要分开看（#10）。
    #
    #   **后端或前端**死了：界面本来就没了，继续守着没有意义 —— 停下全部，
    #   并说清是**哪一样**、为什么（原来只说“有进程退出了”，然后把
    #   backend/frontend 两份日志尾巴都打一遍，读者得自己猜是哪一样）。
    #
    #   **节点那一组**死了：那正是“掉一台”要演示的东西，不该把浏览器一起关掉。
    #   默认只在控制台报警、其余进程照跑（--strict 可恢复旧行为）。
    #
    #   原来的行为：kids 里任何一个 poll() 非空 → return 1 → finally 里 terminate
    #   其余全部、并按端口 kill 9101-9104。而 4 台节点是 run_nodes.py **一个**
    #   子进程托管的，所以实测“kill 掉 node-1”→ 8000 / 5173 / 9102-9104
    #   全部关闭，页面所有请求变成 ERR_CONNECTION_REFUSED ——
    #   不是降级，是整体下线。
    fatal_kids = [
        (name, p)
        for name, p in (("后端", backend_proc), ("前端", frontend_proc))
        if p is not None
    ]
    nodes_kid = ("存储节点组", nodes_proc) if nodes_proc is not None else None

    say("按 Ctrl+C 停掉本次启动的进程。")
    if nodes_kid is not None:
        if args.strict:
            say("  （--strict：只要有一台节点退出，就整体停下）")
        else:
            say("  （节点掉线不会带走后端与前端 —— 可以直接 kill 一台演示降级；")
            say("    想恢复旧行为（一有退出就整体停下）加 --strict）")

    nodes_warned = False
    #: 是不是把控制权交给了「立刻重启」（见 :data:`RESTART_MARKER`）。
    #: 它决定 finally 里**能不能**收尾 —— 交棒了就不能。
    handed_over = False

    # ★★ 光看 ``nodes_kid.poll()`` 是**看不出“某一台掉了”**的：
    #   4 台节点是 run_nodes.py **一个**子进程托管的，死一台它自己不会退出
    #   （那是它刻意的容错行为，见那边的循环），而它的输出被写进了
    #   logs/nodes.log —— 于是"掉了一台"在启动器窗口里**一个字都不显示**。
    #   而报告里要的恰恰是“前者（节点掉了）只在控制台报警”。
    #   所以这里自己按端口探活，把 up/down 的**变化**报出来（只报变化，不刷屏）。
    node_seen: dict[int, str] = {i: "up" for i in range(len(NODES))}
    POLL_EVERY = 5.0
    next_probe = time.time() + POLL_EVERY

    try:
        while True:
            time.sleep(0.5)
            for name, p in fatal_kids:
                if p.poll() is not None:
                    if RESTART_MARKER.exists():
                        # ★★ 「立刻重启」正在接管（见 RESTART_MARKER）。
                        #   这里**必须什么都别做**：不报错，而且 finally 里也不能扫端口 ——
                        #   重启器马上就要把新的一套起来（同样的 9101.. 区间），
                        #   扫端口会把它们一并收走，表现就是“重启后新的起来了又没了”。
                        #
                        #   退出码 **0** 是刻意的：一键启动.cmd 收到 0 会直接关掉
                        #   它那个窗口 —— 用户不必再去关一个已经交棒的旧控制台。
                        say(f"\n{name}已交给「立刻重启」（检测到交接标记）——")
                        say("  本次启动到此交棒：不在这个窗口里收尾（收尾由重启器做）。")
                        say("  这个窗口可以关了，新的控制台在重启器那个窗口里。")
                        handed_over = True
                        return 0
                    log_name = "backend.log" if name == "后端" else "frontend.log"
                    say(
                        f"\n{name}退出了（退出码 {p.returncode}）——"
                        f"界面已经不可用，停下全部。{log_name} 尾巴："
                    )
                    say(tail(LOG_DIR / log_name, 15))
                    return 1
            if nodes_kid is not None:
                name, p = nodes_kid
                if p.poll() is not None:
                    if args.strict:
                        say(
                            f"\n{name}退出了（退出码 {p.returncode}）——"
                            "--strict 下整体停下。"
                        )
                        return 1
                    if not nodes_warned:
                        nodes_warned = True
                        say(
                            f"\n[注意] {name}退出了（退出码 {p.returncode}）。"
                            "后端与前端继续跑 ——"
                        )
                        say(
                            "       页面上这些节点会被标成掉线（／存储节点页里显示“连不上”），"
                            "还有副本的块照样读得到；"
                        )
                        say("       写操作会被拒（因为写要求全部在线）。")
                        say(
                            "       想看降级效果就直接刷页面；要恢复节点，"
                            "Ctrl+C 停掉全部后重新启动即可（数据不丢）。"
                        )

            # 逐台探活：只报“变化”，不然每 5 秒刷一行会把有用的信息淹掉
            if time.time() >= next_probe:
                next_probe = time.time() + POLL_EVERY
                for i in range(len(NODES)):
                    st = node_state(node_port(i), token)
                    was = node_seen.get(i, "up")
                    if st != "up" and was == "up":
                        why = "令牌对不上（401）" if st == "401" else "连不上"
                        say(
                            f"\n[注意] {NODES[i]}（端口 {node_port(i)}）{why} ——"
                            "后端与前端继续跑，页面上它会显示为掉线。"
                        )
                        say(
                            "       还有副本的块照样读得到（读取会自动改问副本）；"
                            "写操作会被拒，因为写要求全部在线。"
                        )
                        say(f"       节点组日志尾巴（logs/nodes.log）：")
                        say(tail(LOG_DIR / "nodes.log", 5))
                    elif st == "up" and was != "up":
                        say(f"\n[恢复] {NODES[i]}（端口 {node_port(i)}）又答话了。")
                    node_seen[i] = st
    except KeyboardInterrupt:
        say("\n收工，停掉本次启动的进程…")
    finally:
        if handed_over:
            # 交棒：收尾归重启器（它停干净之后自己会再按端口扫一遍），
            # 这里一件都不能做 —— 尤其**不能**扫端口（会把新节点收走）。
            say("（本窗口不做收尾：重启器负责停干净并重建新的一套。）")
        else:
            for p in kids:
                if p.poll() is None:
                    p.terminate()
            for p in kids:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    p.kill()
            # ★ Windows 上结束父进程**不会**带走子进程（没有 Job Object 那回事）。
            #   ``run_nodes.py`` 自己会收它的 4 个子进程，但它被 terminate 时收不了 ——
            #   所以在带宽内部再按端口扫一遍，别留 4 个孤儿占着 9101-9104。
            if nodes_proc is not None:
                for i in range(len(NODES)):
                    kill_port(node_port(i))
    say("已停。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
