"""块密钥的封装，以及**用户私钥的口令封装** —— 取代 ABE 的那一层。

这里只有两件事，都是平铺的函数，**刻意不做可插拔接口**
----------------------------------------------------------------
之前有一个 ``ABEProvider`` Protocol + 方案 A / 方案 B 两个实现。现在只有一种
做法，再抽一层接口就只是让人多绕一步，所以按需求**不保留接口**。

第 1 层 · 块密钥 ── 用**所有者的 SM2 公钥**封装
------------------------------------------------
``wrap_key(pk_owner, block_key)`` / ``unwrap_key(sk_owner, ct)``

* 每次封装抽一把**临时私钥** ``d``，密文里带上 ``R = d·G``；
* 双方各算 ``z = d·pk_owner`` / ``sk_owner·R``，值相同（都是 ``d·sk_owner·G`` 的 x 坐标）；
* ``SM4 密钥 = KDF(z, 加密标签)``，密文体用 SM4-CTR 加密；
* ``tag = KDF(z, 认证标签 ‖ R ‖ iv ‖ 密文体)``。

**为什么 tag 用 KDF 算、而不另引一个 MAC**
`core/fabeo.py` 里已经是这个形状（``tag = _kdf(z, _LABEL_TAG + body)``）。
``z`` 是每次封装新抽的临时共享秘密、从不复用，拿它当 PRF 的密钥算 tag 是
安全的；而且这样整条链路只用 SM2 + SM3 + SM4，**不用为口令拉伸之外的地方
引入任何 SHA**。长度扩展在这里不构成问题：KDF 是 SM3 计数模式，只取前
32 字节、counter 固定，攻击者无法续算。

**这个 tag 不是"可选的保险"，它挡的是具体攻击**：CTR 本身不提供完整性
（见 ``core/crypto.py`` 的 warning）。没有 tag 的话，节点把密文体翻一位，
解出来的块密钥就变成另一个值 —— 那个坏密钥会一路带到 SM4 解密，表现为
"文件内容是乱的"，而不是"这里被篡改了"。有了 tag，**篡改在解封这一步
就现形**，并且能明确报出来。

第 2 层 · 用户私钥 ── 用**用户口令**封装
----------------------------------------
``wrap_private_key(password, sk)`` / ``unwrap_private_key(password, blob)``

* ``KEK = PBKDF2-HMAC-SM3(口令, 盐, 迭代数)``；
* 私钥（32 字节的标量）用 KEK 派生的 SM4 密钥加密，加同样的 tag。

**这一层才是"只有自己才能解密"真正落地的地方。**
如果只把私钥明文存库、靠接口判断"这是不是你的文件"，那"只有自己"就只是
一句应用层的话 —— 谁拿到数据库谁就能解开全部文件。现在私钥是**用口令包起来
的**：数据库被拿走、连同这个文件一起被拿走，**没有口令依然解不开**。
（口令本身不落库 —— 库里只有它的加盐哈希，那是给登录校验用的另一回事。）

★ 换了这一层的参数就等于换协议
--------------------------------
两个 ``KIND`` 常量会写进密文，解封时先比对。``core/abe.py`` 里就有这个约定
（``_KIND = "scheme-a"``），原文写的是"避免 A/B 的密文被互相喂进去"。沿用：
老格式的密文喂进来会立刻得到"kind 不认识"，而不是一个看不懂的代数错误。
"""

from __future__ import annotations

import hashlib
import hmac
import os

from .crypto import (
    SM4_IV_BYTES,
    SM4_KEY_BYTES,
    decrypt_segment,
    encrypt_segment,
)
from .sm2 import (
    N as SM2_N,
    Point,
    ecdh_shared,
    gen_private_key,
    is_on_curve,
    kdf,
    point_bytes,
    point_from_bytes,
    public_key_of,
)

__all__ = [
    "KIND_BLOCK",
    "KIND_USER_KEY",
    "PBKDF2_ITERATIONS",
    "PBKDF2_ITERATIONS_MAX",
    "PRIVATE_KEY_BYTES",
    "KeyWrapError",
    "KeyWrapFormatError",
    "KeyWrapIntegrityError",
    "public_bytes",
    "public_from_bytes",
    "new_keypair",
    "wrap_key",
    "unwrap_key",
    "wrap_private_key",
    "unwrap_private_key",
    "make_user_keypair",
    "is_block_ct",
    "is_user_key_blob",
    "self_check",
]

