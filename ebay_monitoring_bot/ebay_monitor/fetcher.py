from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from .config import FetchSettings, SearchSettings


LOGGER = logging.getLogger("ebay_monitor")


@dataclass
class SessionIdentity:
    identity_id: str
    proxy_url: str
    cookies: list[dict[str, Any]]
    user_agent: str
    locale: str
    storage_state_path: str
    created_at: float
    refreshed_at: float
    last_used_at: float = 0.0
    request_count: int = 0
    consecutive_failures: int = 0
    status: str = "healthy"


class SessionIdentityManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def identity_path(self, identity_id: str) -> Path:
        return self.directory / f"{identity_id}.json"

    def storage_state_path(self, identity_id: str) -> Path:
        return self.directory / f"{identity_id}_storage_state.json"

    def profile_path(self, identity_id: str) -> Path:
        path = self.directory / f"{identity_id}_profile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load(self, identity_id: str) -> SessionIdentity | None:
        path = self.identity_path(identity_id)
        if not path.exists():
            return None
        return SessionIdentity(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, identity: SessionIdentity) -> None:
        path = self.identity_path(identity.identity_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(identity), indent=2), encoding="utf-8")
        temporary.replace(path)


def challenge_detected(response: Response) -> bool:
    if response.status_code in {401, 403, 429}:
        return True
    preview = response.text[:15000].casefold()
    signals = (
        "captcha",
        "pardon our interruption",
        "verify you are human",
        "access denied",
        "security measure",
        "unusual traffic",
    )
    return any(signal in preview for signal in signals)


def import_cookies(session: AsyncSession, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(
            name=name,
            value=value,
            domain=cookie.get("domain") or "",
            path=cookie.get("path") or "/",
            secure=bool(cookie.get("secure", False)),
        )


def export_cookies(session: AsyncSession) -> list[dict[str, Any]]:
    jar = getattr(session.cookies, "jar", None)
    if jar is None:
        return []
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
                "httpOnly": str(getattr(cookie, "_rest", {}).get("HttpOnly", "")).casefold()
                == "true",
            }
        )
    return cookies


def create_empty_identity(settings: FetchSettings) -> SessionIdentity:
    now = time.time()
    return SessionIdentity(
        identity_id=settings.identity_id,
        proxy_url=settings.proxy_url,
        cookies=[],
        user_agent="",
        locale=settings.locale,
        storage_state_path="",
        created_at=now,
        refreshed_at=now,
    )


