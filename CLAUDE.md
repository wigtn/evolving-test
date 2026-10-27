# CLAUDE.md

## 프로젝트

ERP AI 플랫폼의 SEL(self-evolving loop) 설계.
SEL은 Agent Runtime VM(Aegra) 안에서 스케줄러 신호로 Creator → Tester → Evaluator → Improver를 실행해 공용 자산(Agent, SKILL, Prompt)을 평가하고 고친다.

- Creator: 실패 분석(앞단, finding) + 개선안 작성 + 새 SKILL 후보·평가셋
- Tester: 평가 샌드박스에서 기존·후보 버전 짝 실행
- Evaluator: 규칙 검사로 판정, 버전 비교 (judge는 승인 화면 참고용)
- Improver: 등록·상신, 승인 요청까지. 활성 전환은 사람 승인 시

범위 밖: 트레이싱 계측·Collector·Phoenix 운영(타 팀), Tool 자산 수정(개발자 작업), 메이저 버전 변경(사람만), 사람 승인 없는 활성 전환.

## 틀리면 안 되는 사실

- 보안 등급은 1이 가장 민감하다. "2등급 이상" = 1·2등급.
- Phoenix 사본에서 1·2등급과 등급 미표시 span은 내용이 없다(구조·메타데이터만). 3등급은 마스킹본.
- 자산 수정 범위: Agent는 허용 필드만, SKILL은 패치·마이너, Prompt는 persona 파생본만(judge는 SEL이 고칠 수 없다), Tool은 대상 아님.
- 자산 버전은 불변이다. 상태는 제안 → 검토 → 활성 → 폐기.
- 저장소 접근은 Database Connector 하나로 한다(Redis만 예외). 평가 작업 원장·리포트는 PostgreSQL.
- 생성·채점·진단에 쓰는 LLM은 사내 LLM만.
- Deep Tracer는 설계에서 빠졌다. 그 분석 기능은 Creator 앞단에 둔다.

## 설계 불변식 (바꾸려면 PRD 개정과 리뷰 필요)

- Tester·Evaluator를 Creator·Improver보다 먼저 만든다. M2a 게이트 통과 전에는 개선 루프를 돌리지 않는다.
- 판정은 PRD 6장 게이트 통계(G1 LCB>0 & MMI, G2 비열등, G3a/G3b, G4) 하나만 쓴다. 회귀 케이스(완전 붕괴 ≤ 허용치)는 보조 조건. finding당 판정 예산 3회.
- judge는 고정. 규칙으로 판정할 수 있는 검사는 judge 앞에 둔다.
- finding당 증분 개선안 하나, release line 하나.
- 사람 개입은 최종 승인(Admin UI) 하나 (에이전틱 원칙). 그 전 판단은 결정적 규칙, 규칙으로 판단 불가면 승인 요청 없이 종료. 예외: 직전 승인 버전으로의 자동 롤백(CAS).
- held-out = Creator가 한 번도 본 적 없는 골든셋 사례(노출 원장). SEL 패치는 SKILL의 평가 케이스 필드를 바꾸지 않는다. 목표 세트는 기준 실패 탐침(현행 버전으로 먼저 돌려 실패한 held-out)으로 코드가 고른다. 판정 예산은 gate_decision 기록 때만 차감. Creator(LLM)는 cause_class까지만 내고 케이스 종료는 코드가 정한다. 상태 전이 전체는 PRD 9.3.
- 평가 중 쓰기 본실행은 평가 계정 권한으로 막는다(플래그 금지, fail-open).
- 활성 전환은 사람 승인으로만. 점수만으로 finding을 fixed로 닫지 않는다.

## 미결 (단정하지 말 것)

- Q5 평가 실행 경로: 실제 도구 vs 합성 세계. 답 전에는 공통 부분(Tester·Evaluator·Creator·Improver)만 만들고 도구 경로는 어댑터로 분리한다.
- Q6 실행 기록 원천: v0.10부터 Phoenix를 별도 VM에 띄우고 계측이 OTLP로 직접 보냄(OTel Collector 없음). SEL은 Phoenix API를 VM 간 조회, 등급별 내용 제거는 계측 SDK로. ARD 정본 적재는 Q9로 미결.
- Q7 평가 대상 에이전트: v0.9에서 "공용 자산만"으로 진행(설계자 확인 대기). Business·Personal 업무는 Runtime 밖 프론티어 도구, 운영 신호는 게이트 기록·세션 기록뿐, 운영 finding은 카나리 전엔 PARKED, R6만 VERIFIED까지.
- Q1–Q4와 v3 SEL 원문 대조: `docs/02_M0_questions_branches.md`.

## 문서

- `docs/01_PRD_SEL_v0.10.md`: SEL PRD. 결정의 이유와 결정 요청은 여기서 확인한다.
- `docs/02_M0_questions_branches.md`: 미결 질문과 답에 따른 설계 분기.
- `docs/03_PRD_tracing_v3.1.md`: 트레이싱 PRD. 참고용.
- `docs/04_SEL_e2e_examples.md`: e2e 개선 예시 14개 (PRD 아님. SEL 애플리케이션 설계 용어).
- `docs/05_SEL_design_review.md`: 설계 검토 기록. `docs/06_SEL_detailed_design_plan.md`: 스케줄러 판단 로직, 컴포넌트, 빌드 순서.
- `sel-lab/`: SEL 실험장. 가짜 데이터는 `python3 sel-lab/gen/build.py`로 재생성. `data/ground_truth/`와 `data/defects/catalog.yaml`은 채점용 정답이라 루프 코드에서 읽지 않는다. 키는 `.env`(커밋 금지).
- `docs/diagrams/`: 다이어그램 원본 spec. 그림을 고칠 때는 png가 아니라 spec을 고쳐 다시 렌더한다.

## 작업 방식

- 한국어로 쓰고 기술 용어는 영어 그대로 쓴다.
- 모르는 건 모른다고 쓴다. 가정을 사실처럼 그림이나 문서에 넣지 않는다. 추천에는 근거와 트레이드오프를 붙인다.
- 빼는 최적화를 선호한다. end-to-end로 측정되는 이득이 없는 복잡성은 넣지 않는다.
- 외부 라이브러리·논문은 버전, 출처, 확인일을 적는다.
- 큰 개정 뒤에는 독립 리뷰어 서브에이전트로 검증한다.
- 다이어그램은 손그림 스타일로 그린다.

## 공개 저장소 규칙

- 이 저장소는 public이다. 회사명, 사내 시스템명(IAM, 클라우드, 포털 등), 조직·인물 언급, 내부 문서 링크·id를 넣지 않는다.
- 이미지 안의 텍스트도 검사한다.
- 그대로 둬도 되는 이름: SAP, ARD, Aegra, OpenAI Dots, Oracle, Deep Tracer.
- 푸시 전에 사용자 확인을 받는다.