# ---------------------------------------------------------------------------
# 域分隔标签
#
# ★ 改这些串等于换协议：老密文会全部解不开。写进密文的 KIND 会让这件事
#   表现为一句明确的"这不是本格式的密文"，而不是一个莫名其妙的解密失败。
# ---------------------------------------------------------------------------

#: 块密钥密文的格式标识。
KIND_BLOCK = "sm2-ecies-v1"

#: 用户私钥密文的格式标识。
KIND_USER_KEY = "userkey-pbkdf2sm3-v1"

#: ECIES 包装里的两处域分隔（KDF 的 info）。
_INFO_ENC = b"vds/ecies/v1/enc"
_INFO_TAG = b"vds/ecies/v1/tag"

#: 用户私钥封装里的两处域分隔。
_INFO_UK_ENC = b"vds/userkey/v1/enc"
_INFO_UK_TAG = b"vds/userkey/v1/tag"

#: 一个私钥标量的字节数。SM2 的阶是 256 位，32 字节正好。
PRIVATE_KEY_BYTES: int = 32

#: 口令拉伸的迭代数。20 万次在本机约 0.1 秒 —— 登录时感觉不到，
#: 暴力破解的代价却抬了两个数量级。
PBKDF2_ITERATIONS: int = 200_000

#: 迭代数的**上限**。
#:
#: ★ 没有上限就是一个 DoS：``users.sk_wrapped`` 里的 ``iters`` 若是被改成
#: ``10**9``，一次登录就会把 CPU 占死很久（解封路径上没有任何人会再问一遍
#: “这个数合理吗”）。密文存在库里、库文件会被谁拿到不由我们假设 ——
#: 所以解封时**必须**把它夹住。
#:
#: 取得比生产档高一个数量级：给以后加固留空间，又不至于让一次解封变成分钟级。
PBKDF2_ITERATIONS_MAX: int = 2_000_000

#: 盐的字节数。
_SALT_BYTES: int = 16

#: 认证标签的字节数。
_TAG_BYTES: int = 32


# ---------------------------------------------------------------------------
# 异常：形状对齐被替换掉的 core/abe.py，好让调用方的 except 少改
# ---------------------------------------------------------------------------

class KeyWrapError(Exception):
    """本模块所有错误的基类。"""


class KeyWrapFormatError(KeyWrapError):
    """密文的形状不对（KIND 不认识、字段缺失、十六进制非法）。"""


class KeyWrapIntegrityError(KeyWrapError):
    """认证标签对不上 —— 密文被改过，或者用错了密钥。"""


# ---------------------------------------------------------------------------
# 公钥的序列化（薄封装，只为让调用方不用直接碰 core.sm2）
# ---------------------------------------------------------------------------

def public_bytes(pk: Point) -> bytes:
    """公钥 → 65 字节未压缩编码（``0x04 || x || y``）。"""
    return point_bytes(pk)


def public_from_bytes(data: bytes) -> Point:
    """65 字节 → 公钥，**并校验它在曲线上**。

    这一步不能省：不校验的话攻击者能塞一个落在小子群上的点，把共享点
    拖进可预测的集合（无效曲线攻击）。``core.sm2.point_from_bytes`` 已经做了。
    """
    if len(data) != 65:
        raise KeyWrapFormatError(f"公钥必须是 65 字节（未压缩），收到 {len(data)}")
    try:
        return point_from_bytes(data)
    except ValueError as exc:
        raise KeyWrapFormatError(str(exc)) from exc


def new_keypair() -> tuple[int, Point]:
    """抽一对新的 SM2 密钥（私钥标量, 公钥点）。"""
    sk = gen_private_key()
    return sk, public_key_of(sk)


# ---------------------------------------------------------------------------
# 第 1 层：块密钥的封装
# ---------------------------------------------------------------------------

