---
asset_ref: skill/fi-gl-balance@1.1.0
kind: skill
description: 계정과목 월말 잔액 조회
allowed_tools: [tool/fi-gl-balance-query@1.2.0]
---
# 계정 잔액 조회

## 절차
1. 계정과목 번호와 회사코드를 확정한다.
2. 조회 월을 YYYY-MM으로 바꿔 `fi-gl-balance-query`를 부른다. "9월"이면 올해 9월이다.
3. 잔액과 통화를 답한다.
