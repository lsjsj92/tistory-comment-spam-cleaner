# app/infrastructure/tistory/auth.py
"""세션 쿠키 입력 처리.

티스토리는 카카오 계정으로 로그인하기 때문에 스크립트로 로그인을 흉내 내기 어렵다.
대신 이미 로그인한 브라우저의 쿠키를 그대로 가져다 쓴다. 사용자가 어떤 형태로 붙여
넣어도 받아들이도록 여러 입력 형식을 지원한다.

지원 형식
    1. 개발자도구 Network 탭의 "Copy as cURL" 결과 전체
    2. ``Cookie: a=b; c=d`` 형태의 헤더 한 줄
    3. ``a=b; c=d`` 형태의 쿠키 문자열
    4. 개발자도구 Application 탭에서 복사한 JSON 배열 ``[{"name": ..., "value": ...}]``
    5. ``{"a": "b"}`` 형태의 JSON 객체
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Optional
from urllib.parse import urlparse

from ...domain.errors import ValidationError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# 로그인 여부 판단에 의미가 있는 쿠키 이름들. 진단 메시지 품질을 높이는 용도로만 쓴다.
KNOWN_SESSION_COOKIES = ("TSSESSION", "_TSESSION", "TISTORY_SESSION")

# 쿠키 이름으로 허용할 문자. RFC 6265 의 token 규칙을 느슨하게 적용한다.
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# cURL 명령에서 헤더/쿠키 옵션을 식별하는 플래그
_HEADER_FLAGS = {"-H", "--header"}
_COOKIE_FLAGS = {"-b", "--cookie"}


def parse_cookie_input(raw: str) -> dict[str, str]:
    """사용자가 붙여넣은 문자열에서 쿠키 이름/값 쌍을 뽑는다.

    Raises:
        ValidationError: 어떤 형식으로도 쿠키를 찾지 못한 경우.
    """
    text = (raw or "").strip()
    if not text:
        raise ValidationError("쿠키 값이 비어 있습니다.")

    for extractor in (_from_json, _from_curl, _from_cookie_header):
        cookies = extractor(text)
        if cookies:
            validate_cookie_values(cookies)
            logger.info("쿠키 %d개를 인식했습니다.", len(cookies))
            return cookies

    raise ValidationError(
        "쿠키를 인식하지 못했습니다. 브라우저 개발자도구 Network 탭에서 요청을 "
        "'Copy as cURL' 로 복사해 그대로 붙여넣거나, 'name=value; name2=value2' 형식으로 입력하세요."
    )


def validate_cookie_values(cookies: dict[str, str]) -> None:
    """쿠키 값이 HTTP 헤더로 전송 가능한지 확인한다.

    쿠키 헤더는 ASCII 범위만 실을 수 있다. 붙여넣기가 잘못되어 한글 같은 문자가
    값으로 들어오면 요청을 만드는 순간 인코딩 오류가 나므로, 여기서 미리 걸러
    사용자에게 원인을 알려준다.

    Raises:
        ValidationError: 전송할 수 없는 문자가 포함된 경우.
    """
    invalid = [
        name
        for name, value in cookies.items()
        if not value.isascii() or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value)
    ]
    if invalid:
        raise ValidationError(
            f"쿠키 값에 전송할 수 없는 문자가 들어 있습니다: {', '.join(sorted(invalid))}. "
            "개발자도구에서 값이 잘리거나 다른 텍스트가 섞이지 않았는지 확인한 뒤 다시 복사하세요."
        )


def _from_json(text: str) -> dict[str, str]:
    """개발자도구에서 복사한 JSON 형태를 해석한다."""
    if not text.startswith(("[", "{")):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    cookies: dict[str, str] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = item.get("value")
            if name and value is not None:
                cookies[name] = str(value)
    elif isinstance(payload, dict):
        for name, value in payload.items():
            if isinstance(value, (str, int)):
                cookies[str(name).strip()] = str(value)
    return {name: value for name, value in cookies.items() if _COOKIE_NAME_RE.match(name)}


def _from_curl(text: str) -> dict[str, str]:
    """cURL 명령 전체에서 Cookie 헤더를 찾아낸다.

    Chrome 은 운영체제별로 세 가지 형식을 내놓는다.

    - bash:       ``curl 'url' -H 'Cookie: a=b'`` (역슬래시 줄바꿈)
    - cmd:        ``curl ^"url^" -H ^"Cookie: a=b^"`` (캐럿 줄바꿈, 캐럿 이스케이프)
    - PowerShell: ``curl.exe "url" -H "Cookie: a=b"`` (백틱 줄바꿈)

    셋 다 받아들여야 사용자가 어느 환경에서 복사하든 그대로 동작한다.
    """
    if "curl" not in text[:200].lower():
        return {}

    # 여러 줄로 복사된 명령의 줄바꿈 이스케이프(\ ^ `)를 먼저 제거한다.
    normalized = re.sub(r"[\\^`]\s*\n", " ", text)
    normalized = normalized.replace("\n", " ")
    normalized = _unescape_cmd_carets(normalized)

    try:
        tokens = shlex.split(normalized)
    except ValueError:
        # 따옴표가 맞지 않는 경우: 정규식으로 Cookie 헤더만 찾아본다.
        matched = re.search(r"[Cc]ookie:\s*([^'\"]+)", normalized)
        return _parse_cookie_pairs(matched.group(1)) if matched else {}

    cookies: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if index + 1 >= len(tokens):
            break
        value = tokens[index + 1]
        if token in _HEADER_FLAGS and value.lower().startswith("cookie:"):
            cookies.update(_parse_cookie_pairs(value.split(":", 1)[1]))
        elif token in _COOKIE_FLAGS:
            cookies.update(_parse_cookie_pairs(value))
    return cookies


def _unescape_cmd_carets(text: str) -> str:
    """Windows cmd 형식의 캐럿 이스케이프를 원래 문자로 되돌린다.

    cmd 는 큰따옴표를 ``^"`` 로, 캐럿 자체를 ``^^`` 로, 퍼센트를 ``%%`` 로 감싼다.
    이 상태로 :func:`shlex.split` 에 넘기면 따옴표 짝이 맞지 않아 쿠키 일부를 잃는다.
    """
    if '^"' not in text:
        return text
    # 순서가 중요하다. 이스케이프된 캐럿을 먼저 자리표시자로 빼두어야
    # 뒤이은 치환이 원래 캐럿을 건드리지 않는다.
    placeholder = "\x00"
    return (
        text.replace("^^", placeholder)
        .replace('^"', '"')
        .replace("%%", "%")
        .replace(placeholder, "^")
    )


def _from_cookie_header(text: str) -> dict[str, str]:
    """``Cookie:`` 접두사가 있든 없든 쿠키 문자열을 해석한다."""
    candidate = text
    lowered = text.lower()
    if lowered.startswith("cookie:"):
        candidate = text.split(":", 1)[1]
    return _parse_cookie_pairs(candidate)


def _parse_cookie_pairs(text: str) -> dict[str, str]:
    """``a=b; c=d`` 를 딕셔너리로 바꾼다. 값에 ``=`` 가 들어 있어도 안전하다."""
    cookies: dict[str, str] = {}
    for chunk in text.replace("\n", ";").split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        name = name.strip()
        if _COOKIE_NAME_RE.match(name):
            cookies[name] = value.strip()
    return cookies


def serialize_cookies(cookies: dict[str, str]) -> str:
    """쿠키 딕셔너리를 저장용 문자열로 직렬화한다."""
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def deserialize_cookies(payload: Optional[str]) -> dict[str, str]:
    """저장된 문자열을 쿠키 딕셔너리로 되돌린다. 값이 없으면 빈 딕셔너리."""
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # 예전 형식이거나 손상된 경우 쿠키 문자열로 한 번 더 시도한다.
        return _parse_cookie_pairs(payload)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def cookie_domain_of(blog_url: str) -> str:
    """쿠키 자동 추출에 사용할 도메인. ``lsjsj92.tistory.com`` 형태를 돌려준다."""
    host = urlparse(blog_url).hostname or ""
    return host


def load_cookies_from_browser(blog_url: str) -> dict[str, str]:
    """설치된 브라우저에서 티스토리 쿠키를 직접 읽어온다.

    ``browser_cookie3`` 는 선택 의존성이다. 설치되어 있지 않거나 OS 키체인 권한 때문에
    실패해도 애플리케이션 전체가 멈추지 않도록 :class:`ValidationError` 로 번역한다.

    Raises:
        ValidationError: 라이브러리가 없거나 쿠키를 읽지 못한 경우.
    """
    try:
        import browser_cookie3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValidationError(
            "browser_cookie3 가 설치되어 있지 않습니다. "
            "pip install browser-cookie3 로 설치하거나 쿠키를 직접 붙여넣으세요."
        ) from exc

    host = cookie_domain_of(blog_url)
    if not host:
        raise ValidationError("블로그 주소에서 도메인을 확인하지 못했습니다.")

    collected: dict[str, str] = {}
    errors: list[str] = []
    # 브라우저별로 순회하며 성공한 것만 모은다. 하나라도 성공하면 진행한다.
    loaders = (
        ("chrome", getattr(browser_cookie3, "chrome", None)),
        ("edge", getattr(browser_cookie3, "edge", None)),
        ("firefox", getattr(browser_cookie3, "firefox", None)),
        ("brave", getattr(browser_cookie3, "brave", None)),
    )
    for name, loader in loaders:
        if loader is None:
            continue
        try:
            jar = loader(domain_name="tistory.com")
        except Exception as exc:  # noqa: BLE001 - 브라우저별 예외 종류가 제각각이다.
            errors.append(f"{name}: {exc}")
            continue
        for cookie in jar:
            if cookie.domain and cookie.domain.lstrip(".") in {"tistory.com", host}:
                collected[cookie.name] = cookie.value

    if not collected:
        detail = " / ".join(errors) if errors else "일치하는 쿠키가 없습니다."
        raise ValidationError(f"브라우저에서 쿠키를 가져오지 못했습니다. {detail}")
    validate_cookie_values(collected)
    return collected


def describe_cookies(cookies: dict[str, str]) -> tuple[str, ...]:
    """쿠키 이름만 정렬해 돌려준다. 값은 절대 노출하지 않는다."""
    return tuple(sorted(cookies))


def has_session_cookie(cookies: dict[str, str]) -> bool:
    """세션으로 보이는 쿠키가 하나라도 있는지 확인한다."""
    return any(name in cookies for name in KNOWN_SESSION_COOKIES)
