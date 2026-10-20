from __future__ import annotations

import json
from typing import Any
import requests

from .config import Settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JEV_HOSTED_URL = "https://jevtypesafeai.com/api/v1/decide"
JEV_OFFICIAL_URL = "https://api.typesafe.ai/v1/systemone"


def _bounded(value: Any, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def deepseek_context(state: dict, settings: Settings, post=requests.post) -> dict:
    if not settings.openrouter_api_key:
        return {"status": "disabled", "allow_new_trade": False,
                "reason_code": "MISSING_OPENROUTER_KEY", "summary": "OpenRouter key is not configured"}
    schema = {"name": "eurusd_risk_review", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "allow_new_trade": {"type": "boolean"}, "event_risk": {"type": "number"},
            "regime": {"type": "string"}, "size_multiplier": {"type": "number"},
            "reason_code": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["allow_new_trade", "event_risk", "regime", "size_multiplier", "reason_code", "summary"]}}
    payload = {
        "model": settings.openrouter_model, "temperature": 0,
        "messages": [
            {"role": "system", "content": ("You are an EUR/USD paper-trading risk filter. Assess candidate coherence from only the supplied evidence. "
                "Never invent prices, claim current news access, or reverse the candidate. Reject uncertainty. "
                "Return exactly one JSON object with all six keys: allow_new_trade (boolean), event_risk (number 0..1; use evidence uncertainty), "
                "regime (string), size_multiplier (number 0..1), reason_code (short string), summary (string). No other keys.")},
            {"role": "user", "content": json.dumps(state, separators=(",", ":"))}],
        "response_format": {"type": "json_object"},
    }
    try:
        response = post(OPENROUTER_URL, headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/shiyu-coder/Kronos", "X-Title": "Kronos EURUSD Paper Trader"},
            json=payload, timeout=60)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content") or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Retry once with the same portable JSON mode.
            fallback = dict(payload)
            fallback["response_format"] = {"type": "json_object"}
            response = post(OPENROUTER_URL, headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/shiyu-coder/Kronos", "X-Title": "Kronos EURUSD Paper Trader"},
                json=fallback, timeout=60)
            response.raise_for_status()
            data = json.loads(response.json()["choices"][0]["message"]["content"])
        return {"status": "ok", "model": settings.openrouter_model,
                "allow_new_trade": bool(data["allow_new_trade"]),
                "event_risk": _bounded(data["event_risk"], 0, 1), "regime": str(data["regime"])[:80],
                "size_multiplier": _bounded(data["size_multiplier"], 0, 1),
                "reason_code": str(data["reason_code"])[:80], "summary": str(data["summary"])[:300]}
    except Exception as exc:
        return {"status": "error", "allow_new_trade": False, "event_risk": 1.0,
                "size_multiplier": 0.0, "reason_code": "DEEPSEEK_UNAVAILABLE",
                "summary": f"Risk review failed: {type(exc).__name__}"}


def jev_decision(state: dict, settings: Settings, post=requests.post) -> dict:
    if not settings.jev_api_key:
        return {"status": "disabled", "choice": "HOLD", "reason": "Jev key is not configured"}
    payload = {"model": settings.jev_model, "state": state, "questions": {
        "action": {"type": "choice", "instructions": "Choose the action supported by the supplied candidate and risk evidence.",
                   "criteria": {"ENTER_LONG": "Candidate is LONG and evidence is strong.",
                                "ENTER_SHORT": "Candidate is SHORT and evidence is strong.",
                                "HOLD": "Evidence is weak, conflicting, risky, or candidate is FLAT."}},
        "allow_trade": {"type": "noul", "instructions": "Is there enough evidence to allow this paper trade?"}}}
    try:
        url = JEV_HOSTED_URL if settings.jev_api_key.startswith("jv_live_") else JEV_OFFICIAL_URL
        response = post(url, headers={"Authorization": f"Bearer {settings.jev_api_key}", "Content-Type": "application/json"},
                        json=payload, timeout=45)
        response.raise_for_status()
        answers = response.json()["answers"]
        action, allow = answers["action"], answers["allow_trade"]
        choice = str(action.get("choice", "HOLD"))
        if choice not in {"ENTER_LONG", "ENTER_SHORT", "HOLD"}:
            choice = "HOLD"
        return {"status": "ok", "model": settings.jev_model, "choice": choice,
                "confidence": _bounded(action.get("confidence", 0), 0, 1),
                "probabilities": action.get("probabilities", {}),
                "allow_trade": float(allow.get("noul", 0)) >= 0.5,
                "allow_probability": _bounded(allow.get("noul", 0), 0, 1)}
    except Exception as exc:
        return {"status": "error", "choice": "HOLD", "allow_trade": False,
                "reason": f"Typed review failed: {type(exc).__name__}"}


def review_candidate(signal, candle_timestamp: str, settings: Settings) -> dict:
    if not settings.ai_enabled or signal.side == "FLAT":
        return {"mode": settings.ai_enforcement, "deepseek": {"status": "not_run"},
                "jev": {"status": "not_run", "choice": "HOLD"}}
    state = {"instrument": "EURUSD", "timeframe": settings.interval, "as_of": candle_timestamp,
             "candidate": signal.to_dict(), "constraints": {"paper_trade": True, "may_reverse": False,
                                                               "time_exit_bars": signal.time_exit_bars}}
    deepseek = deepseek_context(state, settings)
    jev = jev_decision({**state, "deepseek_review": deepseek}, settings)
    return {"mode": settings.ai_enforcement, "deepseek": deepseek, "jev": jev}
