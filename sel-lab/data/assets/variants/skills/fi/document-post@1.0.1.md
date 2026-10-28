---
kind: skill
metadata:
  ref: asset://skill/fi/document-post@1.0.1
  grade: 2
description: 일반 전표 전기
allowed_tools:
- asset://tool/fi/document-post@1.0.0
---
# 전표 전기

## 절차
1. 사용자 요청에서 회사코드, 전기일, 차변·대변 라인을 정리한다. 차변 합계와 대변 합계가 같아야 한다.
2. `fi-document-post`를 dry_run=true로 불러 검증한다. 오류가 있으면 사용자에게 알리고 멈춘다.
4. 승인되면 approval_token을 넣어 본실행한다.
