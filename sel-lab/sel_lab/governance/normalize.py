"""거버넌스 검사용 정규화 파이프라인 (docs/10 W2-5).

순서: NFKC → 보이지 않는 문자 제거(Cf · zero-width · bidi · 한글 채움 문자 · 이체 선택자) → 한글 자모 재조합
      → casefold → UTS#39 confusables skeleton(한글 제외) → 결합 부호 제거 → casefold.
검사할 글과 사전(안전 키워드 · 도구명 · 지시문 패턴)은 모두 같은 파이프라인을 거친 뒤 비교한다.

- norm(text): 위 파이프라인. 공백 · 문장부호는 남긴다(URL · 식별자 검사용).
- compact(text): norm 뒤 글자 · 숫자만 남긴다(키워드 · 도구명 비교용. 줄 나눔 · 띄어쓰기 · '-' '_' 우회를 없앤다).
- has_invisible(text): 원문에 보이지 않는 문자가 있었는가(INFLOW-HIDDEN).

confusables 데이터: data/confusables.txt (Unicode 18.0.0, 2026-08-06, unicode.org/Public/security/latest, 2026-10-28 받음).
표준 라이브러리 우선 원칙의 예외로 데이터 파일만 동봉한다(코드 의존성 없음).
한계: 한글은 skeleton에서 뺀다(자모를 한자 등으로 바꾸면 한국어 비교가 깨진다). 한자 '丨'로 'ㅣ'를 흉내 내는 식은 못 잡는다(알려진 한계).
"""
from __future__ import annotations

import functools
import unicodedata
from pathlib import Path

DATA = Path(__file__).parent / "data" / "confusables.txt"

# Cf 범주가 아닌데 보이지 않는 문자
EXTRA_INVISIBLE = {0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x180E, 0x200B, 0x2800}

# 초성 → 종성 (자모 재조합: 호환 자모는 NFKC 뒤 초성이 되므로, 모음이 뒤따르지 않는 초성을 앞 음절의 받침으로 붙인다)
L2T = {0x1100: 0x11A8, 0x1101: 0x11A9, 0x1102: 0x11AB, 0x1103: 0x11AE, 0x1105: 0x11AF, 0x1106: 0x11B7,
       0x1107: 0x11B8, 0x1109: 0x11BA, 0x110A: 0x11BB, 0x110B: 0x11BC, 0x110C: 0x11BD, 0x110E: 0x11BE,
       0x110F: 0x11BF, 0x1110: 0x11C0, 0x1111: 0x11C1, 0x1112: 0x11C2}


def _is_hangul(cp: int) -> bool:
    return (0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F or 0xAC00 <= cp <= 0xD7A3
            or 0xA960 <= cp <= 0xA97F or 0xD7B0 <= cp <= 0xD7FF or 0xFFA0 <= cp <= 0xFFDC)


def _invisible(ch: str) -> bool:
    cp = ord(ch)
    return (unicodedata.category(ch) == "Cf" or cp in EXTRA_INVISIBLE
            or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF)


def has_invisible(text: str) -> bool:
    return any(_invisible(c) for c in text)


def strip_invisible(text: str) -> str:
    return "".join(c for c in text if not _invisible(c))


def _is_L(c): return 0x1100 <= ord(c) <= 0x1112
def _is_V(c): return 0x1161 <= ord(c) <= 0x1175
def _is_T(c): return 0x11A8 <= ord(c) <= 0x11C2
def _open_syllable(c): return 0xAC00 <= ord(c) <= 0xD7A3 and (ord(c) - 0xAC00) % 28 == 0


def recompose_jamo(text: str) -> str:
    """'ㅅㅡㅇㅇㅣㄴ' → '승인'. 입력은 NFKC를 거친 글(호환 자모가 초성 · 중성으로 바뀐 상태)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if _is_L(c) and i + 1 < n and _is_V(text[i + 1]):
            out.append(c + text[i + 1])
            i += 2
            continue
        if _is_L(c) and out and (_open_syllable(out[-1][-1]) or (len(out[-1]) == 2 and _is_V(out[-1][-1])))\
                and ord(c) in L2T and not (i + 1 < n and _is_V(text[i + 1])):
            out[-1] = out[-1] + chr(L2T[ord(c)])
            i += 1
            continue
        out.append(c)
        i += 1
    return unicodedata.normalize("NFC", "".join(out))


@functools.lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    t: dict[str, str] = {}
    for line in DATA.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        src, dst, *_ = [x.strip() for x in line.split(";")]
        if " " in src:
            continue
        t[chr(int(src, 16))] = "".join(chr(int(x, 16)) for x in dst.split())
    return t


def skeleton(text: str) -> str:
    t = _table()
    return "".join(c if _is_hangul(ord(c)) else t.get(c, c) for c in text)


def _drop_marks(text: str) -> str:
    d = unicodedata.normalize("NFD", text)
    kept = "".join(c for c in d if not (unicodedata.category(c) == "Mn" and not _is_hangul(ord(c))))
    return unicodedata.normalize("NFC", kept)


@functools.lru_cache(maxsize=4096)
def norm(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    s = strip_invisible(s)
    s = recompose_jamo(s)
    s = s.casefold()
    s = skeleton(s)
    s = _drop_marks(s)
    return s.casefold()


def _keep(c: str) -> bool:
    return unicodedata.category(c)[0] in "LN"


@functools.lru_cache(maxsize=4096)
def compact(text: str) -> str:
    return "".join(c for c in norm(text) if _keep(c))


def surface(text: str) -> str:
    """식별자 · URL 검사용: NFKC + 보이지 않는 문자 제거만 (skeleton은 숫자를 글자로 바꾸므로 쓰지 않는다)."""
    return strip_invisible(unicodedata.normalize("NFKC", text))