async def create_identity_with_camoufox(settings: FetchSettings) -> SessionIdentity:
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as exc:
        raise RuntimeError(
            "Camoufox is not installed. Run: pip install -r requirements.txt && python -m camoufox fetch"
        ) from exc

    manager = SessionIdentityManager(settings.session_directory)
    storage_state_path = manager.storage_state_path(settings.identity_id)
    browser_kwargs: dict[str, Any] = {
        "headless": settings.camoufox_headless,
        "locale": settings.locale,
        "block_images": True,
        "block_webrtc": True,
        "persistent_context": True,
        "user_data_dir": str(manager.profile_path(settings.identity_id)),
    }
    if settings.proxy_url:
        browser_kwargs["proxy"] = {"server": settings.proxy_url}

    async with AsyncCamoufox(**browser_kwargs) as context:
        page = await context.new_page()
        await page.goto(settings.homepage_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(settings.camoufox_wait_ms)
        user_agent = await page.evaluate("navigator.userAgent")
        cookies = await context.cookies()
        await context.storage_state(path=str(storage_state_path))

    now = time.time()
    return SessionIdentity(
        identity_id=settings.identity_id,
        proxy_url=settings.proxy_url,
        cookies=cookies,
        user_agent=user_agent,
        locale=settings.locale,
        storage_state_path=str(storage_state_path),
        created_at=now,
        refreshed_at=now,
    )


class EbayFetchClient:
    def __init__(self, settings: FetchSettings) -> None:
        self.settings = settings
        self.manager = SessionIdentityManager(settings.session_directory)
        self.identity = self.manager.load(settings.identity_id) or create_empty_identity(settings)
        self.session: AsyncSession | None = None
        self._request_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_requests))

    async def start(self) -> None:
        if self.settings.use_camoufox_on_start:
            self.identity = await create_identity_with_camoufox(self.settings)
            self.manager.save(self.identity)
        await self._create_session()
        if self.settings.warm_homepage_first:
            await self.get_html(self.settings.homepage_url, allow_camoufox_refresh=True)

    async def close(self) -> None:
        if self.session is not None:
            self._persist_session_cookies()
            await self.session.close()
            self.session = None

    async def _create_session(self) -> None:
        if self.session is not None:
            await self.session.close()
        kwargs: dict[str, Any] = {
            "impersonate": self.settings.impersonate,
            "timeout": max(1, self.settings.request_timeout_seconds),
            "headers": {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": f"{self.identity.locale},en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        }
        if self.identity.proxy_url:
            kwargs["proxy"] = self.identity.proxy_url
        if self.identity.user_agent:
            kwargs["headers"]["User-Agent"] = self.identity.user_agent
        self.session = AsyncSession(**kwargs)
        import_cookies(self.session, self.identity.cookies)

    def _persist_session_cookies(self) -> None:
        if self.session is None:
            return
        self.identity.cookies = export_cookies(self.session)
        self.identity.last_used_at = time.time()
        self.manager.save(self.identity)

    async def refresh_identity(self) -> None:
        self.identity = await create_identity_with_camoufox(self.settings)
        self.manager.save(self.identity)
        await self._create_session()

    async def get_html(self, url: str, *, allow_camoufox_refresh: bool = True) -> str:
        if self.session is None:
            await self._create_session()
        assert self.session is not None
        response = await self._get_with_retries(url)
        self.identity.request_count += 1
        self.identity.last_used_at = time.time()

        if challenge_detected(response):
            self.identity.consecutive_failures += 1
            self.identity.status = "challenged"
            self.manager.save(self.identity)
            if allow_camoufox_refresh and self.settings.use_camoufox_on_challenge:
                await self.refresh_identity()
                assert self.session is not None
                response = await self._get_with_retries(url)
                self.identity.request_count += 1
            if challenge_detected(response):
                raise RuntimeError(f"eBay challenge or block detected for {url} (HTTP {response.status_code})")

        response.raise_for_status()
        self.identity.consecutive_failures = 0
        self.identity.status = "healthy"
        self._persist_session_cookies()
        return response.text

    async def _get_with_retries(self, url: str) -> Response:
        attempts = max(1, self.settings.request_retries + 1)
        for attempt in range(1, attempts + 1):
            assert self.session is not None
            try:
                async with self._request_semaphore:
                    return await self.session.get(url)
            except (CurlTimeout, CurlConnectionError) as exc:
                if attempt >= attempts:
                    raise
                delay = max(0.0, self.settings.retry_backoff_seconds) * attempt
                LOGGER.warning(
                    "Fetch attempt %s/%s failed for %s: %s; retrying in %.1fs.",
                    attempt,
                    attempts,
                    url,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    def search_url(self, keyword: str, *, buying_option: str, page: int, limit: int) -> str:
        params: dict[str, Any] = {
            "_nkw": keyword,
            "_sop": "10",
            "_ipg": max(1, min(limit, 240)),
            "_pgn": max(1, page),
        }
        option = buying_option.casefold()
        if option in {"bin", "buy_it_now", "fixed_price"}:
            params["LH_BIN"] = "1"
        elif option in {"auction", "auctions"}:
            params["LH_Auction"] = "1"
        return f"{self.settings.search_url}?{urlencode(params)}"

    async def search_html(
        self,
        keyword: str,
        *,
        buying_option: str,
        page: int,
        limit: int,
    ) -> tuple[str, str]:
        url = self.search_url(keyword, buying_option=buying_option, page=page, limit=limit)
        return await self.get_html(url), url

    async def detail_html(self, url: str) -> str:
        return await self.get_html(url)
