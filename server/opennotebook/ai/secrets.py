"""Keeping an AI key secret at rest.

A key entered in the web app is stored encrypted (Fernet: AES with an HMAC,
from `cryptography`), never as it was typed. The encryption key is the
operator's `OPENNOTEBOOK_SECRET_KEY` when set (any string; it is hashed into a
key), else one made on first use and kept beside the files, readable by the
studio's user only. The api and the worker share the files, so both read
the same one.

Losing it loses the stored keys, not the studio: a key that cannot be
decrypted reads as missing, and the setup tour asks for it again.
"""

import base64
import functools
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from opennotebook.config import settings

log = logging.getLogger(__name__)

SECRET_ENV = "OPENNOTEBOOK_SECRET_KEY"
KEY_FILE = ".secret.key"


def _key_file() -> Path:
    return settings().files_dir / KEY_FILE


@functools.cache
def _fernet() -> Fernet:
    given = os.environ.get(SECRET_ENV, "").strip()
    if given:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(given.encode()).digest()))
    path = _key_file()
    try:
        return Fernet(path.read_bytes().strip())
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Made readable by its owner only, and never replaced once there: two
    # processes starting at once both end up with the first one's.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return Fernet(path.read_bytes().strip())
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.info("made the key that encrypts stored AI keys, at %s", path)
    return Fernet(key)


def seal(secret: str) -> str:
    """`secret`, encrypted. Empty stays empty."""
    return _fernet().encrypt(secret.encode()).decode() if secret else ""


def unseal(sealed: str) -> str:
    """The secret `sealed` holds, or empty when it cannot be decrypted (the
    encryption key changed): the key then reads as missing."""
    if not sealed:
        return ""
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken, ValueError:
        log.warning("a stored AI key could not be decrypted; was %s changed?", SECRET_ENV)
        return ""


def forget() -> None:
    """Read the encryption key again on next use, for tests."""
    _fernet.cache_clear()
