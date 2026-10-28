"""W1 · 편집 대상 주소 · 편집 명령 · 적용기 (docs/10 W1, docs/11 4장).

LLM은 자산 파일을 다시 쓰지 않고 "이 주소를 이렇게 바꿔라"라는 편집 명령만 낸다.
적용기는 허용 주소만 반영하고, 나머지는 거부 코드와 함께 묶음 전체를 거부한다.
표준 라이브러리와 PyYAML만 쓴다(사내 환경에서 새 패키지를 설치할 수 없다).
"""
from .address import Address, parse_address
from .apply import EditError, EditResult, apply_bundle
