---
asset_ref: skill/fi-open-items@1.4.2
kind: skill
description: 거래처별 미결 전표 조회와 합계
allowed_tools: [tool/fi-vendor-search@1.0.0, tool/fi-open-items-query@1.0.0]
---
# 미결 전표 조회

## 절차
1. 거래처를 확정한다. `fi-vendor-search`로 찾는다.
<!-- rule:disambiguation -->
2. 검색 결과가 2건 이상이면 후보(번호, 이름, 지역)를 보여주고 어느 거래처인지 묻는다. 추측해서 고르지 않는다.
<!-- rule:period_default -->
3. 사용자가 기간을 말하지 않으면 당월(YYYY-MM)로 조회하고, 답에 "당월 기준"이라고 밝힌다. 사용자가 "전체", "누적", "연간"이라고 하면 그 범위로 조회한다.
4. `fi-open-items-query`로 조회한다. ROW_LIMIT_EXCEEDED가 오면 기간을 나눠 다시 조회한다.
5. 통화별로 건수와 합계를 계산한다. 통화가 다르면 합치지 않는다.

## 답 형식
- 거래처 번호와 이름, 조회 기간, 통화별 건수·합계를 적는다.

## 예시
<!-- rule:example_currency -->
사용자: 가상거래처-0007 9월 미결 알려줘
답: 가상거래처-0007(90000007), 2026-09 기준 미결 3건. KRW 1,250,000원, USD 2건 합계 1,200.00 USD.
