"""Ed25519 signatures (RFC 8032), pure Python.

Adapted from the reference implementation in RFC 8032 section 6 and checked against its test
vectors. It is not constant-time: fine for signing earnest's own trust records on the user's
machine and for verifying others', not for a shared signing service.
"""
from __future__ import annotations

import hashlib
import os

p = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I_ = pow(2, (p - 1) // 4, p)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(d * y * y + 1, p - 2, p)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0:
        x = (x * I_) % p
    if x % 2 != 0:
        x = p - x
    return x


_By = 4 * pow(5, p - 2, p) % p
_Bx = _xrecover(_By)
B = (_Bx, _By, 1, _Bx * _By % p)   # extended coordinates
_ZERO = (0, 1, 1, 0)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    Bv = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = Bv - A, D - C, D + C, Bv + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s: int, P):
    Q = _ZERO
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q) -> bool:
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _compress(P) -> bytes:
    zinv = pow(P[2], p - 2, p)
    x, y = P[0] * zinv % p, P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= p:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x * x * 1 - (y * y - 1) * pow(d * y * y + 1, p - 2, p)) % p != 0:
        raise ValueError("not on curve")
    if x & 1 != sign:
        x = p - x
    return (x, y, 1, x * y % p)


def _secret_expand(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != 32:
        raise ValueError("secret key must be 32 bytes")
    h = _h(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    a, _ = _secret_expand(secret)
    return _compress(_mul(a, B))


def sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _secret_expand(secret)
    A = _compress(_mul(a, B))
    r = int.from_bytes(_h(prefix + msg), "little") % L
    R = _compress(_mul(r, B))
    k = int.from_bytes(_h(R + A + msg), "little") % L
    s = (r + k * a) % L
    return R + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _decompress(public)
        R = _decompress(signature[:32])
        s = int.from_bytes(signature[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(signature[:32] + public + msg), "little") % L
        return _equal(_mul(s, B), _add(R, _mul(k, A)))
    except ValueError:
        return False


def new_secret() -> bytes:
    return os.urandom(32)