def _expect(counter: object, *, what: str, kind: str) -> dict:
    """校验密文对象的基本形状，返回它（便于链式写）。"""
    if not isinstance(counter, dict):
        raise KeyWrapFormatError(f"{what}必须是对象，收到 {type(counter).__name__}")
    got = counter.get("kind")
    if got != kind:
        raise KeyWrapFormatError(
            f"{what}的格式是 {got!r}，本实现只认 {kind!r}"
            "（旧 ABE 密文、或换了 KIND 之后的密文，都会在这里被拦下）"
        )
    return counter


def _hex_field(obj: dict, name: str, *, size: int | None = None) -> bytes:
    raw = obj.get(name)
    if not isinstance(raw, str):
        raise KeyWrapFormatError(f"字段 {name!r} 应当是十六进制字符串")
    try:
        out = bytes.fromhex(raw)
    except ValueError as exc:
        raise KeyWrapFormatError(f"字段 {name!r} 不是合法的十六进制：{exc}") from exc
    if size is not None and len(out) != size:
        raise KeyWrapFormatError(f"字段 {name!r} 应当是 {size} 字节，收到 {len(out)}")
    return out


def wrap_key(pk_owner: Point, block_key: bytes) -> dict:
    """用所有者的公钥把一把**块密钥**包起来。

    :param pk_owner: 文件所有者的 SM2 公钥点。
    :param block_key: 明文块密钥（SM4 密钥，16 字节）。
    :returns: 可直接 JSON 序列化的密文字典。

    ★ 本函数**不留** ``block_key`` 的任何副本（局部变量随栈消失），
      调用方拿到返回值之后也必须立刻丢掉明文 —— 这是 ``core/store.py``
      那条"密钥不落库、不留副本"性质的延续。
    """
    if len(block_key) != SM4_KEY_BYTES:
        raise KeyWrapError(f"块密钥必须是 {SM4_KEY_BYTES} 字节，收到 {len(block_key)}")
    if pk_owner is None or not is_on_curve(pk_owner):
        raise KeyWrapError("所有者的公钥不在 SM2 曲线上")

    d_eph = gen_private_key()
    r_bytes = point_bytes(public_key_of(d_eph))
    z = ecdh_shared(d_eph, pk_owner)

    sm4_key = kdf(z, _INFO_ENC, SM4_KEY_BYTES)
    iv = os.urandom(SM4_IV_BYTES)
    body = encrypt_segment(block_key, sm4_key, iv)
    tag = kdf(z, _INFO_TAG + r_bytes + iv + body, _TAG_BYTES)

    return {
        "kind": KIND_BLOCK,
        "R": r_bytes.hex(),
        "iv": iv.hex(),
        "body": body.hex(),
        "tag": tag.hex(),
    }


def unwrap_key(sk_owner: int, ct: dict) -> bytes:
    """用所有者的私钥解出一把块密钥。

    :raises KeyWrapIntegrityError: 标签对不上（密文被改过，或这不是你的密文）。
    :raises KeyWrapFormatError: 形状不对（含"这是旧 ABE 密文"）。
    """
    _expect(ct, what="块密钥密文", kind=KIND_BLOCK)
    r_bytes = _hex_field(ct, "R", size=65)
    iv = _hex_field(ct, "iv", size=SM4_IV_BYTES)
    body = _hex_field(ct, "body")
    tag = _hex_field(ct, "tag", size=_TAG_BYTES)

    try:
        r_point = point_from_bytes(r_bytes)
    except ValueError as exc:
        raise KeyWrapFormatError(f"密文里的临时公钥非法：{exc}") from exc

    z = ecdh_shared(sk_owner, r_point)
    expect_tag = kdf(z, _INFO_TAG + r_bytes + iv + body, _TAG_BYTES)
    if not hmac.compare_digest(expect_tag, tag):
        raise KeyWrapIntegrityError(
            "块密钥密文的认证标签对不上 —— 密文被改过，或者这不是你的私钥"
        )

    sm4_key = kdf(z, _INFO_ENC, SM4_KEY_BYTES)
    return decrypt_segment(body, sm4_key, iv)


# ---------------------------------------------------------------------------
# 第 2 层：用户私钥的口令封装
# ---------------------------------------------------------------------------

