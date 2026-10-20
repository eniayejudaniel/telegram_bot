from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default, cast):
    value = os.getenv(name)
    return default if value in (None, "") else cast(value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    symbol: str = os.getenv("SYMBOL", "EURUSD=X")
    interval: str = os.getenv("INTERVAL", "1h")
    lookback: int = _get("LOOKBACK", 512, int)
    pred_len: int = _get("PRED_LEN", 24, int)
    sample_count: int = _get("SAMPLE_COUNT", 100, int)
    model_id: str = os.getenv("MODEL_ID", "NeoQuasar/Kronos-mini")
    tokenizer_id: str = os.getenv("TOKENIZER_ID", "NeoQuasar/Kronos-Tokenizer-2k")
    device: str = os.getenv("DEVICE", "auto")
    temperature: float = _get("TEMPERATURE", 1.0, float)
    top_p: float = _get("TOP_P", 0.9, float)
    min_edge_atr: float = _get("MIN_EDGE_ATR", 0.35, float)
    stop_atr: float = _get("STOP_ATR", 1.25, float)
    min_risk_reward: float = _get("MIN_RISK_REWARD", 1.25, float)
    spread_pips: float = _get("SPREAD_PIPS", 0.8, float)
    slippage_pips: float = _get("SLIPPAGE_PIPS", 0.2, float)
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    alert_interval_seconds: int = _get("ALERT_INTERVAL_SECONDS", 3600, int)
    min_path_agreement: float = _get("MIN_PATH_AGREEMENT", 0.55, float)
    min_target_probability: float = _get("MIN_TARGET_PROBABILITY", 0.45, float)
    min_expected_pips: float = _get("MIN_EXPECTED_PIPS", 0.0, float)
    max_dispersion_atr: float = _get("MAX_DISPERSION_ATR", 2.5, float)
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")
    jev_api_key: str = os.getenv("JEV_API_KEY", "")
    jev_model: str = os.getenv("JEV_MODEL", "jev-latest")
    ai_enabled: bool = _bool("AI_ENABLED", True)
    ai_enforcement: str = os.getenv("AI_ENFORCEMENT", "shadow").strip().lower()
    paper_balance: float = _get("PAPER_BALANCE", 25000.0, float)
    risk_per_trade: float = _get("RISK_PER_TRADE", 0.01, float)
    daily_loss_limit: float = _get("DAILY_LOSS_LIMIT", 0.03, float)
    max_drawdown: float = _get("MAX_DRAWDOWN", 0.06, float)
    trading_enabled: bool = _bool("TRADING_ENABLED", True)
    max_data_age_minutes: int = _get("MAX_DATA_AGE_MINUTES", 120, int)

    def validate(self) -> None:
        if self.lookback != 512:
            raise ValueError("LOOKBACK must be 512 for this strategy")
        if self.pred_len != 24:
            raise ValueError("PRED_LEN must be 24 for this strategy")
        if self.sample_count != 100:
            raise ValueError("SAMPLE_COUNT must be 100 for this strategy")
        if self.ai_enforcement not in {"shadow", "deepseek_veto"}:
            raise ValueError("AI_ENFORCEMENT must be shadow or deepseek_veto")
        for name in ("risk_per_trade", "daily_loss_limit", "max_drawdown"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between 0 and 1")
