# app/services/spam_rules.py
"""스팸 규칙 엔진.

`config/rules.yaml` 의 규칙을 댓글에 적용해 점수, 등급, 근거를 만든다.

두 가지 원칙을 지킨다.

1. 판정 결과는 언제나 설명 가능해야 한다. 적중한 규칙 id 를 그대로 남겨 화면에서
   왜 이 댓글이 스팸으로 분류됐는지 확인할 수 있게 한다.
2. 보호가 점수보다 우선한다. 블로그 운영자 본인의 댓글과 화이트리스트 대상은
   규칙에 전부 걸리더라도 정상 등급으로 되돌린다. 삭제는 되돌릴 수 없다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from ..config.rules import Rule, RulesConfig, load_rules
from ..domain.enums import SpamLevel
from ..domain.errors import ConfigurationError
from ..domain.models import SpamVerdict
from ..infrastructure.db.repositories import CommentRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import parse_user_datetime

logger = get_logger(__name__)

# 정규식을 적용할 최대 문자 수. 매크로가 만든 초장문 본문에 역참조가 많은 패턴을
# 그대로 돌리면 한 건에서 수십 초가 걸릴 수 있어(ReDoS) 앞부분만 검사한다.
MAX_REGEX_INPUT_LENGTH = 20_000

# 분당 폭주 판정에 쓰는 버킷 키 형식. 저장 기준과 같은 UTC 로 잡는다.
MINUTE_BUCKET_FORMAT = "%Y-%m-%d %H:%M"

# 화이트리스트로 보호된 댓글의 근거 표식
WHITELIST_REASON = "whitelisted"

# 재채점 결과를 몇 건씩 나눠 커밋할지. 한 트랜잭션이 쓰기 잠금을 오래 쥐면
# 동시에 진행 중인 삭제 작업의 결과 기록이 잠금 대기로 실패한다.
_RESCORE_CHUNK = 500


class CommentLike(Protocol):
    """규칙 평가에 필요한 최소 속성.

    수집 직후의 :class:`~app.domain.models.ParsedComment` 와 DB 의
    ``Comment`` ORM 객체를 모두 같은 방식으로 다루기 위한 구조적 타입이다.
    """

    comment_id: int
    nickname: str
    content: str
    written_at: datetime
    is_secret: bool
    is_reply: bool
    is_admin: bool


def _as_utc(moment: datetime) -> datetime:
    """비교용으로 UTC aware datetime 을 만든다.

    naive 값은 저장 규약대로 UTC 로 해석한다. 사용자 시간대 해석은 설정 문자열
    쪽에서만 일어난다.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _truncate(text: str) -> str:
    """정규식 검사 대상 문자열을 안전한 길이로 자른다."""
    if len(text) <= MAX_REGEX_INPUT_LENGTH:
        return text
    return text[:MAX_REGEX_INPUT_LENGTH]


def minute_bucket(moment: datetime) -> str:
    """UTC 기준 분 단위 버킷 키."""
    return _as_utc(moment).strftime(MINUTE_BUCKET_FORMAT)