def _stretch(password: str, salt: bytes, iterations: int) -> bytes:
    """口令 → 32 字节 KEK。用 PBKDF2-HMAC-**SM3**（与全项目的 SM 系一致）。"""
    if not password:
        raise KeyWrapError("口令不能为空")
    if iterations < 1000:
        raise KeyWrapError(f"迭代数太低（{iterations}），至少 1000")
    # ★ 上限同样必要（理由见 PBKDF2_ITERATIONS_MAX）：
    #   这个值可以直接来自库里的密文，不夹住就是一条免费的 DoS 路径。
    if iterations > PBKDF2_ITERATIONS_MAX:
        raise KeyWrapError(
            f"迭代数太高（{iterations}），上限 {PBKDF2_ITERATIONS_MAX} —— "
            f"这份密钥密文可能被改过"
        )
    return hashlib.pbkdf2_hmac(
        "sm3", password.encode("utf-8"), salt, iterations, dklen=32
    )


def wrap_private_key(
    password: str,
    sk: int,
    *,
    iterations: int = PBKDF2_ITERATIONS,
    salt: bytes | None = None,
) -> dict:
    """用口令把**用户私钥**包起来 —— 这是"库被拿走也解不开"的落点。

    :param salt: 一般不用传（每次自动抽）。测试里会传固定值以复现。
    """
    if not (1 <= sk < SM2_N):
        raise KeyWrapError("私钥标量必须落在 [1, N-1]")
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    if len(salt) < 8:
        raise KeyWrapError("盐至少 8 字节")

    kek = _stretch(password, salt, iterations)
    sm4_key = kdf(kek, _INFO_UK_ENC, SM4_KEY_BYTES)
    iv = os.urandom(SM4_IV_BYTES)
    body = encrypt_segment(sk.to_bytes(PRIVATE_KEY_BYTES, "big"), sm4_key, iv)
    tag = kdf(kek, _INFO_UK_TAG + salt + iv + body, _TAG_BYTES)

    return {
        "kind": KIND_USER_KEY,
        "salt": salt.hex(),
        "iters": iterations,
        "iv": iv.hex(),
        "body": body.hex(),
        "tag": tag.hex(),
    }


def unwrap_private_key(password: str, blob: dict) -> int:
    """口令 + 密文 → 私钥标量。

    :raises KeyWrapIntegrityError: **口令错**，或者密文被改过。
        这两件事在密码学上分不开，所以给同一句话 —— 这也是想要的
        （不能给攻击者一个"口令对不对"的 oracle）。
    """
    _expect(blob, what="用户私钥密文", kind=KIND_USER_KEY)
    salt = _hex_field(blob, "salt")
    iv = _hex_field(blob, "iv", size=SM4_IV_BYTES)
    body = _hex_field(blob, "body")
    tag = _hex_field(blob, "tag", size=_TAG_BYTES)

    raw_iters = blob.get("iters")
    if not isinstance(raw_iters, int) or raw_iters < 1000:
        raise KeyWrapFormatError(f"字段 'iters' 不合法：{raw_iters!r}")

    kek = _stretch(password, salt, raw_iters)
    expect_tag = kdf(kek, _INFO_UK_TAG + salt + iv + body, _TAG_BYTES)
    if not hmac.compare_digest(expect_tag, tag):
        raise KeyWrapIntegrityError("口令不对，或者私钥密文被改过")

    sm4_key = kdf(kek, _INFO_UK_ENC, SM4_KEY_BYTES)
    sk = int.from_bytes(decrypt_segment(body, sm4_key, iv), "big")
    if not (1 <= sk < SM2_N):
        # 走到这里说明标签是对的但标量越界 —— 只可能是别处写坏了
        raise KeyWrapError("解出来的私钥标量越界，密文可能损坏")
    return sk


def make_user_keypair(password: str, *, iterations: int = PBKDF2_ITERATIONS):
    """一步生成"用户密钥对 + 口令封装好的私钥"。

    :returns: ``(sk, pk, blob)`` —— 调用方**把 sk 立刻丢掉**（或只留在内存会话里），
        库里只该存 ``public_bytes(pk)`` 与 ``blob``。
    """
    sk, pk = new_keypair()
    return sk, pk, wrap_private_key(password, sk, iterations=iterations)


