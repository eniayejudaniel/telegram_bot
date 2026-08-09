from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class FetchSettings:
    homepage_url: str = "https://www.ebay.co.uk/"
    search_url: str = "https://www.ebay.co.uk/sch/i.html"
    identity_id: str = "ebay_uk_default"
    session_directory: Path = Path("data/sessions")
    impersonate: str = "chrome"
    locale: str = "en-GB"
    proxy_url: str = ""
    warm_homepage_first: bool = True
    use_camoufox_on_start: bool = False
    use_camoufox_on_challenge: bool = True
    camoufox_headless: bool = True
    camoufox_wait_ms: int = 3000
    request_timeout_seconds: int = 60
    request_retries: int = 2
    retry_backoff_seconds: float = 2.0
    max_concurrent_requests: int = 6
    cycle_timeout_seconds: int = 300


@dataclass
class SearchSettings:
    keywords: list[str]
    limit_per_page: int = 100
    max_pages_per_keyword: int = 2
    buying_options: list[str] = field(default_factory=lambda: ["bin", "auction"])
    excluded_title_terms: list[str] = field(default_factory=list)


@dataclass
class PricingSettings:
    alert_threshold_below_median: float = 0.28
    min_baseline_samples: int = 5
    uncertain_listing_alerts: bool = True
    priority_weight_mixed_lot: float = 15.0
    priority_weight_exclusive: float = 20.0
    max_alerts_per_cycle: int = 5


@dataclass
class ParserSettings:
    assumed_case_units: dict[str, int] = field(default_factory=dict)
    set_aliases: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class AppConfig:
    poll_interval_seconds: int
    marketplace_id: str
    currency: str
    database_path: Path
    dry_run: bool
    fetch: FetchSettings
    search: SearchSettings
    pricing: PricingSettings
    parser: ParserSettings
    telegram_bot_token: str
    telegram_chat_id: str


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping.")
    return data


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_config(path: str | Path) -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    data = _read_yaml(config_path)
    fetch_data = data.get("fetch") or {}
    search_data = data.get("search") or {}
    pricing_data = data.get("pricing") or {}
    parser_data = data.get("parser") or {}

    keywords = search_data.get("keywords") or []
    if not keywords:
        raise ValueError("At least one search keyword is required.")

    return AppConfig(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 600)),
        marketplace_id=str(data.get("marketplace_id", "EBAY_GB")),
        currency=str(data.get("currency", "GBP")),
        database_path=Path(data.get("database_path", "data/ebay_monitor.sqlite3")),
        dry_run=bool(data.get("dry_run", False)),
        fetch=FetchSettings(
            homepage_url=str(fetch_data.get("homepage_url", "https://www.ebay.co.uk/")),
            search_url=str(fetch_data.get("search_url", "https://www.ebay.co.uk/sch/i.html")),
            identity_id=str(fetch_data.get("identity_id", "ebay_uk_default")),
            session_directory=Path(fetch_data.get("session_directory", "data/sessions")),
            impersonate=str(fetch_data.get("impersonate", "chrome")),
            locale=str(fetch_data.get("locale", "en-GB")),
            proxy_url=_env("EBAY_PROXY_URL", str(fetch_data.get("proxy_url", ""))),
            warm_homepage_first=bool(fetch_data.get("warm_homepage_first", True)),
            use_camoufox_on_start=bool(fetch_data.get("use_camoufox_on_start", False)),
            use_camoufox_on_challenge=bool(fetch_data.get("use_camoufox_on_challenge", True)),
            camoufox_headless=bool(fetch_data.get("camoufox_headless", True)),
            camoufox_wait_ms=int(fetch_data.get("camoufox_wait_ms", 3000)),
            request_timeout_seconds=int(fetch_data.get("request_timeout_seconds", 60)),
            request_retries=int(fetch_data.get("request_retries", 2)),
            retry_backoff_seconds=float(fetch_data.get("retry_backoff_seconds", 2.0)),
            max_concurrent_requests=int(fetch_data.get("max_concurrent_requests", 6)),
            cycle_timeout_seconds=int(fetch_data.get("cycle_timeout_seconds", 300)),
        ),
        search=SearchSettings(
            keywords=[str(keyword) for keyword in keywords],
            limit_per_page=int(search_data.get("limit_per_page", 100)),
            max_pages_per_keyword=int(search_data.get("max_pages_per_keyword", 2)),
            buying_options=[
                str(option).casefold()
                for option in search_data.get("buying_options", ["bin", "auction"])
            ],
            excluded_title_terms=[
                str(term).casefold() for term in search_data.get("excluded_title_terms", [])
            ],
        ),
        pricing=PricingSettings(
            alert_threshold_below_median=float(
                pricing_data.get("alert_threshold_below_median", 0.28)
            ),
            min_baseline_samples=int(pricing_data.get("min_baseline_samples", 5)),
            uncertain_listing_alerts=bool(pricing_data.get("uncertain_listing_alerts", True)),
            priority_weight_mixed_lot=float(pricing_data.get("priority_weight_mixed_lot", 15)),
            priority_weight_exclusive=float(pricing_data.get("priority_weight_exclusive", 20)),
            max_alerts_per_cycle=max(1, int(pricing_data.get("max_alerts_per_cycle", 5))),
        ),
        parser=ParserSettings(
            assumed_case_units={
                str(key): int(value)
                for key, value in (parser_data.get("assumed_case_units") or {}).items()
            },
            set_aliases={
                str(franchise): {str(alias).casefold(): str(name) for alias, name in aliases.items()}
                for franchise, aliases in (parser_data.get("set_aliases") or {}).items()
            },
        ),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
    )
