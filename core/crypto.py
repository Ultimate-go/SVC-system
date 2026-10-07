"""加密与向量分量 —— 把明文变成可以承诺的向量。

数据流
------
::

    明文文件 ──切 1 KB 段──▶ 每段独立 SM4-CTR ──▶ 密文段
                                                   │
                                     SM3 摘要（32 字节）
                                                   ▼
                                           向量分量 v ∈ [0, 2^256)

**承诺的对象是密文摘要，不是明文** —— 于是"验证完整性"与"能否读懂"
彻底解耦：任何人都能验证密文没被改（验证不受限），但只有拿到块密钥的
人才能解出明文（解密受限）。这正是需求里那个病例本场景的骨架。

一条纪律：**分量由验证方自己从收到的密文算**
------------------------------------------------------------------------
:func:`vector_element` 是向量分量的**唯一合法来源**，而它只吃密文。
节点声称的那份分量在协议里不参与判定（``/node/retrieve`` 甚至不传它）——
所以"承诺的分量"与"实际交付的字节"没有可声明的自由度。

三个参数约定（规划 §2.5）
------------------------
* **每段一把独立密钥**。不是为了加密本身（整文件一把 CTR 也能做随机访问），
  而是为了**按段隔离** —— 一块坏了只影响那一块，改一块也不必重算后面的。
  代价是每段都要单独保护它的密钥：块密钥由 :mod:`core.keywrap` 用
  文件**所有者的 SM2 公钥**逐块封装。
* **IV 绝不复用**。CTR 模式下 IV 复用等于明文异或泄露。
* **模式必须选 CTR**。CTR 的分段之间互相独立，改第 3 段不影响别的段；
  CBC 会把第 4 段一起弄坏，那样"改一块"就得重算后续所有块。

.. warning::

   **纯 CTR 不提供完整性**。这是**故意的分工**：完整性由向量承诺负责
   （:func:`svc.verify` 的两步校验），而承诺里那些分量又是验证方**自己从
   密文算**出来的 —— 两件事合起来就是"交付的字节没被改"的完整结论，
   不需要再叠一层密文自检。要在答辩时主动说明这个分工，
   否则会被问"你这不是没做认证加密吗"。
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "SEGMENT_BYTES",
    "DIGEST_BYTES",
    "L",
    "SM4_KEY_BYTES",
    "SM4_IV_BYTES",
    "new_key",
    "new_iv",
    "encrypt_segment",
    "decrypt_segment",
    "split_segments",
    "join_segments",
    "sm3_digest",
    "vector_element",
]

#: 一个向量分量对应多少字节的密文。1 KB 是"少到能随机访问、多到不至于
#: 让块数膨胀"的折中：一份 8 MB 的文件切出来约 8 千块。
#: （改造前这里的说法是"n_max = 8192 时文件上限 = 8 MB" ——
#:   块数现在已经没有上限了，1 KB 保留下来是因为它既好读又好算。）
SEGMENT_BYTES: int = 1024

#: SM3 的输出字节数。
DIGEST_BYTES: int = 32

#: 向量的每个分量占多少比特。正好装下一个 SM3 摘要。
L: int = DIGEST_BYTES * 8  # 256

SM4_KEY_BYTES: int = 16
SM4_IV_BYTES: int = 16


def new_key() -> bytes:
    """生成一把 SM4 块密钥。"""
    return os.urandom(SM4_KEY_BYTES)


def new_iv() -> bytes:
    """生成一个 IV。**每段都必须新抽一个**，绝不复用。"""
    return os.urandom(SM4_IV_BYTES)


def _cipher(key: bytes, iv: bytes):
    if len(key) != SM4_KEY_BYTES:
        raise ValueError(f"SM4 密钥必须是 {SM4_KEY_BYTES} 字节，收到 {len(key)}")
    if len(iv) != SM4_IV_BYTES:
        raise ValueError(f"IV 必须是 {SM4_IV_BYTES} 字节，收到 {len(iv)}")
    return Cipher(algorithms.SM4(key), modes.CTR(iv))


def encrypt_segment(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """SM4-CTR 加密一段。"""
    enc = _cipher(key, iv).encryptor()
    return enc.update(plain) + enc.finalize()


def decrypt_segment(cipher: bytes, key: bytes, iv: bytes) -> bytes:
    """SM4-CTR 解密一段（CTR 是对合运算，加解密同一条路径）。"""
    dec = _cipher(key, iv).decryptor()
    return dec.update(cipher) + dec.finalize()


def split_segments(data: bytes, segment_bytes: int = SEGMENT_BYTES) -> list[bytes]:
    """把字节串切成段。最后一段可能不足 ``segment_bytes``，**不补齐** ——
    因为每段是独立加密的，补齐反而会把长度信息搞乱（长度由调用方另存）。
    """
    if segment_bytes <= 0:
        raise ValueError("segment_bytes 必须为正")
    return [data[i : i + segment_bytes] for i in range(0, len(data), segment_bytes)]


def join_segments(segs) -> bytes:
    """把段拼回原字节串。段的边界已经由长度决定，不需要填充约定。"""
    return b"".join(segs)


def sm3_digest(data: bytes) -> bytes:
    """SM3 摘要（32 字节）。"""
    h = hashes.Hash(hashes.SM3())
    h.update(data)
    return h.finalize()


def vector_element(cipher: bytes) -> int:
    """密文段 → 向量分量。

    取 SM3 摘要当大端整数，恰好落在 :math:`[0, 2^{256})` ——
    正好是 ``l = 256`` 允许的值域，不需要截断也不会越界。

    ★ 这是向量分量的**唯一合法来源**：验证方必须拿它自己算，不能用
    节点声称的那份（见模块说明）。
    """
    return int.from_bytes(sm3_digest(cipher), "big")


def file_digest(data: bytes) -> str:
    """整份数据的 SHA-256 十六进制串 —— **只用于日志/展示**，
    不是密码学承诺。不要在任何安全相关的判断里用它。
    """
    return hashlib.sha256(data).hexdigest()
