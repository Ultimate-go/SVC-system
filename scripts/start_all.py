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

**默认不带演示文件** —— 文件、切成几块、怎么切，都由你在界面上自己选。

幂等
----
已经在跑的东西**不会再起一个**：脚本先探活（节点 9101-9104 / 后端 8000 /
前端 5173），活的就跳过。所以"点两下"是安全的。

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = sys.executable
#: 默认节点名。``--nodes N`` 会把它换成 node-1..node-N（全脚本共用这一份）。
#: ★ 后端与前端**不写死台数** —— 后端从 ``VDS_NODE_URLS`` 读，前端从 ``/api/nodes`` 取，
#:   所以改这里就够了。
NODES: tuple[str, ...] = ("node-1", "node-2", "node-3", "node-4")
#: ``--nodes`` 的默认值。单独抽出来，因为 argparse 在 main() 里读它时
#: 那句 ``global NODES`` 还没生效（用了会 SyntaxError）。
DEFAULT_NODE_COUNT = len(NODES)
BASE_PORT = 9101
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
URL = f"http://127.0.0.1:{FRONTEND_PORT}"
TOKEN_FILE = ROOT / "nodes" / "token.txt"
LOG_DIR = ROOT / "logs"

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


def kill_port(port: int) -> list[int]:
    """停掉占用该端口的进程树（``/T`` 连子进程一起，否则 npm 底下那个 node 会留下）。"""
    killed = []
    for pid in listener_pids(port):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True
        )
        killed.append(pid)
    return killed


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


def _ask_node_count() -> int | None:
    """双击启动时问一句“起几台”。回车 = 默认；``q`` = 放弃。

    返回 ``None`` 表示使用者放弃（调用方直接退 0 —— 那不是失败，别报错）。

    ★★ 为什么这段逻辑在这里、而不在 ``一键启动.cmd`` 里：
    cmd.exe 在 ``chcp 65001`` 下读含**多字节字符**的 .cmd 会**丢失行位置** ——
    它会把一个 ``rem`` 注释劈成两半、把后半段当命令去执行。实测双击时报的是

        '的延迟展开写法（set' is not recognized as an internal or external command

    然后 cmd 从错位的地方接着跑（流程还能走完，但那两行错误会一直刷在屏幕上，
    而且下次未必这么幸运）。所以：**.cmd 保持纯 ASCII**，中文交互一律交给 Python。
    """
    say()
    say(f"  起几台存储节点？（直接回车 = {DEFAULT_NODE_COUNT} 台）")
    say("    2 台 —— 最小可跑：每块要存 2 份副本，而副本必须落在不同的机器上")
    say(f"    {DEFAULT_NODE_COUNT} 台 —— 答辩默认，块会摊在这几台上")
    say("    6 台及以上 —— 演示“加机器也不会把老块搬过去，新块才开始用上新机器”")
    say("  注：改台数后新节点上暂时没有块（旧块仍在原来那几台）—— 想干净重来请配 --reset")
    try:
        raw = input(f"  台数（回车 = {DEFAULT_NODE_COUNT}，q = 取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("  （没读到输入）—— 用默认值")
        return DEFAULT_NODE_COUNT
    if raw in ("q", "Q", "quit", "exit"):
        return None
    if not raw:
        return DEFAULT_NODE_COUNT
    try:
        n = int(raw)
    except ValueError:
        # 从宽处理：启动器不该因为多打了一个字就罢工
        say(f"  “{raw}”不是数字 —— 用默认值 {DEFAULT_NODE_COUNT}")
        return DEFAULT_NODE_COUNT
    if n < 1:
        say(f"  台数至少是 1 —— 用默认值 {DEFAULT_NODE_COUNT}")
        return DEFAULT_NODE_COUNT
    return n


# ---------------------------------------------------------------- 各步骤


#: ``--stop`` 扫的端口区间有多宽。
#:
#: ★ 不能用 ``len(NODES)``：``--stop`` 走的是“不提问、用默认台数”那条分支，
#:   而你**上一次可能是** ``--nodes 6`` 起的 —— “起过几台”这件事不写在
#:   任何地方，只写在还活着的进程上。扫一段最省事，
#:   也顺带治住“以为停了、其实 9105/9106 还在跑”（它们占着端口，
#:   还锁着各自的 ``node.db``，紧接着的 ``--reset`` 也动不了）。
STOP_PORT_SPAN = 64


def stop_everything(quiet: bool = False) -> None:
    # ★ 端口表**只查一次**（见 :func:`listening_map` 里的理由）。
    #   原来是 `for p in ports: kill_port(p)`，每个端口都起一个 PowerShell ——
    #   4 个端口时看不出来，扫 65 个端口时就变成卡几分钟。
    table = listening_map()
    for p in [BACKEND_PORT, FRONTEND_PORT] + [
        BASE_PORT + i for i in range(STOP_PORT_SPAN)
    ]:
        got = sorted(set(table.get(p, ())))
        for pid in got:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True,
                text=True,
            )
        if got and not quiet:
            say(f"  停掉 {p}（PID {', '.join(map(str, got))}）")


