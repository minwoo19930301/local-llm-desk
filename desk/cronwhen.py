"""Cron 식 파싱·다음 실행 시각 계산·한국어 요약 (stdlib만 사용).

지원 문법: ``*``, ``*/n``, ``a-b``, ``a,b``, ``a-b/n``, ``a/n`` 및 월·요일 영문 3자 이름.
요일 7은 0(일요일)과 같다. 일(day-of-month)과 요일이 둘 다 제한되면 표준 cron처럼 OR.
"""
from __future__ import annotations

from datetime import datetime, timedelta

FieldSets = tuple[set[int], set[int], set[int], set[int], set[int]]

_RANGES: tuple[tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DAY_KO = ("일", "월", "화", "수", "목", "금", "토")
_MAX_DAYS = 366


def parse(expr: str) -> FieldSets | None:
    """``분 시 일 월 요일`` 다섯 칸을 정수 집합 다섯 개로 바꾼다. 잘못되면 None."""
    parsed = _parse_fields(expr)
    return parsed[0] if parsed else None


def is_valid(expr: str) -> bool:
    return _parse_fields(expr) is not None


def next_runs(expr: str, start: datetime, count: int = 50, horizon: datetime | None = None) -> list[datetime]:
    """``start`` 이후(초과) 실행 시각들. 시스템 cron과 같은 로컬 시간대의 tz-aware 값을 반환한다. 최대 366일 탐색."""
    parsed = _parse_fields(expr)
    if not parsed or count <= 0:
        return []
    (minutes, hours, doms, months, dows), dom_star, dow_star = parsed
    start = _to_local(start).replace(second=0, microsecond=0)
    limit = _to_local(horizon) if horizon else None
    out: list[datetime] = []
    day = start.date()
    for _ in range(_MAX_DAYS + 1):
        if day.month in months and _day_matches(day, doms, dows, dom_star, dow_star):
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    wall = datetime(day.year, day.month, day.day, hour, minute)
                    when = _to_local(wall)
                    if when.replace(tzinfo=None) != wall:
                        continue  # DST 전환으로 존재하지 않는 현지 시각
                    if when <= start:
                        continue
                    if limit and when > limit:
                        return out
                    out.append(when)
                    if len(out) >= count:
                        return out
        day += timedelta(days=1)
    return out


def describe(expr: str) -> str:
    """한국어 한 줄 요약. 못 요약하면 원문."""
    parsed = _parse_fields(expr)
    if not parsed:
        return (expr or "").strip()
    (minutes, hours, doms, months, dows), dom_star, dow_star = parsed
    raw = (expr or "").split()
    all_days = dom_star and dow_star and len(months) == 12
    if len(hours) == 24 and all_days:
        if len(minutes) == 60:
            return "매분"
        step = _step_of(raw[0], 60)
        if step:
            return f"{step}분마다"
    if len(minutes) == 1 and all_days:
        minute = next(iter(minutes))
        if len(hours) == 24:
            return f"매시 {minute}분"
        step = _step_of(raw[1], 24)
        if step and step > 1:
            return f"{step}시간마다"
    if len(minutes) != 1 or len(hours) != 1:
        return (expr or "").strip()
    hhmm = f"{next(iter(hours)):02d}:{next(iter(minutes)):02d}"
    if dom_star and len(months) == 12:
        return f"{_dow_label(dows, dow_star)} {hhmm}"
    if dow_star and len(doms) == 1:
        dom = next(iter(doms))
        if len(months) == 12:
            return f"매월 {dom}일 {hhmm}"
        if len(months) == 1:
            return f"{next(iter(months))}월 {dom}일 {hhmm}"
    return (expr or "").strip()


# --- 내부 ---------------------------------------------------------------


def _parse_fields(expr: str) -> tuple[FieldSets, bool, bool] | None:
    """필드 집합과 '일/요일 칸이 *로 시작하는지' 플래그를 함께 돌려준다."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    sets: list[set[int]] = []
    for idx, (part, (lo, hi)) in enumerate(zip(parts, _RANGES)):
        values = _parse_field(part, lo, hi, idx)
        if values is None:
            return None
        if idx == 4 and 7 in values:
            values.discard(7)
            values.add(0)
        sets.append(values)
    fields: FieldSets = (sets[0], sets[1], sets[2], sets[3], sets[4])
    return fields, parts[2].startswith("*"), parts[4].startswith("*")


def _parse_field(text: str, lo: int, hi: int, idx: int) -> set[int] | None:
    out: set[int] = set()
    for piece in text.split(","):
        values = _parse_piece(piece.strip().lower(), lo, hi, idx)
        if values is None:
            return None
        out |= values
    return out or None


def _parse_piece(piece: str, lo: int, hi: int, idx: int) -> set[int] | None:
    if not piece:
        return None
    base, has_step, step_text = piece.partition("/")
    step = 1
    if has_step:
        if not step_text.isdigit() or int(step_text) < 1:
            return None
        step = int(step_text)
    if base == "*":
        first, last = lo, hi
    elif "-" in base:
        a, _, b = base.partition("-")
        first, last = _atom(a, idx), _atom(b, idx)
    else:
        first = _atom(base, idx)
        last = hi if has_step else first  # "5/15" == "5-max/15" (Vixie cron)
    if first is None or last is None or first < lo or last > hi or first > last:
        return None
    return set(range(first, last + 1, step))


def _atom(text: str, idx: int) -> int | None:
    if text.isdigit():
        return int(text)
    if idx == 3 and text in _MONTHS:
        return _MONTHS.index(text) + 1
    if idx == 4 and text in _DAYS:
        return _DAYS.index(text)
    return None


def _day_matches(day, doms: set[int], dows: set[int], dom_star: bool, dow_star: bool) -> bool:
    dow = (day.weekday() + 1) % 7  # Monday=0 → Sunday=0 기준으로 변환
    dom_ok = day.day in doms
    dow_ok = dow in dows
    if dom_star or dow_star:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def _to_local(when: datetime) -> datetime:
    # Resolve each date through the OS so future dates use their own DST offset.
    # A naive datetime is a local wall time, as it is for the system cron daemon.
    return when.astimezone()


def _step_of(field: str, span: int) -> int | None:
    """``*/n`` 형태면 n, 그 외엔 None."""
    if field.startswith("*/") and field[2:].isdigit():
        step = int(field[2:])
        return step if 1 <= step < span else None
    return None


def _dow_label(dows: set[int], dow_star: bool) -> str:
    if dow_star or len(dows) == 7:
        return "매일"
    if dows == {1, 2, 3, 4, 5}:
        return "평일"
    if dows == {0, 6}:
        return "주말"
    names = "·".join(_DAY_KO[d] for d in sorted(dows))
    return f"매주 {names}요일"