# ---------------------------------------------------------------------------
# 形状判定（供调用方区分"新密文 / 老 ABE 密文"）
# ---------------------------------------------------------------------------

def is_block_ct(obj: object) -> bool:
    """这是不是本格式的块密钥密文。"""
    return isinstance(obj, dict) and obj.get("kind") == KIND_BLOCK


def is_user_key_blob(obj: object) -> bool:
    """这是不是本格式的用户私钥密文。"""
    return isinstance(obj, dict) and obj.get("kind") == KIND_USER_KEY


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def self_check() -> list[tuple[str, bool]]:
    """用互相独立的性质校验本模块。返回 ``[(说明, 是否通过), ...]``。

    放在这里（而不是只放在测试里）是为了让启动器/自检接口也能跑一遍；
    风格对齐 ``core.sm2.self_check``。
    """
    out: list[tuple[str, bool]] = []

    def check(name: str, fn) -> None:  # noqa: ANN001 - 局部小工具
        try:
            out.append((name, bool(fn())))
        except Exception as exc:  # noqa: BLE001 - 自检要报告失败而不是抛出
            out.append((f"{name}（异常：{type(exc).__name__}: {exc}）", False))

    sk, pk = new_keypair()
    key = bytes(range(SM4_KEY_BYTES))

    def roundtrip() -> bool:
        return unwrap_key(sk, wrap_key(pk, key)) == key

    def wrong_key() -> bool:
        other_sk, _ = new_keypair()
        try:
            unwrap_key(other_sk, wrap_key(pk, key))
        except KeyWrapIntegrityError:
            return True
        return False

    def tamper_body() -> bool:
        ct = wrap_key(pk, key)
        raw = bytearray(bytes.fromhex(ct["body"]))
        raw[0] ^= 0x01
        ct["body"] = bytes(raw).hex()
        try:
            unwrap_key(sk, ct)
        except KeyWrapIntegrityError:
            return True
        return False

    def tamper_r() -> bool:
        # 换掉临时公钥：z 会变，标签必然对不上
        ct = wrap_key(pk, key)
        other_sk, _ = new_keypair()
        ct["R"] = point_bytes(public_key_of(other_sk)).hex()
        try:
            unwrap_key(sk, ct)
        except (KeyWrapIntegrityError, KeyWrapFormatError):
            return True
        return False

    def rejects_old_kind() -> bool:
        try:
            unwrap_key(sk, {"kind": "scheme-a", "anything": "x"})
        except KeyWrapFormatError:
            return True
        return False

    def user_key_roundtrip() -> bool:
        blob = wrap_private_key("correct horse", sk, iterations=1000)
        return unwrap_private_key("correct horse", blob) == sk

    def user_key_wrong_password() -> bool:
        blob = wrap_private_key("correct horse", sk, iterations=1000)
        try:
            unwrap_private_key("wrong horse", blob)
        except KeyWrapIntegrityError:
            return True
        return False

    def user_key_tamper() -> bool:
        blob = wrap_private_key("pw", sk, iterations=1000)
        raw = bytearray(bytes.fromhex(blob["body"]))
        raw[0] ^= 0x80
        blob["body"] = bytes(raw).hex()
        try:
            unwrap_private_key("pw", blob)
        except KeyWrapIntegrityError:
            return True
        return False

    check("块密钥：封了能解回来", roundtrip)
    check("块密钥：别人的私钥解不开", wrong_key)
    check("块密钥：密文体被改一位就报错", tamper_body)
    check("块密钥：临时公钥被换就报错", tamper_r)
    check("块密钥：旧 ABE 密文被明确拒绝", rejects_old_kind)
    check("私钥：口令封了能解回来", user_key_roundtrip)
    check("私钥：口令错就报错", user_key_wrong_password)
    check("私钥：密文被改一位就报错", user_key_tamper)
    check("私钥：解出来的标量落在 [1, N-1]", lambda: 1 <= unwrap_private_key(
        "pw", wrap_private_key("pw", sk, iterations=1000)
    ) < SM2_N)
    return out
