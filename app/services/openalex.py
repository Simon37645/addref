from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests


class OpenAlexError(RuntimeError):
    pass


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
DEFAULT_RETRY_COUNT = 3


@dataclass(slots=True)
class OpenAlexSettings:
    api_key: str
    email: str = ""
    timeout_seconds: int = 20


@dataclass(slots=True)
class SourceMetrics:
    display_name: str
    issn_l: str
    impact_factor: float | None


class OpenAlexClient:
    base_url = "https://api.openalex.org"

    def __init__(self, settings: OpenAlexSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self._build_user_agent()})
        self._source_cache: dict[str, SourceMetrics | None] = {}

    def get_source_metrics(self, issn: str) -> SourceMetrics | None:
        normalized = _normalize_issn(issn)
        if not normalized:
            return None
        cache_key = normalized.upper()
        if cache_key in self._source_cache:
            return self._source_cache[cache_key]

        response = self._get(
            f"/sources/issn:{cache_key}",
            params={"select": "display_name,issn_l,summary_stats"},
        )
        if response.status_code == 404:
            self._source_cache[cache_key] = None
            return None
        if response.status_code >= 400:
            raise OpenAlexError(
                f"OpenAlex request failed with {response.status_code}: {response.text[:240]}"
            )

        payload = response.json()
        metrics = SourceMetrics(
            display_name=str(payload.get("display_name", "")).strip(),
            issn_l=str(payload.get("issn_l", "")).strip(),
            impact_factor=_to_float((payload.get("summary_stats") or {}).get("2yr_mean_citedness")),
        )
        self._source_cache[cache_key] = metrics
        if metrics.issn_l:
            self._source_cache[_normalize_issn(metrics.issn_l).upper()] = metrics
        return metrics

    def _get(self, path: str, *, params: dict[str, str]) -> requests.Response:
        request_params = dict(params)
        api_key = str(self.settings.api_key or "").strip()
        email = str(self.settings.email or "").strip()
        if api_key:
            request_params["api_key"] = api_key
        if email:
            request_params["mailto"] = email

        last_error: Exception | None = None
        for attempt in range(DEFAULT_RETRY_COUNT + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}{path}",
                    params=request_params,
                    timeout=self.settings.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= DEFAULT_RETRY_COUNT:
                    raise OpenAlexError(f"OpenAlex request failed: {exc}") from exc
                self._sleep_before_retry(attempt)
                continue

            if response.status_code < 400 or response.status_code == 404:
                return response

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < DEFAULT_RETRY_COUNT:
                last_error = OpenAlexError(
                    f"OpenAlex request failed with {response.status_code}: {response.text[:240]}"
                )
                self._sleep_before_retry(attempt)
                continue

            return response

        raise OpenAlexError(f"OpenAlex request failed: {last_error}")

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(min(4.0, 0.5 * (2**attempt)))

    def _build_user_agent(self) -> str:
        email = str(self.settings.email or "").strip()
        if email:
            return f"addref-openalex/1.0 ({email})"
        return "addref-openalex/1.0"


def _normalize_issn(value: str) -> str:
    cleaned = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    if len(cleaned) != 8:
        return ""
    return f"{cleaned[:4]}-{cleaned[4:]}"


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except Exception:  # noqa: BLE001
        return None
