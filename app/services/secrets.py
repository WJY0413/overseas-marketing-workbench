from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _key_path():
    return get_settings().data_dir / ".secret_key"


def _load_or_create_key() -> bytes:
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Stored SMTP password cannot be decrypted with the current local key.") from exc
