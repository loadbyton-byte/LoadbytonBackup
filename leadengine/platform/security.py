"""Password hashing, opaque tokens, TOTP and secret encryption. Stdlib + `cryptography` (Fernet) only."""
from __future__ import annotations

import base64
import hashlib
import threading
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024      # n=2^15, r=8 needs 32 MiB; OpenSSL's default limit would reject it
MIN_PASSWORD, MAX_PASSWORD = 12, 256


def check_password_policy(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters."
    return None


_SCRYPT_SLOTS = threading.BoundedSemaphore(4)       # bounds concurrent 32 MiB hashes so a login flood cannot exhaust memory/CPU


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _SCRYPT_SLOTS:
        return _scrypt_raw(password, salt, n, r, p)


def _scrypt_raw(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """False (never an exception) for malformed hashes or absurd inputs. Constant-time compare of the derived key."""
    try:
        if len(password) > MAX_PASSWORD:
            return False
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if not (2**10 <= n <= 2**20) or r > 32 or p > 8:      # refuse parameters that could be used to burn CPU/memory
            return False
        salt, expected = base64.b64decode(salt_b64, validate=True), base64.b64decode(dk_b64, validate=True)
        return hmac.compare_digest(_scrypt(password, salt, n, r, p), expected)
    except (ValueError, TypeError, OverflowError, MemoryError):
        return False


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """(raw token to give the user, sha256 hex to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# ---------------------------------------------------------------- TOTP (RFC 6238, SHA-1, 6 digits, 30 s)
def new_totp_secret() -> str:
    """160-bit secret, 32 base32 characters (no padding needed)."""
    return base64.b32encode(os.urandom(20)).decode()


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def totp_code(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((time.time() if at is None else at) // 30))


def totp_verify(secret: str, code: str, at: float | None = None) -> bool:
    return totp_matching_step(secret, code, at) is not None


def totp_matching_step(secret: str, code: str, at: float | None = None) -> int | None:
    """Time-step counter that matches `code` (within ±1 step) or None. Callers store it to refuse replay of the same step."""
    if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
        return None
    now = int((time.time() if at is None else at) // 30)
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, email: str, issuer: str = "DigitalBurj Leads") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    out: set[str] = set()
    while len(out) < n:
        out.add("".join(secrets.choice(alphabet) for _ in range(5)) + "-" + "".join(secrets.choice(alphabet) for _ in range(5)))
    return sorted(out)


# ---------------------------------------------------------------- secret encryption
def generate_key() -> str:
    return Fernet.generate_key().decode()


class Encryptor:
    def __init__(self, key: str):
        try:
            self._f = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise ValueError("ENCRYPTION_KEY must be a Fernet key (generate with leadengine.platform.security.generate_key())") from e

    def encrypt(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError) as e:
            raise ValueError("cannot decrypt value (wrong ENCRYPTION_KEY or tampered data)") from e
