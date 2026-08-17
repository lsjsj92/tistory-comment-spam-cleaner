# app/infrastructure/security/crypto.py
"""민감 값 암복호화와 마스킹.

세션 쿠키는 블로그 계정 전체를 조작할 수 있는 자격 증명이므로 평문으로 저장하지
않는다. `.env` 의 ``APP_SECRET_KEY`` 에서 파생한 대칭키로 암호화해 DB에 넣는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from ...domain.errors import ConfigurationError

# 암호문 앞에 붙여 형식을 식별하는 접두사. 나중에 알고리즘을 바꿀 때 구분자가 된다.
_PREFIX = "fernet:v1:"

# 비밀번호 해시 파라미터. 로컬 단일 계정용이므로 과하지 않게 잡는다.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def derive_fernet_key(secret_key: str) -> bytes:
    """임의 문자열에서 Fernet 이 요구하는 32바이트 base64 키를 유도한다.

    사용자가 ``APP_SECRET_KEY`` 에 아무 문자열이나 넣어도 동작하도록 SHA-256 으로
    길이를 맞춘다. 같은 입력에는 항상 같은 키가 나오므로 재기동 후에도 복호화된다.
    """
    if not secret_key:
        raise ConfigurationError("APP_SECRET_KEY 가 비어 있어 암호화 키를 만들 수 없습니다.")
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    """대칭키 기반 문자열 암복호화기."""

    def __init__(self, secret_key: str) -> None:
        self._fernet = Fernet(derive_fernet_key(secret_key))

    def encrypt(self, plaintext: str) -> str:
        """평문을 접두사가 붙은 암호문 문자열로 바꾼다."""
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        """암호문을 평문으로 되돌린다.

        Raises:
            ConfigurationError: 형식이 다르거나 키가 바뀌어 복호화할 수 없는 경우.
        """
        if not ciphertext.startswith(_PREFIX):
            raise ConfigurationError("저장된 값의 암호문 형식을 알 수 없습니다.")
        token = ciphertext[len(_PREFIX) :].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigurationError(
                "저장된 값을 복호화하지 못했습니다. APP_SECRET_KEY 가 변경되었을 수 있습니다. "
                "설정 화면에서 쿠키를 다시 등록하세요."
            ) from exc

    def try_decrypt(self, ciphertext: Optional[str]) -> Optional[str]:
        """복호화에 실패하면 예외 대신 None 을 돌려주는 관대한 버전."""
        if not ciphertext:
            return None
        try:
            return self.decrypt(ciphertext)
        except ConfigurationError:
            return None


def mask_secret(value: str, *, keep: int = 4) -> str:
    """로그와 API 응답에 노출해도 되도록 값을 가린다."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * min(len(value) - keep, 12)}"


def hash_password(password: str) -> str:
    """선택적 로그인 기능용 비밀번호 해시. ``salt$hash`` 형식으로 반환한다."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """저장된 해시와 비밀번호를 상수 시간으로 비교한다."""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


def constant_time_equals(left: str, right: str) -> bool:
    """평문 비교가 필요한 곳에서 타이밍 공격을 피하기 위한 헬퍼."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
