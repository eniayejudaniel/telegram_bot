from __future__ import annotations

import requests


def format_alert(signal, ai=None, registration=None) -> str:
    px = lambda x: "—" if x is None else f"{x:.5f}"
    metrics = signal.metrics or {}
    distribution = ""
    if metrics:
        distribution = (f"\n100-path agreement: {metrics['path_agreement']:.0%}"
                        f"\nTarget / stop probability: {metrics['target_probability']:.0%} / "
                        f"{metrics['stop_probability']:.0%}"
                        f"\nExpected terminal move: {metrics['expected_terminal_pips']:.1f} pips")
    ai_line = ""
    if ai and ai.get("deepseek", {}).get("status") == "ok":
        ds, jev = ai["deepseek"], ai.get("jev", {})
        ai_line = (f"\nAI ({ai.get('mode', 'shadow')}): DeepSeek "
                   f"{'ALLOW' if ds['allow_new_trade'] else 'VETO'}; Jev {jev.get('choice', 'HOLD')}")
    execution = ""
    if registration and registration.get("opened"):
        p = registration["position"]
        execution = (f"\nPaper position: OPENED\nSize: {p['units']:,.0f} units "
                     f"({p['standard_lots']:.2f} lots)\nRisk: ${p['risk_usd']:,.2f}")
    elif registration and signal.side != "FLAT":
        execution = f"\nPaper position: REJECTED ({registration.get('reason', 'UNKNOWN')})"
    return (f"EURUSD PAPER TRADE\n{signal.side} | {signal.regime}\n"
            f"Entry: {px(signal.entry)}\nStop: {px(signal.stop)}\nTarget: {px(signal.target)}\n"
            f"Time exit: {signal.time_exit_bars} hourly candles\n"
            f"Confidence score: {signal.confidence:.2f}{distribution}{ai_line}{execution}\n{signal.reason}")


def send_alert(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat_id, "text": text}, timeout=20)
    response.raise_for_status()
