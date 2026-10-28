"""편집 대상 주소.

- YAML 자산: asset://종류/ns/이름@버전 + ':' + 필드 경로 (점으로 구분)
- Markdown 자산: asset://종류/ns/이름@버전 + '#' + ATX 제목 경로 ('/'로 중첩). '#'만 있으면 첫 제목 앞 본문(제목 없는 문서는 본문 전체)
- 제목 안의 '/', '#', ':', '\\'는 '\\'로 이스케이프한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REF_PART = re.compile(r"(asset://\w+/[\w-]+/[\w-]+@[^#:/\s]+)(.*)", re.S)
FIELD_SEG = re.compile(r"[\w-]+")


class AddressError(ValueError):
    pass


@dataclass(frozen=True)
class Address:
    ref: str
    mode: str                         # "field" | "section"
    field: tuple[str, ...] = ()
    section: tuple[str, ...] = ()     # () = 첫 제목 앞 본문

    def __str__(self) -> str:
        if self.mode == "field":
            return f"{self.ref}:{'.'.join(self.field)}"
        return f"{self.ref}#{'/'.join(escape(s) for s in self.section)}"


def escape(title: str) -> str:
    return re.sub(r"([\\/#:])", r"\\\1", title)


def _split_section(rest: str) -> tuple[str, ...]:
    if rest == "":
        return ()
    parts, cur, i = [], [], 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            if i + 1 >= len(rest) or rest[i + 1] not in "\\/#:":
                raise AddressError(f"잘못된 이스케이프: {rest!r}")
            cur.append(rest[i + 1])
            i += 2
            continue
        if ch in "#:":
            raise AddressError(f"이스케이프되지 않은 '{ch}': {rest!r}")
        if ch == "/":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    if any(p.strip() == "" or p != p.strip() for p in parts):
        raise AddressError(f"빈 제목 또는 앞뒤 공백: {rest!r}")
    return tuple(parts)


def parse_address(addr: str) -> Address:
    if not isinstance(addr, str):
        raise AddressError("주소는 문자열이어야 한다")
    m = REF_PART.fullmatch(addr)
    if not m:
        raise AddressError(f"자산 참조로 시작하지 않음: {addr!r}")
    ref, rest = m.groups()
    if rest.startswith(":"):
        segs = tuple(rest[1:].split("."))
        if not segs or not all(FIELD_SEG.fullmatch(s) for s in segs):
            raise AddressError(f"잘못된 필드 경로: {rest!r}")
        return Address(ref, "field", field=segs)
    if rest.startswith("#"):
        return Address(ref, "section", section=_split_section(rest[1:]))
    raise AddressError(f"주소에 위치(':' 또는 '#')가 없음: {addr!r}")
