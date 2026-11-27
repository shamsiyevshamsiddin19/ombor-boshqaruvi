"""Reversible, encrypted copy of a user's password so an administrator can read it back.

The login check never uses this: Django's salted hash in ``User.password`` stays the
only thing ``check_password`` looks at. The copy here is Fernet-encrypted (AES-128-CBC
+ HMAC-SHA256) with a key taken from ``PASSWORD_REVEAL_KEY``, or derived from
``SECRET_KEY`` when that is unset. A database dump alone therefore does not expose
any password; the key has to leak too.

Rotating the key makes every stored copy unreadable (``decrypt`` returns ``None``);
the copy is written again the next time the password is set or the user logs in.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

_KEY_CONTEXT = b"wms.users.password-vault.v1:"


def _fernet() -> Fernet:
    secret = getattr(settings, "PASSWORD_REVEAL_KEY", "") or settings.SECRET_KEY
    digest = hashlib.sha256(_KEY_CONTEXT + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(raw_password: str) -> str:
    return _fernet().encrypt(raw_password.encode()).decode()


def decrypt(token: str) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