class SpamRuleEngine:
    """규칙 설정 하나에 대응하는 평가기.

    설정은 생성 시점에 고정된다. 규칙을 바꾸려면 새 엔진을 만든다. 평가 도중에
    기준이 바뀌면 같은 배치 안에서 서로 다른 잣대가 섞이기 때문이다.
    """

    def __init__(self, config: RulesConfig, tz_name: str) -> None:
        self._config = config
        self._tz_name = tz_name
        # 규칙별 작성 시각 범위를 UTC 로 미리 변환해 둔다.
        self._windows: dict[str, tuple[Optional[datetime], Optional[datetime]]] = {
            rule.id: self._resolve_window(rule) for rule in config.rules
        }
        self._whitelist_nicknames = set(config.whitelist.nicknames)
        self._whitelist_comment_ids = set(config.whitelist.comment_ids)

    @property
    def config(self) -> RulesConfig:
        """평가에 사용 중인 설정."""
        return self._config

    def _resolve_window(self, rule: Rule) -> tuple[Optional[datetime], Optional[datetime]]:
        """규칙의 ``written_between`` 을 UTC 경계로 바꾼다.

        설정 파일의 시각 문자열은 한국시간이고 저장된 작성 시각은 UTC 이므로,
        비교 전에 반드시 같은 기준으로 맞춘다.
        """
        window = rule.when.written_between
        if window is None:
            return (None, None)
        try:
            start = parse_user_datetime(window.start, tz_name=self._tz_name)
            end = parse_user_datetime(window.end, tz_name=self._tz_name)
        except ValueError as exc:
            raise ConfigurationError(
                f"규칙 '{rule.id}' 의 written_between 시각을 해석할 수 없습니다: {exc}"
            ) from exc
        return (start, end)

    def evaluate(
        self, comment: CommentLike, *, nickname_count: int, minute_count: int
    ) -> SpamVerdict:
        """댓글 1건을 평가한다.

        Args:
            comment: 평가 대상.
            nickname_count: 같은 닉네임의 총 작성 횟수.
            minute_count: 같은 분(UTC)에 작성된 댓글 수.
        """
        score = 0
        reasons: list[str] = []
        for rule in self._config.rules:
            if self._matches(
                rule, comment, nickname_count=nickname_count, minute_count=minute_count
            ):
                score += rule.weight
                reasons.append(rule.id)

        whitelisted = self._is_whitelisted(comment)
        if whitelisted:
            reasons.append(WHITELIST_REASON)
            level = SpamLevel.NORMAL
        else:
            level = self._level_for(score)

        return SpamVerdict(
            score=score,
            level=level,
            reasons=tuple(reasons),
            whitelisted=whitelisted,
        )

    def evaluate_all(self, comments: Sequence[CommentLike]) -> dict[int, SpamVerdict]:
        """댓글 묶음을 평가한다.

        닉네임별 횟수와 분당 건수는 전체를 한 번만 훑어 집계한다. 댓글마다 다시
        세면 4천 건 규모에서 제곱 시간이 된다.
        """
        nickname_counts: Counter[str] = Counter()
        minute_counts: Counter[str] = Counter()
        buckets: list[str] = []
        for comment in comments:
            nickname_counts[comment.nickname] += 1
            bucket = minute_bucket(comment.written_at)
            buckets.append(bucket)
            minute_counts[bucket] += 1

        return {
            comment.comment_id: self.evaluate(
                comment,
                nickname_count=nickname_counts[comment.nickname],
                minute_count=minute_counts[bucket],
            )
            for comment, bucket in zip(comments, buckets)
        }

    def _level_for(self, score: int) -> SpamLevel:
        """점수를 등급으로 바꾼다. 임계값은 경계 포함이다."""
        thresholds = self._config.thresholds
        if score >= thresholds.spam:
            return SpamLevel.SPAM
        if score >= thresholds.suspicious:
            return SpamLevel.SUSPICIOUS
        return SpamLevel.NORMAL

    def _is_whitelisted(self, comment: CommentLike) -> bool:
        """삭제 대상에서 제외해야 하는 댓글인지 판정한다."""
        if comment.is_admin:
            # 운영자 본인 댓글은 설정과 무관하게 항상 보호한다.
            return True
        if comment.comment_id in self._whitelist_comment_ids:
            return True
        nickname = comment.nickname
        if nickname in self._whitelist_nicknames:
            return True
        target = _truncate(nickname)
        return any(
            pattern.search(target) for pattern in self._config.whitelist.nickname_patterns
        )

    def _matches(
        self, rule: Rule, comment: CommentLike, *, nickname_count: int, minute_count: int
    ) -> bool:
        """규칙의 모든 조건을 만족하는지 확인한다. 하나라도 어긋나면 False."""
        condition = rule.when

        if condition.written_between is not None:
            start, end = self._windows[rule.id]
            written_at = _as_utc(comment.written_at)
            if start is not None and written_at < start:
                return False
            if end is not None and written_at > end:
                return False

        if rule.nickname_pattern is not None:
            if not rule.nickname_pattern.search(_truncate(comment.nickname)):
                return False

        if rule.content_pattern is not None:
            if not rule.content_pattern.search(_truncate(comment.content)):
                return False

        if condition.content_equals is not None:
            # 파서가 남긴 앞뒤 공백 때문에 일치가 어긋나지 않도록 정리해서 비교한다.
            if comment.content.strip() != condition.content_equals.strip():
                return False

        if condition.nickname_equals is not None:
            if comment.nickname.strip() != condition.nickname_equals.strip():
                return False

        if condition.same_nickname_count_gte is not None:
            if nickname_count < condition.same_nickname_count_gte:
                return False

        if condition.comments_per_minute_gte is not None:
            if minute_count < condition.comments_per_minute_gte:
                return False

        if condition.is_secret is not None and comment.is_secret is not condition.is_secret:
            return False

        if condition.is_reply is not None and comment.is_reply is not condition.is_reply:
            return False

        return True


class SpamScoringService:
    """DB 에 저장된 댓글을 다시 채점하는 서비스."""

    def __init__(self, database: Database, rules_path: Path, tz_name: str) -> None:
        self._database = database
        self._rules_path = rules_path
        self._tz_name = tz_name

    def load_engine(self) -> SpamRuleEngine:
        """현재 규칙 파일로 엔진을 만든다.

        호출 시점마다 파일을 다시 읽는다. 사용자가 화면에서 규칙을 고친 뒤
        재기동 없이 곧바로 결과를 확인할 수 있어야 하기 때문이다.
        """
        return SpamRuleEngine(load_rules(self._rules_path), self._tz_name)

    async def rescore(self, entry_ids: Optional[Sequence[int]] = None) -> int:
        """대상 댓글을 다시 평가해 저장한다.

        Args:
            entry_ids: 특정 게시글로 한정할 때 지정한다. None 이면 전체.

        Returns:
            갱신된 댓글 수.
        """
        engine = self.load_engine()

        # 읽기 세션을 먼저 닫는다. 평가는 순수 계산이므로 DB 를 붙들고 있을 이유가 없다.
        async with self._database.session() as session:
            comments = await CommentRepository(session).all_for_scoring(entry_ids)
        if not comments:
            logger.info("채점 대상 댓글이 없습니다.")
            return 0

        verdicts = engine.evaluate_all(comments)

        # 쓰기는 여러 트랜잭션으로 나눈다. 한 트랜잭션이 오래 잠금을 쥐면 같은 시각에
        # 돌고 있는 삭제 작업의 결과 기록이 잠금 대기로 실패한다.
        updated = 0
        items = list(verdicts.items())
        for start in range(0, len(items), _RESCORE_CHUNK):
            chunk = dict(items[start : start + _RESCORE_CHUNK])
            async with self._database.session() as session:
                updated += await CommentRepository(session).apply_verdicts(chunk)

        spam_count = sum(1 for verdict in verdicts.values() if verdict.level is SpamLevel.SPAM)
        logger.info(
            "스팸 점수를 갱신했습니다: 대상 %d건, 갱신 %d건, 스팸 %d건",
            len(comments),
            updated,
            spam_count,
        )
        return updated
