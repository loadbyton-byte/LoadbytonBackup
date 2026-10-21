import pytest

from leadengine.platform.security import (Encryptor, check_password_policy, generate_key, hash_password, hash_token, new_recovery_codes,
                                          new_token, new_totp_secret, otpauth_uri, totp_code, totp_verify, verify_password)

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # base32("12345678901234567890"), the RFC 6238 SHA-1 test key


def test_hash_format_and_roundtrip():
    h = hash_password("correct horse battery")
    assert h.split("$")[:4] == ["scrypt", "32768", "8", "1"]
    assert verify_password("correct horse battery", h) and not verify_password("wrong horse battery", h)
    assert hash_password("correct horse battery") != h                 # per-user random salt


def test_policy():
    assert check_password_policy("a" * 11) is not None
    assert check_password_policy("a" * 12) is None
    assert check_password_policy("a" * 256) is None and check_password_policy("a" * 257) is not None


@pytest.mark.parametrize("stored", ["", "x", "scrypt$1", "scrypt$a$b$c$d$e", "bcrypt$1$2$3$4$5", "scrypt$32768$8$1$!!$!!"])
def test_verify_rejects_garbage_stored_values(stored):
    assert verify_password("whatever-password", stored) is False


def test_verify_rejects_overlong_password_without_hashing():
    assert verify_password("a" * 5000, hash_password("a" * 20)) is False


def test_token_pair():
    raw, h = new_token()
    assert hash_token(raw) == h and len(h) == 64 and len(raw) >= 43 and new_token()[0] != raw


def test_totp_rfc6238_vectors():
    assert totp_code(RFC_SECRET, 59) == "287082"
    assert totp_code(RFC_SECRET, 1111111109) == "081804"


def test_totp_window_and_format():
    s = new_totp_secret()
    assert len(s) >= 32
    code = totp_code(s, 1_700_000_000)
    assert totp_verify(s, code, 1_700_000_000) and totp_verify(s, code, 1_700_000_030) and totp_verify(s, code, 1_699_999_970)
    assert not totp_verify(s, code, 1_700_000_090) and not totp_verify(s, "12345", 1_700_000_000) and not totp_verify(s, "abcdef", 1_700_000_000)


def test_otpauth_uri():
    u = otpauth_uri(RFC_SECRET, "a@b.com")
    assert u.startswith("otpauth://totp/") and f"secret={RFC_SECRET}" in u and "issuer=DigitalBurj" in u and "a%40b.com" in u


def test_recovery_codes_unique_and_formatted():
    codes = new_recovery_codes()
    assert len(codes) == 10 == len(set(codes)) and all(len(c) == 11 and c[5] == "-" for c in codes)


def test_encryptor_roundtrip_and_tamper():
    e = Encryptor(generate_key())
    t = e.encrypt("secret-value")
    assert t != "secret-value" and e.decrypt(t) == "secret-value"
    with pytest.raises(ValueError):
        e.decrypt(t[:-3] + "AAA")
    with pytest.raises(ValueError):
        Encryptor(generate_key()).decrypt(t)
    with pytest.raises(ValueError):
        Encryptor("not-a-key")
