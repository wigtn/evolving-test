"""W2 · 거버넌스 정책 파일과 검사기 (docs/10 W2).

정책: data/policy/sel-governance-policy@<ver>.yaml (사람이 관리, SEL은 못 씀)
검사: check(asset, bundle, ...) → Decision. Improver에는 feedback(decision)의 범주 이름만 돌려준다.
표준 라이브러리와 PyYAML만 쓴다. 동형 문자 비교용 유니코드 데이터 파일(data/confusables.txt)만 동봉.
"""
from .checker import (Decision, Hit, check, feedback, log_decision, reject_before_generation, target_allowed,
                      tool_catalog, tool_scope)
from .policy import Policy, PolicyError, allowed, allowed_fn, default, load
