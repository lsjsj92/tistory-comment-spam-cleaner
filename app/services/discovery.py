# app/services/discovery.py
"""게시글 탐색 서비스.

sitemap 으로 블로그 전체 게시글을 찾고, 사용자가 입력한 주소나 번호를 수집 대상
정의로 바꾼다. 여기서 만드는 :class:`TargetSpec` 이 수집기의 입력이 된다.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..domain.errors import TistoryApiError, ValidationError
from ..domain.models import TargetSpec
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.tistory.client import TistoryClient

logger = get_logger(__name__)

# 게시글 주소의 경로 형태. `/723` 또는 `/723/` 만 게시글이고
# `/category/...`, `/tag/...`, `/` 는 게시글이 아니다.
ENTRY_PATH_PATTERN = re.compile(r"^/(\d+)/?$")

# 외부 컴파일 의존성이 없는 표준 HTML 파서
HTML_PARSER = "html.parser"

# 게시글 제목이 들어 있는 메타 태그 속성
OG_TITLE_PROPERTY = "og:title"

# 중첩 sitemap 은 한 단계만 따라가되, 그 단계에서 읽을 문서 수도 이 값으로 제한한다.
MAX_NESTED_SITEMAPS = 20


class DiscoveryService:
    """블로그에서 수집 대상 게시글을 찾아내는 서비스."""

    def __init__(self, client: TistoryClient, blog_url: str) -> None:
        self._client = client
        self._blog_url = blog_url.rstrip("/")
        self._host = (urlparse(self._blog_url).netloc or "").lower()

    # ------------------------------------------------------------------
    # sitemap 기반 전체 탐색
    # ------------------------------------------------------------------
    async def discover_from_sitemap(self) -> list[TargetSpec]:
        """sitemap.xml 에서 게시글 목록을 뽑는다.

        Returns:
            entry 번호 내림차순(최신 글 우선) 대상 목록. 제목은 채우지 않는다.
            수백 건의 제목을 여기서 모두 가져오면 요청이 과도해지기 때문이다.

        Raises:
            TistoryApiError: sitemap 을 가져오지 못했거나 XML 로 해석할 수 없는 경우.
        """
        document = await self._client.fetch_sitemap()
        page_locs, nested_locs = self._read_document(document)

        for nested_loc in nested_locs[:MAX_NESTED_SITEMAPS]:
            nested_document = await self._fetch_nested(nested_loc)
            if nested_document is None:
                continue
            # 한 단계만 따라간다. 여기서 또 sitemapindex 가 나와도 펼치지 않는다.
            child_locs, _ = self._read_document(nested_document)
            page_locs.extend(child_locs)

        entry_ids = {
            entry_id
            for entry_id in (self._entry_id_from_loc(loc) for loc in page_locs)
            if entry_id is not None
        }
        logger.info("sitemap 에서 게시글 %d건을 찾았습니다.", len(entry_ids))
        return [self._spec(entry_id) for entry_id in sorted(entry_ids, reverse=True)]

    # ------------------------------------------------------------------
    # 사용자 입력 해석
    # ------------------------------------------------------------------
    async def resolve_entry(self, url_or_id: str) -> TargetSpec:
        """주소 전체 또는 게시글 번호를 수집 대상 정의로 바꾼다.

        Args:
            url_or_id: `https://blog.tistory.com/723`, `blog.tistory.com/723`,
                `/723`, `723` 을 모두 받는다.

        Raises:
            ValidationError: 번호를 찾을 수 없거나 다른 블로그의 주소인 경우.
        """
        entry_id = self._parse_entry_id(url_or_id)
        title = await self.fetch_title(entry_id)
        return self._spec(entry_id, title=title)

    async def fetch_title(self, entry_id: int) -> Optional[str]:
        """게시글 제목을 가져온다.

        제목은 화면 표시용 부가 정보다. 실패해도 대상 등록이나 수집을 막지 않도록
        예외를 밖으로 내보내지 않고 None 을 돌려준다.
        """
        try:
            html = await self._client.fetch_entry_html(entry_id)
            return self._extract_title(html)
        except Exception:
            # 제목 하나 때문에 대상 등록이나 수집이 실패해서는 안 된다.
            logger.warning("게시글 %s 의 제목을 가져오지 못했습니다.", entry_id, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 내부 처리
    # ------------------------------------------------------------------
    def _spec(self, entry_id: int, *, title: Optional[str] = None) -> TargetSpec:
        """게시글 번호로 대상 정의를 만든다."""
        return TargetSpec(entry_id=entry_id, url=f"{self._blog_url}/{entry_id}", title=title)

    def _read_document(self, document: str) -> tuple[list[str], list[str]]:
        """sitemap 문서에서 (게시글 주소, 중첩 sitemap 주소) 를 분리해 읽는다.

        `urlset` 과 `sitemapindex` 를 모두 받는다. 네임스페이스 선언이 문서마다
        다르므로 태그 이름은 접두사를 떼고 비교한다.
        """
        try:
            # 응답 앞에 BOM 이나 공백이 붙어 오면 XML 선언보다 앞서서 파싱이 깨진다.
            root = ElementTree.fromstring(document.lstrip("\ufeff \t\r\n"))
        except ElementTree.ParseError as exc:
            raise TistoryApiError("sitemap.xml 을 XML 로 해석하지 못했습니다.", detail=str(exc)) from exc

        page_locs: list[str] = []
        nested_locs: list[str] = []
        for child in root:
            name = _local_name(child.tag)
            if name not in ("url", "sitemap"):
                continue
            loc = _first_loc(child)
            if loc is None:
                continue
            if name == "sitemap":
                nested_locs.append(loc)
            else:
                page_locs.append(loc)
        return page_locs, nested_locs

    async def _fetch_nested(self, sitemap_url: str) -> Optional[str]:
        """중첩 sitemap 문서를 가져온다. 같은 블로그의 문서만 따라간다.

        클라이언트의 공개 메서드를 쓰므로 속도 제한과 재시도 정책이 다른 요청과
        동일하게 적용된다.
        """
        path = self._same_host_path(sitemap_url)
        if path is None:
            logger.warning("다른 호스트의 sitemap 은 따라가지 않습니다: %s", sitemap_url)
            return None
        try:
            document = await self._client.fetch_text(path)
        except TistoryApiError:
            logger.warning("중첩 sitemap 요청에 실패했습니다: %s", sitemap_url, exc_info=True)
            return None
        if document is None:
            logger.warning("중첩 sitemap 응답이 정상이 아닙니다: %s", sitemap_url)
        return document

    def _same_host_path(self, url: str) -> Optional[str]:
        """같은 블로그의 주소면 경로 부분만, 아니면 None 을 돌려준다."""
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc.lower() != self._host:
            return None
        return parsed.path or "/"

    def _entry_id_from_loc(self, loc: str) -> Optional[int]:
        """sitemap 의 `<loc>` 값이 게시글 주소면 번호를, 아니면 None 을 돌려준다."""
        parsed = urlparse(loc.strip())
        if parsed.netloc and parsed.netloc.lower() != self._host:
            return None
        matched = ENTRY_PATH_PATTERN.match(parsed.path or "")
        return int(matched.group(1)) if matched else None

    def _parse_entry_id(self, url_or_id: str) -> int:
        """사용자 입력에서 게시글 번호를 뽑는다."""
        text = (url_or_id or "").strip()
        if not text:
            raise ValidationError("게시글 번호 또는 주소를 입력하세요.")

        if text.isdigit():
            return _positive_entry_id(text, url_or_id)

        # 스킴이 없는 `blog.tistory.com/723` 도 주소로 해석되게 보정한다.
        parsed = urlparse(text if "//" in text else f"//{text}")
        if parsed.netloc and parsed.netloc.lower() != self._host:
            raise ValidationError(f"이 블로그의 주소가 아닙니다: {url_or_id}")

        matched = ENTRY_PATH_PATTERN.match(parsed.path or "")
        if matched is None:
            raise ValidationError(f"주소에서 게시글 번호를 찾지 못했습니다: {url_or_id}")
        return _positive_entry_id(matched.group(1), url_or_id)

    @staticmethod
    def _extract_title(html: str) -> Optional[str]:
        """게시글 HTML 에서 제목을 뽑는다. og:title 을 먼저 보고 없으면 title 태그를 쓴다."""
        soup = BeautifulSoup(html, HTML_PARSER)

        meta = soup.find("meta", attrs={"property": OG_TITLE_PROPERTY})
        if meta is not None:
            content = str(meta.get("content") or "").strip()
            if content:
                return content

        if soup.title is not None:
            text = soup.title.get_text(strip=True)
            if text:
                return text
        return None


def _local_name(tag: str) -> str:
    """`{http://...}url` 처럼 네임스페이스가 붙은 태그에서 이름만 떼어낸다."""
    return tag.rsplit("}", 1)[-1]


def _first_loc(element: ElementTree.Element) -> Optional[str]:
    """`<url>` 또는 `<sitemap>` 아래의 첫 `<loc>` 값."""
    for child in element:
        if _local_name(child.tag) != "loc":
            continue
        text = (child.text or "").strip()
        return text or None
    return None


def _positive_entry_id(digits: str, original: str) -> int:
    """숫자 문자열을 게시글 번호로 바꾼다. 0 이하는 존재할 수 없는 번호다."""
    entry_id = int(digits)
    if entry_id <= 0:
        raise ValidationError(f"게시글 번호는 1 이상이어야 합니다: {original}")
    return entry_id
