# CLAUDE.md

## 프로젝트

ERP AI 플랫폼의 SEL(self-evolving loop) 설계.
SEL은 Agent Runtime VM(Aegra) 안에서 스케줄러 신호로 Creator → Tester → Evaluator → Improver를 실행해 공용 자산(Agent, SKILL, Prompt)을 평가하고 고친다.

- Creator: 실패 분석(앞단, finding) + 개선안 작성 + 새 SKILL 후보·평가셋
- Tester: 평가 샌드박스에서 기존·후보 버전 짝 실행
- Evaluator: 규칙 검사 → judge 채점, 버전 비교
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
- 판정은 케이스별 짝 비교. 회귀 정의는 PRD 6장 판정 규칙 하나만 쓴다.
- judge는 고정. 규칙으로 판정할 수 있는 검사는 judge 앞에 둔다.
- finding당 증분 개선안 하나, release line 하나.
- 판정용 held-out은 사람이 관리. SEL 패치는 SKILL의 평가 케이스 필드를 바꾸지 않는다.
- 평가 중 쓰기 본실행은 평가 계정 권한으로 막는다(플래그 금지, fail-open).
- 활성 전환은 사람 승인으로만. 점수만으로 finding을 fixed로 닫지 않는다.

## 미결 (단정하지 말 것)

- Q5 평가 실행 경로: 실제 도구 vs 합성 세계. 답 전에는 공통 부분(Tester·Evaluator·Creator·Improver)만 만들고 도구 경로는 어댑터로 분리한다.
- Q6 실행 기록 원천: ARD 정본 vs Phoenix 사본.
- Q7 평가 대상 에이전트.
- Q1–Q4와 v3 SEL 원문 대조: `docs/02_M0_questions_branches.md`.

## 문서

- `docs/01_PRD_SEL_v0.3.md`: SEL PRD. 결정의 이유와 결정 요청은 여기서 확인한다.
- `docs/02_M0_questions_branches.md`: 미결 질문과 답에 따른 설계 분기.
- `docs/03_PRD_tracing_v3.1.md`: 트레이싱 PRD. 참고용.
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
