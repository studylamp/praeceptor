"""Password/PIN hashing. Shared by seeding and the auth layer.

Argon2id via argon2-cffi. Student PINs and (if ever stored) other secrets go
through here; the parent/admin password is checked against the env value, not
stored hashed.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

_ph = PasswordHasher()


def hash_secret(raw: str) -> str:
    return _ph.hash(raw)


def verify_secret(hashed: str, raw: str) -> bool:
    """Constant-time-ish verify. Returns False on any mismatch or malformed hash."""
    try:
        return _ph.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    return _ph.check_needs_rehash(hashed)
