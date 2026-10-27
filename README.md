# evolving-test

사내 ERP AI 에이전트를 위한 **SEL(self-evolving loop) 샌드박스 평가·개선 플랫폼** 설계 문서 모음.

Phoenix에 쌓인 에이전트 trace를 분석하고, 실데이터 없는 합성 업무 환경(가짜 SAP·문서·페르소나)과 시나리오·정답을 만든다. 그 위에서 에이전트 버전을 테스트·평가(Tester·Evaluator)하고, 운영 실패 클러스터에서 skill·prompt 개선안을 만들어(Creator·Improver) 회귀 없이 나아진 후보만 사람 승인으로 등록한다. 샌드박스는 비즈니스 영역이 직접 열어 쓰는 것을 목표로 한다.

## 최종 아키텍처

![SEL 아키텍처](docs/images/sel-architecture-handdrawn.png)

| 영역 | 하는 일 | v3 SEL 역할 |
| --- | --- | --- |
| A · 수집·분석 | Phoenix trace를 run으로 조립, 실패 클러스터와 시나리오 템플릿(구조만) | Creator·Improver 트리거 |
| B · 합성 데이터 | 가짜 회사(world)와 시나리오·정답(suite). 정답은 세계에서 계산 | Tester의 평가셋 |
| C · 샌드박스 평가 | 별도 배포 Gateway 위에서 실행, 규칙 채점, 짝 비교, 회귀 판정 | Tester · Evaluator |
| L · 개선 루프 | 실패 재현 → 진단 → 증분 패치 → held-out 판정 → 사람 승인 → 자산 등록 | Creator · Improver |

일반 버전: [docs/images/sel-architecture.png](docs/images/sel-architecture.png) · 원본 spec: [docs/diagrams/](docs/diagrams/)

## 문서

| 문서 | 내용 | 상태 |
| --- | --- | --- |
| [01 · SEL 평가·개선 PRD](docs/01_PRD_SEL-sandbox-eval_v0.2.md) | A·B·C·L 네 영역, 마일스톤 M0–M4(M2c 개선 루프 v0 포함), 지표, 결정 요청 D1–D10 | v0.2 |
| [02 · M0 미결 질문과 설계 분기](docs/02_M0_questions_branches.md) | 답이 없는 질문 4개(비운영 SAP, 평가 런타임, 과거 버전 이력, v3 SEL 규격)와 답에 따른 설계 분기 | v0.2 |
| [03 · 트레이싱 PRD (참고)](docs/03_PRD_tracing_v3.1.md) | Collector 정본·사본 분리, 등급 기반 내용 제거. 트레이싱은 다른 팀 소관 | v3.1, 리뷰 결과 미반영 |

## 핵심 설계 결정

- **정답은 합성 세계에서 계산**하고, 규칙 채점을 우선한다 (τ-bench, AppWorld 방식).
- **SAP 자격증명이 없는 별도 배포 샌드박스 Gateway**를 쓴다. 운영 Gateway에 모드 플래그를 다는 방식은 플래그가 빠지면 운영으로 나간다(fail-open).
- 채점 입력은 **실행기가 직접 수집**한다. Phoenix는 열람에만 쓴다.
- **개선의 근거는 샌드박스에서 재현한 trace**로 얻는다. 운영 trace에서는 실패 클러스터와 빈도만 가져온다. 1·2등급은 내용이 없고 3등급은 마스킹본이라 진단 재료가 되지 못하기 때문이다.
- 개선 루프는 **release line 하나, 클러스터당 증분 패치 하나, dev/held-out 분리, 회귀 0 + 정책 준수 전부 통과**일 때만 채택한다.
- 구현 순서는 **하네스(M2a) → 개선 루프 v0(M2c) ∥ 하네스 완성(M2b)**이다. 하네스가 회귀를 놓치면 개선 루프가 회귀를 개선으로 채택하기 때문이다.
- 실행은 자동, 판단(suite 승인·후보 승인·배포 결정)은 사람이 한다.

## 다음 단계

1. M0 미결 질문(02 문서)의 답을 받는다. v3 SEL 섹션과 `eval-job/v1` 규격으로 역할 정의를 대조한다.
2. 세부 설계를 쓴다: 시나리오·채점 계약 → 개선 루프(후보 기록·채택 규칙) → C 샌드박스 → B 합성 세계 → A 수집·분석.
