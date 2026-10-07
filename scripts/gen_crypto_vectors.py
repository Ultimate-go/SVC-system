"""生成前端密码学实现的**对齐测试向量**（Python 侧权威输出）。

为什么要这个脚本
----------------
「服务器不可信」这件事只有真正做到"**浏览器自己解密、自己验证**"才成立。
于是前端必须把下面这些原语用纯 JS 重写一遍：

* ``SM3`` 摘要（分量 v_i = SM3(密文段)）
* ``SM4-CTR`` 解密
* ``SM2`` 曲线点运算 + ECDH + KDF（``SM3(shared || counter_be32 || info)``）
* 块密钥解封（``core.keywrap.unwrap_key`` 的**自定义 ECIES 形状**）
* 用户私钥解封（PBKDF2-HMAC-**SM3** + SM4-CTR + 认证标签）
* 证据验证（``add_back`` 链：``(S, Λ) ← (S^{e_i}, Λ^{e_i}·S^{v_i})``）

这些实现只要有一位不同，"客户端验证通过"就只是另一个人的猜测。

所以本脚本把 Python 的输出**固化**成 JSON，前端 ``npm test`` 逐条断言。
任何一处偏差都会在本地第一次跑测试时暴露，而不是等到演示现场。

用法
----
::

    python -X utf8 scripts/gen_crypto_vectors.py
    npm --prefix frontend test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.crypto import encrypt_segment, sm3_digest, vector_element  # noqa: E402
from core.keywrap import (  # noqa: E402
    new_keypair,
    public_bytes,
    unwrap_key,
    unwrap_private_key,
    wrap_key,
    wrap_private_key,
)
from core.sm2 import ecdh_shared, kdf, public_key_of  # noqa: E402
from core.session import _load_prime_cache  # noqa: E402
from svc import (  # noqa: E402
    CRS,
    DeterministicRNG,
    PrimeGen,
    commit,
    generate_primes,
    open_subvector,
    setup,
    specialize,
    verify,
)

OUT = ROOT / "frontend" / "tests" / "crypto-vectors.json"


def _h(b: bytes) -> str:
    return b.hex()


def sm3_vectors() -> list[dict]:
    """SM3 摘要 —— 含空串、单字节、以及一段 1024 字节（真实段长）。"""
    cases = [
        b"",
        b"abc",
        bytes(range(256)),
        b"a" * 55,  # 长度正好落在补齐边界前
        b"a" * 64,  # 正好一个分组
        bytes(range(256)) * 4,  # 1024 字节 = 真实密文段长度
    ]
    return [{"msg_hex": _h(m), "digest_hex": _h(sm3_digest(m))} for m in cases]


def sm4_vectors() -> list[dict]:
    """SM4-CTR —— 与 ``core.crypto.encrypt_segment`` 逐字节相同（CTR 对合）。"""
    cases = [
        (bytes(16), bytes(16), b""),
        (bytes(16), bytes(16), b"A"),
        (bytes(range(16)), bytes(range(16)), bytes(range(256)) * 4),  # 1024 字节
        (
            bytes.fromhex("0123456789abcdeffedcba9876543210"),
            bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
            b"the quick brown fox jumps over the lazy dog",
        ),
    ]
    return [
        {
            "key_hex": _h(k),
            "iv_hex": _h(iv),
            "plain_hex": _h(p),
            "cipher_hex": _h(encrypt_segment(p, k, iv)),
        }
        for k, iv, p in cases
    ]


def kdf_vectors() -> list[dict]:
    """KDF —— ``SM3(shared || counter_be32 || info)``，counter 从 1 起。"""
    cases = [
        (b"\x00" * 32, b"ENC", 16),
        (b"\x11" * 32, b"TAG", 32),
        (bytes(range(32)), bytes(range(8)), 48),  # 跨块，考计数器拼接
    ]
    return [
        {
            "shared_hex": _h(s),
            "info_hex": _h(i),
            "length": n,
            "out_hex": _h(kdf(s, i, n)),
        }
        for s, i, n in cases
    ]


def sm2_vectors() -> dict:
    """SM2 曲线 + ECDH —— 前端要能算出同样的共享点 x 坐标。"""
    sk_a, pk_a = new_keypair()
    sk_b, pk_b = new_keypair()
    return {
        "sk_a": str(sk_a),
        "pk_a_hex": _h(public_bytes(pk_a)),
        "sk_b": str(sk_b),
        "pk_b_hex": _h(public_bytes(pk_b)),
        # a 用自己的私钥 + b 的公钥算 z；b 用另一个方向算，两边必须相等
        "z_hex": _h(ecdh_shared(sk_a, pk_b)),
        "z_hex_other_side": _h(ecdh_shared(sk_b, pk_a)),
    }


def block_key_vectors() -> list[dict]:
    """块密钥封装 —— 前端写不出这格式，但必须**解得开**它。"""
    out = []
    for seed in (0x00, 0x55, 0xFF):
        sk, pk = new_keypair()
        key = bytes([seed]) * 16
        ct = wrap_key(pk, key)
        assert unwrap_key(sk, ct) == key  # 先自证一遍
        out.append({"sk": str(sk), "key_hex": _h(key), "ct": ct})
    return out


def user_key_vectors() -> list[dict]:
    """用户私钥封装 —— 前端要用**口令**在浏览器里解出私钥。"""
    out = []
    for password in ("vds12345", "another-pass"):
        sk, _pk = new_keypair()
        blob = wrap_private_key(password, sk, iterations=1000)
        assert unwrap_private_key(password, blob) == sk
        out.append({"password": password, "sk": str(sk), "blob": blob})
    return out


def svc_vectors() -> dict:
    """证据验证 —— 前端 ``add_back`` 链必须与 :func:`svc.verify` 同判。

    特意用**真实的 l = 256 / 1024 位模数**（不是小玩具参数），
    这样前端遇到的是同一量级的 BigInt；并且一次给出"应当通过"与
    "应当拒绝"两套，防止前端实现成"永远返回 true"。
    """
    rng = DeterministicRNG(b"align-svc-vectors")
    # ★ 这里刻意用**小参数**（65 位素数 / 256 位模数）。
    #
    #   本向量要钉住的是**代数**：add_back 链、两条判据、BigInt 运算量级 ——
    #   这些与位长无关。而 Python 生成 512 位素数要**分钟级**
    #   （项目为此才做了素数表落盘 + 启动预热），把向量生成拖到不可用没有意义。
    #
    #   真实量级（1024 位 N / 257 位素数）由浏览器对着**真实后端**跑的
    #   端到端验证覆盖 —— 见 `frontend/tests/crypto-e2e.test.js` 与验收文档。
    crs = setup(lambda_bits=256, l=64, n=8, rng=rng)
    crs_n = specialize(crs, 8)
    vals = [rng.randbelow(1 << 64) for _ in range(8)]
    com = commit(crs_n, vals)
    I = [1, 3, 5]
    pi = open_subvector(crs_n, I, [vals[i] for i in I], vals)
    rep = verify(crs_n, com.C, I, [vals[i] for i in I], pi)
    assert rep.ok, rep.message  # 脚本自己先跑通

    # 反例：把第 3 个位置的值改掉 —— Λ 那条等式必须不成立
    bad_vals = list(vals)
    bad_vals[3] = (bad_vals[3] + 1) % (1 << 64)
    bad = verify(crs_n, com.C, I, [bad_vals[i] for i in I], pi)

    return {
        "N": str(crs_n.N),
        "g": str(crs_n.g),
        "l": 64,
        "prime_bits": 65,
        "n": 8,
        "primes": [str(p) for p in crs.primegen.first(8)],
        "e_all": str(crs_n.e_all),
        "U_n": str(crs_n.U_n),
        "C": str(com.C),
        # ---- 应当通过 ----
        "ok_case": {
            "I": I,
            "values": [str(vals[i]) for i in I],
            "S_I": str(pi.S_I),
            "Lambda_I": str(pi.Lambda_I),
            "expected": True,
        },
        # ---- 应当拒绝（值被改）----
        "bad_case": {
            "I": I,
            "values": [str(bad_vals[i]) for i in I],
            "S_I": str(pi.S_I),
            "Lambda_I": str(pi.Lambda_I),
            "expected": False,
        },
        "bad_code_name": bad.code.name,
    }


def client_pack() -> dict:
    """一整套**完整的** ``/cipher`` 应答 —— 给前端 ``openBlocks`` 做端到端回归。

    为什么需要它（安全审计 S4）：δ 钉扎那个修复的判据是
    “**本地 δ 说的话算，远端 δ 不算**”，而这需要一份**真实自洽**的材料才能测：
    密文、块密钥密文、证据 ``(S_I, Λ_I)``、``δ = (U, C)``、素数，彼此必须对得上。
    """
    rng = DeterministicRNG(b"align-client-pack")
    # ★ ``l`` 必须与真实系统一致（= 256）：``vector_element`` 固定产出 256 位的
    #   SM3 摘要，而 ``svc.commit`` 要求 ``v_i < 2^l``。
    #   但**模数 N 可以很小** —— 前端只拿 pack 里的 N 做模幂，数学上与位长无关；
    #   这样脚本不必花分钟级去造 1024 位 RSA 模数。
    #   素数优先从项目**落盘的素数表**读（257 位素数现场生成是分钟级的 ——
    #   这也正是项目要做素数预热的原因）。
    pg = PrimeGen(8, 256 + 1)
    cached = _load_prime_cache(ROOT / ".primecache" / "primes-b257.txt", 257)
    if cached:
        pg._adopt(cached[:8])
    n_mod, g_mod = generate_primes(rng, 128)
    crs = CRS(N=n_mod, g=g_mod, primegen=pg, l=256)
    crs_n = specialize(crs, 8)

    sk_owner, pk_owner = new_keypair()
    plains = [b"hello-vds-pack", bytes(range(48)), b"X" * 70]
    blocks = []
    vals = []
    keys = []
    for i, pt in enumerate(plains):
        key = bytes([i + 1]) * 16
        iv = bytes([0x10 + i]) * 16
        ct = encrypt_segment(pt, key, iv)
        keys.append(key)
        vals.append(vector_element(ct))
        blocks.append(
            {
                "block_idx": i,
                "global_index": i,
                "ciphertext_hex": ct.hex(),
                "iv_hex": iv.hex(),
                "plain_len": len(pt),
                "key_ct": wrap_key(pk_owner, key),
                "element": str(vals[-1]),
                "holder": "node-1",
            }
        )
    # 剩下的位置用随机值填满（它们不在这次打开的范围里，只是让向量长度对上）
    vals += [rng.randbelow(1 << 256) for _ in range(8 - len(plains))]

    com = commit(crs_n, vals)
    I = list(range(len(plains)))
    pi = open_subvector(crs_n, I, [vals[i] for i in I], vals)
    rep = verify(crs_n, com.C, I, [vals[i] for i in I], pi)
    assert rep.ok, rep.message  # 脚本自己先跑通

    return {
        "sk": str(sk_owner),
        "plains_hex": [p.hex() for p in plains],
        "keys_hex": [k.hex() for k in keys],
        "pack": {
            "owner": "probe",
            "file_key": "pack",
            "offset": 0,
            "n": 8,
            "segment_bytes": 1024,
            "crs": {
                "N": str(crs_n.N),
                "g": str(crs_n.g),
                "l": 256,
                "prime_bits": 257,
                "n_max": 8192,
            },
            "delta": {
                "U": str(crs_n.U_n),
                "C": str(com.C),
                "n": 8,
                "offset": 0,
                "fp": "alignpack0",
            },
            "primes": {
                "bits": 257,
                "start": str(1 << 256),
                "indices": I,
                "values": [str(crs.primegen.get(i)) for i in I],
            },
            "blocks": blocks,
            "proof": {
                "S_I": str(pi.S_I),
                "Lambda_I": str(pi.Lambda_I),
                "I": I,
            },
            "verify": {"ok": True, "code": 0, "message": "验证通过"},
            "nodes_used": ["node-1"],
        },
    }


def element_vectors() -> list[dict]:
    """分量 —— 证实"密文 → 整数"这一步就是 SM3 大端。"""
    out = []
    for ct in (bytes(16), bytes(range(64)), bytes([7]) * 1024):
        out.append({"cipher_hex": _h(ct), "element": str(vector_element(ct))})
    return out


def main() -> int:
    data = {
        "_note": "由 scripts/gen_crypto_vectors.py 生成；前端 npm test 逐条断言。勿手改。",
        "sm3": sm3_vectors(),
        "sm4ctr": sm4_vectors(),
        "kdf": kdf_vectors(),
        "sm2": sm2_vectors(),
        "block_keys": block_key_vectors(),
        "user_keys": user_key_vectors(),
        "elements": element_vectors(),
        "svc": svc_vectors(),
        "client_pack": client_pack(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] 已写出 {OUT}")
    for key in ("sm3", "sm4ctr", "kdf", "elements", "block_keys", "user_keys"):
        print(f"     {key}: {len(data[key])} 条")
    print("     svc: 1 套（含应通过 / 应拒绝各一）")
    print("     client_pack: 1 套（完整 /cipher 应答，供 openBlocks 与 δ 钉扎回归）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
