from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests


class OpenAICompatError(RuntimeError):
    pass


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
DEFAULT_RETRY_COUNT = 3


@dataclass(slots=True)
class OpenAISettings:
    base_url: str
    api_key: str
    model: str
    api_mode: str = "auto"
    timeout_seconds: int = 90


class OpenAICompatClient:
    def __init__(self, settings: OpenAISettings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def complete_text(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        modes = self._resolve_modes()
        last_error: Exception | None = None

        for mode in modes:
            try:
                text = self._complete_text(mode, system_prompt, user_prompt)
                return {"mode": mode, "text": text}
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        raise OpenAICompatError(f"OpenAI-compatible request failed: {last_error}") from last_error

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        modes = self._resolve_modes()
        last_error: Exception | None = None

        for mode in modes:
            try:
                text = self._complete_text(mode, system_prompt, user_prompt)
                data = _extract_json_payload(text)
                if not isinstance(data, dict):
                    raise OpenAICompatError("Model did not return a JSON object.")
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        raise OpenAICompatError(f"OpenAI-compatible request failed: {last_error}") from last_error

    def _resolve_modes(self) -> list[str]:
        mode = (self.settings.api_mode or "auto").strip().lower()
        if mode == "responses":
            return ["responses"]
        if mode in {"chat", "chat_completions", "v1/chat/completions"}:
            return ["chat_completions"]
        return ["chat_completions", "responses"]

    def _complete_text(self, mode: str, system_prompt: str, user_prompt: str) -> str:
        url = self._build_url(mode)
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

        if mode == "responses":
            payload = {
                "model": self.settings.model,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_prompt}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    },
                ],
                "temperature": 0.2,
            }
        else:
            payload = {
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            }

        response = self._post_with_retry(
            url=url,
            headers=headers,
            payload=payload,
            mode=mode,
        )

        body = response.json()
        text = self._extract_text(body, mode)
        if not text:
            raise OpenAICompatError(f"{mode} response did not contain readable text.")
        return text

    def _post_with_retry(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        mode: str,
    ) -> requests.Response:
        last_error: Exception | None = None

        for attempt in range(DEFAULT_RETRY_COUNT + 1):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= DEFAULT_RETRY_COUNT:
                    raise OpenAICompatError(f"{mode} request failed: {exc}") from exc
                self._sleep_before_retry(attempt)
                continue

            if response.status_code < 400:
                return response

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < DEFAULT_RETRY_COUNT:
                last_error = OpenAICompatError(
                    f"{mode} request failed with {response.status_code}: {response.text[:400]}"
                )
                self._sleep_before_retry(attempt)
                continue

            raise OpenAICompatError(
                f"{mode} request failed with {response.status_code}: {response.text[:400]}"
            )

        raise OpenAICompatError(f"{mode} request failed: {last_error}")

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(min(5.0, 0.8 * (2**attempt)))

    def _build_url(self, mode: str) -> str:
        base_url = (self.settings.base_url or "").strip().rstrip("/")
        if not base_url:
            raise OpenAICompatError("Base URL is required.")
        if base_url.endswith("/chat/completions"):
            if mode == "chat_completions":
                return base_url
            return base_url[: -len("/chat/completions")] + "/responses"
        if base_url.endswith("/responses"):
            if mode == "responses":
                return base_url
            return base_url[: -len("/responses")] + "/chat/completions"
        if base_url.endswith("/v1"):
            suffix = "responses" if mode == "responses" else "chat/completions"
            return f"{base_url}/{suffix}"
        suffix = "/v1/responses" if mode == "responses" else "/v1/chat/completions"
        return f"{base_url}{suffix}"

    def _extract_text(self, body: dict[str, Any], mode: str) -> str:
        if mode == "responses":
            output_text = body.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text

            output = body.get("output", [])
            chunks: list[str] = []
            if isinstance(output, list):
                for item in output:
                    for content in item.get("content", []):
                        if isinstance(content, dict):
                            text = content.get("text")
                            if isinstance(text, str):
                                chunks.append(text)
            return "\n".join(chunks).strip()

        choices = body.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            return "\n".join(chunks).strip()
        return ""


def _extract_json_payload(text: str) -> Any:
    cleaned = text.strip()
    if not cleaned:
        raise OpenAICompatError("Model returned empty content.")

    direct = _try_load_json(cleaned)
    if direct is not None:
        return direct

    fenced = cleaned
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            data = _try_load_json(candidate)
            if data is not None:
                return data
        fenced = cleaned.replace("```json", "").replace("```", "").strip()

    data = _try_load_json(_find_balanced_json(fenced))
    if data is not None:
        return data

    raise OpenAICompatError("Could not parse JSON from model output.")


def _try_load_json(candidate: str) -> Any | None:
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001
        return None


def _find_balanced_json(text: str) -> str:
    start = None
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if start is None and char in "{[":
            start = index
            depth = 1
            continue

        if start is None:
            continue

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char in "{[":
            depth += 1
            continue
        if char in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise OpenAICompatError("No balanced JSON object found in model output.")
