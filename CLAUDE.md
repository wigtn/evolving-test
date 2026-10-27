# CLAUDE.md

## 프로젝트

ERP AI 에이전트용 SEL 샌드박스 평가 플랫폼 설계.
Phoenix trace 분석 → 합성 업무 환경·시나리오·정답 생성 → 샌드박스에서 에이전트 버전 평가.
목표는 비즈니스 영역이 직접 쓰는 셀프서비스 평가.

범위 밖: 트레이싱 계측·Collector·Phoenix 운영(타 팀), skill·prompt 개선 루프(v3 SEL 소관).

## 틀리면 안 되는 사실

- 보안 등급은 1이 가장 민감하다. "2등급 이상" = 1·2등급.
- Phoenix 사본에서 1·2등급과 등급 미표시 span은 내용이 없다(구조·메타데이터만). 3등급은 마스킹본.
- 샌드박스에서 실데이터 금지. 합성 값은 예약 범위 ID와 누가 봐도 가짜인 이름만 쓴다.
- 생성·채점·user simulator에 쓰는 LLM은 사내 LLM만.
- Deep Tracer는 설계에서 빠졌다. 실행 기록의 원천은 Aegra Runtime(Business·Personal)과 Gateway.

## 설계 불변식 (바꾸려면 PRD 개정과 리뷰 필요)

- 샌드박스 Gateway는 SAP 자격증명 없이 별도 배포한다. 운영 Gateway에 모드 플래그를 다는 방식은 금지(fail-open).
- 채점 입력은 실행기가 직접 수집한다(응답, Gateway 호출 로그, 최종 상태). Phoenix는 열람만 하고 experiments는 쓰지 않는다.
- 규칙 채점 우선. LLM judge는 보조로만, pairwise만.
- 평가 하네스를 생성 자동화보다 먼저 만든다. 첫 게이트는 M2a.
- 실행은 자동, 판단(suite 승인·표본 점검·배포)은 사람.

## 문서

- `docs/01_PRD_SEL-sandbox-eval_v0.1.md`: SEL PRD. 결정의 이유와 D1–D8은 여기서 확인한다.
- `docs/02_M0_questions_branches.md`: M0 미결 질문과 답에 따른 설계 분기. 답이 오기 전에는 이 문서의 기본 가정을 따른다.
- `docs/03_PRD_tracing_v3.1.md`: 트레이싱 PRD. 참고용.
- 현재 단계(2026-10-27): M0. v3 SEL 섹션과 `eval-job/v1` 규격을 기다리는 중.

## 작업 방식

- 한국어로 쓰고 기술 용어는 영어 그대로 쓴다.
- 모르는 건 모른다고 쓴다. 추천에는 근거와 트레이드오프를 붙인다.
- 빼는 최적화를 선호한다. end-to-end로 측정되는 이득이 없는 복잡성은 넣지 않는다.
- 외부 라이브러리·논문은 버전, 출처, 확인일을 적는다.
- 큰 개정 뒤에는 독립 리뷰어 서브에이전트로 검증한다.
- 다이어그램은 손그림 스타일(roughjs + Gaegu)로 그린다.

## 공개 저장소 규칙

- 이 저장소는 public이다. 회사명, 사내 시스템명(IAM, 클라우드 등), 조직·인물 언급, 내부 문서 링크·id를 넣지 않는다.
- 이미지 안의 텍스트도 검사한다.
- 그대로 둬도 되는 이름: SAP, ARD, Aegra, OpenAI Dots, Oracle, Deep Tracer.
- 푸시 전에 사용자 확인을 받는다.
