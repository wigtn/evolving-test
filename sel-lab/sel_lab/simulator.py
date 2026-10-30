"""user simulator와 세션 기록.

평가에서는 케이스에 적힌 답(simulator_reply)으로 결정적으로 응답한다 (LLM 불필요).
운영 트래픽 시뮬레이션에서는 실행 결과가 틀렸을 때 persona 확률로 재요청·수정을 남긴다.
이 세션 기록이 repeat_question(재요청 · 정정)과 lab 전용 no_asset_match의 원천이다. 루프는 이벤트 종류만 쓰고 발화 내용은 Creator에 넘기지 않는다.
"""
from __future__ import annotations

import random
import re

import yaml

from .config import DATA

PERSONAS = None


def personas():
    global PERSONAS
    if PERSONAS is None:
        PERSONAS = yaml.safe_load((DATA / "personas" / "users.yaml").read_text(encoding="utf-8"))
    return PERSONAS


def user(uid: str) -> dict:
    return next(u for u in personas()["users"] if u["id"] == uid)


class CaseSimulator:
    """평가용. 에이전트가 되물으면 케이스의 답으로 한 번만 응답한다."""

    def __init__(self, case: dict):
        self.reply = case.get("expected", {}).get("simulator_reply")
        self.used = False

    def follow_up(self, last_answer: str):
        if self.reply and not self.used and ("?" in last_answer or "어느" in last_answer):
            self.used = True
            return self.reply
        return None


class TrafficSimulator(CaseSimulator):
    """운영 트래픽용. 되물으면 대답은 하지만, 결과가 틀리면 세션 이벤트를 남긴다."""


def session_events(case: dict, score: dict, uid: str, rng: random.Random) -> list[dict]:
    u = user(uid)
    ev = []
    failed = {c for c, ok in score["checks"].items() if not ok}
    # 세션 기록에는 사용자가 실제로 할 법한 말만 남긴다 (채점 항목 이름은 남기지 않는다)
    if failed & {"correct", "grounded", "clarifies_ambiguity", "scope_stated", "currency_separated"} and rng.random() < u["rerequest_prob"]:
        if "clarifies_ambiguity" in failed:
            text = "그 거래처 말고요, 다른 지점이에요. 다시 조회해 주세요"
        elif "currency_separated" in failed:
            text = "달러랑 원화가 섞였어요. 통화별로 다시 주세요"
        else:
            text = "결과가 이상해요. 다시 조회해 주세요"
        ev.append({"type": "rerequest", "utterance": text})
    if "format" in failed and rng.random() < u["correction_prob"]:
        ev.append({"type": "correction", "utterance": "금액을 48,210,000원처럼 원 단위 숫자로 고쳐 씀"})
    return ev


def recon_request(uid: str, vendors: list[str]) -> str:
    return "월말 대사: " + ", ".join(vendors) + " 이번 달 미결을 통화별로 표로 정리해줘"


def is_recon(msg: str) -> bool:
    return bool(re.match(r"^월말 대사", msg))
