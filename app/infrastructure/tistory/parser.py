# app/infrastructure/tistory/parser.py
"""티스토리 댓글 목록 HTML 파서.

`POST /comment/view` 응답의 ``data.comment`` 는 ``<li>`` 조각들의 나열이다.
실제 응답에서 확인한 구조는 다음과 같다.

``li`` 클래스 조합
    - ``rp_general``                  : 일반 댓글
    - ``rp_secret hiddenComment``     : 비밀 댓글
    - ``re_reply rp_general``         : 일반 대댓글
    - ``re_reply rp_admin``           : 운영자 대댓글
    - ``re_reply rp_secret hiddenComment`` : 비밀 대댓글

주의할 점
    - ``tit_nickname`` 안에 ``<a>`` 링크와 빈 프로필 트리거 ``<span>`` 이 들어갈 수 있다.
    - ``txt_date`` 안에는 날짜 뒤에 "신고" 링크가 따라붙는다. 날짜만 떼어내야 한다.
    - 속성이 작은따옴표로 감싸여 있어 정규식 대신 HTML 파서를 쓴다.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag

from ...domain.models import CommentPage, ParsedComment
from ..logging_setup import get_logger
from ..timeutils import from_epoch, parse_tistory_datetime

logger = get_logger(__name__)

# 운영자가 본문을 지웠을 때 티스토리가 넣는 안내 문구
ADMIN_DELETED_MARKER = "운영정책 위배로 관리자 삭제되었습니다"

# 비밀 댓글일 때 노출되는 본문
SECRET_MARKER = "비밀댓글입니다"

# li id 에서 댓글 번호를 뽑는 패턴
_COMMENT_ID_RE = re.compile(r"^comment(\d+)$")

# 날짜 문자열: YYYY.MM.DD HH:MM
_DATE_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}")

# BeautifulSoup 파서. 컴파일 의존성이 없는 표준 파서를 쓴다.
_PARSER = "html.parser"


class CommentHtmlParser:
    """댓글 조각 HTML을 도메인 객체로 바꾼다."""

    def __init__(self, *, tz_name: str = "Asia/Seoul") -> None:
        self._tz_name = tz_name

    def parse_page(self, payload: dict[str, Any], entry_id: int) -> CommentPage:
        """`/comment/view` 응답 JSON 전체를 :class:`CommentPage` 로 변환한다.

        Args:
            payload: 응답 JSON 의 ``data`` 객체.
            entry_id: 요청한 게시글 번호.
        """
        html = payload.get("comment") or ""
        comments = self.parse_comments(html, entry_id)

        cursor = _to_int(payload.get("ts"))
        has_more = bool(payload.get("isMoreComments"))
        first_id = _to_int(payload.get("firstCommentId"))
        raw_count = _to_int(payload.get("count")) or 0

        # 티스토리의 count 는 최상위 댓글만 센다. 대댓글은 부모에 딸려 함께 내려오므로
        # 전체 개수와 비교하면 정상 상황에서도 불일치로 보인다. 최상위끼리 비교해야 한다.
        top_level = sum(1 for comment in comments if not comment.is_reply)
        if raw_count and raw_count != top_level:
            # 여기서 어긋나면 마크업이 바뀌어 일부 댓글을 놓치고 있을 가능성이 있다.
            logger.warning(
                "댓글 파싱 건수 불일치 entry=%s 응답=%s 최상위파싱=%s 대댓글=%s",
                entry_id,
                raw_count,
                top_level,
                len(comments) - top_level,
            )

        # 커서가 비어 있으면 배치의 가장 오래된 댓글 시각으로 대체해 페이징이 끊기지 않게 한다.
        if cursor is None and comments:
            cursor = comments[0].written_ts

        return CommentPage(
            comments=tuple(comments),
            cursor=cursor,
            has_more=has_more,
            first_comment_id=first_id,
            raw_count=raw_count,
        )

    def parse_comments(self, html: str, entry_id: int) -> list[ParsedComment]:
        """``<li>`` 조각 나열에서 댓글 목록을 뽑는다. 순서는 시간 오름차순이다."""
        if not html.strip():
            return []

        soup = BeautifulSoup(html, _PARSER)
        results: list[ParsedComment] = []
        for element in soup.find_all("li"):
            comment = self._parse_item(element, entry_id)
            if comment is not None:
                results.append(comment)
        return results

    def _parse_item(self, element: Tag, entry_id: int) -> Optional[ParsedComment]:
        """``li`` 하나를 :class:`ParsedComment` 로 변환한다. 댓글이 아니면 None."""
        comment_id = _extract_comment_id(element.get("id"))
        if comment_id is None:
            # "이전 댓글 더보기" 같은 제어용 li 는 건너뛴다.
            return None

        classes = set(element.get("class") or [])
        content_box = element.find("span", class_="reply_content") or element

        date_text = _extract_date_text(content_box)
        if date_text is None:
            logger.warning("댓글 %s 의 작성 시각을 찾지 못해 건너뜁니다.", comment_id)
            return None

        try:
            written_at = parse_tistory_datetime(date_text, tz_name=self._tz_name)
        except ValueError:
            logger.warning("댓글 %s 의 시각 형식을 해석하지 못했습니다: %r", comment_id, date_text)
            return None

        nickname = _extract_nickname(content_box)
        content = _extract_content(content_box)

        is_secret = "rp_secret" in classes or "hiddenComment" in classes
        return ParsedComment(
            comment_id=comment_id,
            entry_id=entry_id,
            nickname=nickname,
            content=content,
            written_at=written_at,
            # 목록 HTML은 분 단위까지만 준다. 초 단위는 수집기가 커서 값으로 보정한다.
            written_ts=int(written_at.timestamp()),
            is_secret=is_secret,
            is_reply="re_reply" in classes,
            is_admin="rp_admin" in classes,
            is_admin_deleted=ADMIN_DELETED_MARKER in content,
        )


def _extract_comment_id(raw_id: Any) -> Optional[int]:
    """``comment23916088`` 형태의 id 속성에서 번호를 뽑는다."""
    if not isinstance(raw_id, str):
        return None
    matched = _COMMENT_ID_RE.match(raw_id.strip())
    return int(matched.group(1)) if matched else None


def _extract_nickname(container: Tag) -> str:
    """작성자 이름. 링크로 감싸여 있어도 텍스트만 모은다."""
    node = container.find("span", class_="tit_nickname")
    if node is None:
        return ""
    return node.get_text(separator=" ", strip=True)


def _extract_date_text(container: Tag) -> Optional[str]:
    """작성 시각 문자열. 뒤에 붙는 "신고" 링크는 제외한다."""
    node = container.find("span", class_="txt_date")
    if node is None:
        return None
    matched = _DATE_RE.search(node.get_text(separator=" ", strip=True))
    return matched.group(0) if matched else None


def _extract_content(container: Tag) -> str:
    """댓글 본문. ``<br>`` 은 줄바꿈으로 살린다."""
    node = container.find("span", class_="txt_reply")
    if node is None:
        return ""
    for line_break in node.find_all("br"):
        line_break.replace_with("\n")
    return node.get_text().strip()


def _to_int(value: Any) -> Optional[int]:
    """문자열로 오는 숫자 필드를 안전하게 정수로 바꾼다."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cursor_to_datetime(cursor: Optional[int]):
    """페이징 커서(epoch 초)를 UTC datetime 으로 바꾼다. None 은 그대로 통과시킨다."""
    return from_epoch(cursor) if cursor is not None else None
