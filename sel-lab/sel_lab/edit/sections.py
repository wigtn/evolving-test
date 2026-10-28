"""Markdown 본문의 ATX 제목 트리. 코드 펜스 안의 '#'는 제목이 아니다. 밑줄형(Setext) 제목은 주소 공간 밖이다.

섹션의 직접 본문 = 제목 다음 줄부터 다음 제목(레벨 무관) 직전까지. 하위 섹션은 포함하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Heading:
    level: int
    title: str
    line: int
    path: tuple[str, ...]


def _title(raw: str | None) -> str:
    t = (raw or "").strip()
    t = re.sub(r"[ \t]+#+[ \t]*$", "", t)    # 닫는 '#' 제거
    return "" if re.fullmatch(r"#*", t) else t


def headings(lines: list[str]) -> list[Heading]:
    out, stack, fence = [], [], None
    for i, line in enumerate(lines):
        f = FENCE.match(line)
        if f:
            mark = f.group(1)[0]
            if fence is None:
                fence = mark
            elif fence == mark:
                fence = None
            continue
        if fence is not None:
            continue
        m = ATX.match(line)
        if not m:
            continue
        level, title = len(m.group(1)), _title(m.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        out.append(Heading(level, title, i, tuple(t for _, t in stack)))
    return out


def structure(lines: list[str]) -> list[tuple[int, tuple[str, ...]]]:
    return [(h.level, h.path) for h in headings(lines)]


class SectionError(LookupError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def block(lines: list[str], path: tuple[str, ...]) -> tuple[int, int]:
    """섹션 직접 본문의 [시작, 끝) 줄 범위."""
    hs = headings(lines)
    if path == ():
        return 0, (hs[0].line if hs else len(lines))
    hit = [h for h in hs if h.path == path]
    if not hit:
        raise SectionError("E_ADDR_NOT_FOUND", f"섹션 없음: {'/'.join(path)}")
    if len(hit) > 1:
        raise SectionError("E_ADDR_AMBIGUOUS", f"같은 제목 경로가 {len(hit)}개: {'/'.join(path)}")
    h = hit[0]
    nxt = [x.line for x in hs if x.line > h.line]
    return h.line + 1, (nxt[0] if nxt else len(lines))


def all_paths(lines: list[str]) -> list[tuple[str, ...]]:
    return [()] + [h.path for h in headings(lines)]
