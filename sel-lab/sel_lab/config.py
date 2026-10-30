"""설정. 키는 환경변수 또는 sel-lab/.env에서만 읽는다."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNS = Path(os.environ["SEL_RUNS"]) if os.environ.get("SEL_RUNS") else ROOT / "runs"   # 테스트는 임시 디렉터리를 쓴다
TODAY = date(2026, 10, 27)          # 가짜 세계의 기준일
CUR_PERIOD = TODAY.strftime("%Y-%m")


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)   # 셸 환경변수가 .env보다 우선


_load_dotenv()


def env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def offline() -> bool:
    return env("SEL_OFFLINE", "0") == "1" or not env("SEL_LLM_API_KEY")


def judge_enabled() -> bool:
    return env("SEL_JUDGE_ENABLED", "0") == "1" and not offline()


def chat_model(role: str):
    """role: agent | creator | testgen | improver | judge. 사내 OpenAI 호환 Gateway로만 호출한다."""
    if offline():
        return None
    from langchain_openai import ChatOpenAI

    model = env(f"SEL_{role.upper()}_MODEL") or env("SEL_MODEL")
    if not model:
        raise RuntimeError(f"SEL_{role.upper()}_MODEL 또는 SEL_MODEL을 설정하세요")
    return ChatOpenAI(base_url=env("SEL_LLM_BASE_URL"), api_key=env("SEL_LLM_API_KEY"), model=model, temperature=0,
                      request_timeout=float(env("SEL_LLM_TIMEOUT", "60")), max_retries=2)


def worker_kind() -> str:
    """create_agent(기본) | deepagents. DeepAgents는 파일 도구와 task 서브에이전트를 붙여, 서브에이전트의 도구 호출이
    실행 기록에 남지 않고 채점이 틀어진다. 그래서 기본은 도구를 우리가 준 것만 노출하는 LangChain create_agent다."""
    return env("SEL_WORKER", "create_agent")