def reset_data() -> None:
    """清协调者库 + 各节点库 + 日志。**令牌文件不动**（省得每次都要重配）。"""
    for name in ("vds.db", "vds.db-wal", "vds.db-shm"):
        f = ROOT / name
        if f.exists():
            f.unlink()
    for nid in NODES:
        shutil.rmtree(ROOT / "nodes" / nid, ignore_errors=True)
    if LOG_DIR.exists():
        for f in LOG_DIR.glob("*.log"):
            f.unlink()
    say("  已清空：vds.db、nodes/node-*、logs/*.log")


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
    env["VDS_NODE_URLS"] = ",".join(
        f"{nid}=http://127.0.0.1:{BASE_PORT + i}" for i, nid in enumerate(NODES)
    )
    env["VDS_NODE_IDS"] = ",".join(NODES)
    env["VDS_NODE_TOKEN"] = token
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


def ensure_nodes(token: str) -> subprocess.Popen | None:
    """确保所有节点活着且用**这一份**令牌。已就绲就返回 None。"""
    states = {i: node_state(BASE_PORT + i, token) for i in range(len(NODES))}
    if all(s == "up" for s in states.values()):
        say(f"  {len(NODES)} 台节点都在（{BASE_PORT}-{BASE_PORT + len(NODES) - 1}），跳过")
        return None

    # 不是“全好”就**全部重启**（数据目录不动，所以数据不丢）：
    # 只补缺的那几台的话，还活着的台会占着端口，新起的进程绑不上 ——
    # 而 os 报“端口被占”时看起来像代码 bug，不像“上次没停干净”。
    bad = [i for i, s in states.items() if s == "401"]
    if bad:
        say(
            "  有节点活着但令牌不对（端口 "
            + ", ".join(str(BASE_PORT + i) for i in bad)
            + "）—— 重启成这份令牌（数据保留）"
        )
    else:
        say("  节点没起齐 —— 全部重启（数据保留）")
    for i in range(len(NODES)):
        kill_port(BASE_PORT + i)
    time.sleep(1.0)

    # 用 run_nodes.py 起，而不是在这里手写 4 次 Popen —— 等活、收子进程那套逻辑
    # 已经在那儿了，重复写一遍迟早会分叉。
    cmd = [
        PY,
        str(ROOT / "scripts" / "run_nodes.py"),
        "--nodes", ",".join(NODES),
        "--base-port", str(BASE_PORT),
        "--token", token,
    ]
    say("  起 4 台节点…")
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
        if all(node_state(BASE_PORT + i, token) == "up" for i in range(len(NODES))):
            say(f"  {len(NODES)} 台节点就绪（{BASE_PORT}-{BASE_PORT + len(NODES) - 1}）")
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
    if alive(BACKEND_PORT):
        say(f"  后端已经在 {BACKEND_PORT} 上，跳过")
        return None
    env = node_env(token)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / "backend.log").open("wb")
    say("  起后端…（日志 logs/backend.log）")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.main:app", "--port", str(BACKEND_PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        # 200 或 401 都算活着（401 = 服务起来了，只是没带令牌）
        if http_code(f"http://127.0.0.1:{BACKEND_PORT}/api/status") in (200, 401):
            say(f"  后端就绪（{BACKEND_PORT}，跨进程模式）")
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
    没有 node_modules/vite 才退回 ``npm run dev``。"""
    vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    node = shutil.which("node")
    if vite.exists() and node:
        return [node, str(vite)]
    npm = shutil.which("npm")
    if npm:
        return ["cmd", "/c", npm, "run", "dev"]
    return None


def start_frontend() -> subprocess.Popen | None:
    if alive(FRONTEND_PORT):
        say(f"  前端已经在 {FRONTEND_PORT} 上，跳过")
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
    proc = subprocess.Popen(
        cmd, cwd=str(frontend), stdout=log, stderr=subprocess.STDOUT
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if alive(FRONTEND_PORT):
            say(f"  前端就绪（{URL}）")
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
    say(f"  打开： {URL}")
    say()
    say(f"  演示账号（口令统一 {DEMO_PASSWORD}）：")
    for username, role, display in USERS:
        tag = "管理员（能看审计与用户管理）" if role == "admin" else "普通用户"
        say(f"    {username:10s} {display:6s} {tag}")
    say()
    say("  登录页下方有这 5 个按钮，点一下就自动填好账号与口令。")
    say("  默认没有任何预设文件 —— 「文件与块」页里自己选文件、自己决定切成几块。")
    say()
    say("  日志： logs/backend.log、logs/frontend.log")
    say("  停止： 在这个窗口按 Ctrl+C（或者 python scripts/start_all.py --stop）")
    say("=" * 62)
    say()


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    global NODES  # noqa: PLW0603 - 全脚本只此一处改它，见文件头 NODES 的注释

    _force_utf8_console()
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
        help="起几台存储节点（不传且是双击/真控制台时，启动器会问你一句）。"
        "改台数意味着无块可用的空节点会加入，旧块仍然只在原来那几台上 ——"
        "所以改台数后建议 --reset",
    )
    args = ap.parse_args(argv)

    if args.nodes is not None and args.nodes < 1:
        say("--nodes 至少是 1")
        return 2

    # ★ 台数由使用者决定（需求 #2）——问话放这里而不是 .cmd 里，理由见 _ask_node_count。
    #
    #   只在“没给 --nodes”**且**“stdin 是真控制台”时才问：
    #   双击 .cmd 满足后者；管道 / 重定向 / 被别的脚本调用都不满足，
    #   于是它们不会被一个没人回答的提问卡住（直接按默认值走）。
    count = args.nodes
    if count is None:
        if args.stop or not (sys.stdin and sys.stdin.isatty()):
            count = DEFAULT_NODE_COUNT
        else:
            asked = _ask_node_count()
            if asked is None:
                say("好，什么都不做。")
                return 0
            count = asked

    if count < 1:
        say("节点台数至少是 1")
        return 2
    if count != len(NODES):
        NODES = tuple(f"node-{i}" for i in range(1, count + 1))

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
        stop_everything(quiet=True)
        time.sleep(1.0)
        reset_data()

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

    say("\n[2/5] 存储节点")
    nodes_proc = ensure_nodes(token)
    if nodes_proc is None and not all(
        node_state(BASE_PORT + i, token) == "up" for i in range(len(NODES))
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
    if backend_proc is None and not alive(BACKEND_PORT):
        return 1

    say("\n[5/5] 前端")
    frontend_proc = start_frontend()

    if not args.no_browser and alive(FRONTEND_PORT):
        webbrowser.open(URL)

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
                    st = node_state(BASE_PORT + i, token)
                    was = node_seen.get(i, "up")
                    if st != "up" and was == "up":
                        why = "令牌对不上（401）" if st == "401" else "连不上"
                        say(
                            f"\n[注意] {NODES[i]}（端口 {BASE_PORT + i}）{why} ——"
                            "后端与前端继续跑，页面上它会显示为掉线。"
                        )
                        say(
                            "       还有副本的块照样读得到（读取会自动改问副本）；"
                            "写操作会被拒，因为写要求全部在线。"
                        )
                        say(f"       节点组日志尾巴（logs/nodes.log）：")
                        say(tail(LOG_DIR / "nodes.log", 5))
                    elif st == "up" and was != "up":
                        say(f"\n[恢复] {NODES[i]}（端口 {BASE_PORT + i}）又答话了。")
                    node_seen[i] = st
    except KeyboardInterrupt:
        say("\n收工，停掉本次启动的进程…")
    finally:
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
                kill_port(BASE_PORT + i)
    say("已停。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
