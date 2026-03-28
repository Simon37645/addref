from __future__ import annotations

import json
import mimetypes
import ipaddress
import threading
from collections import deque
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from app.services.citation_jobs import CitationJobStore
from app.services.citation_pipeline import CitationPipeline, CitationPipelineError, SearchFilters
from app.services.mailer import (
    MailDeliveryError,
    ResendMailer,
    ResendSettings,
    SMTPMailer,
    SMTPSettings,
)
from app.services.ncbi import NCBIClient, NCBIError
from app.services.openalex import OpenAlexClient, OpenAlexError, OpenAlexSettings
from app.services.openai_compat import OpenAICompatClient, OpenAICompatError, OpenAISettings
from app.services.user_store import (
    AuthenticationError,
    REGISTER_CODE_PURPOSE,
    RESET_PASSWORD_CODE_PURPOSE,
    SESSION_TTL_DAYS,
    ValidationError,
    authenticate_user,
    change_user_password,
    count_successful_default_uses,
    create_email_verification_code,
    create_session,
    create_user_with_verification,
    delete_email_verification_code,
    ensure_user_password,
    delete_session,
    get_user_by_session,
    init_db,
    record_usage,
    reset_password_with_verification,
)
from app.utils.ris import build_ris


ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "static"
AUTH_CONFIG_PATH = ROOT_DIR / "auth.json"
SESSION_COOKIE_NAME = "addref_session"
DEFAULT_SERVICE_DAILY_LIMIT = 20
DEFAULT_SERVICE_MAX_TEXT_LENGTH = 5000
CITATION_JOB_STORE = CitationJobStore()
MAX_CONCURRENT_CITATION_JOBS = 3
_CITATION_QUEUE_CONDITION = threading.Condition()
_CITATION_WAIT_QUEUE: deque[str] = deque()
_CITATION_ACTIVE_JOB_IDS: set[str] = set()


class AuthRequiredError(RuntimeError):
    pass


class UsagePolicyError(RuntimeError):
    pass


class AddRefHandler(BaseHTTPRequestHandler):
    server_version = "AddRef/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path in {"/settings", "/settings.html"}:
            self._serve_file(STATIC_DIR / "settings.html", "text/html; charset=utf-8")
            return
        if path in {"/auth", "/auth.html"}:
            self._serve_file(STATIC_DIR / "auth.html", "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._write_json({"status": "ok", "service": "addref", "port": 14785})
            return
        if path == "/api/default-config":
            self._handle_default_config()
            return
        if path == "/api/session":
            self._handle_session()
            return
        if path == "/api/cite-jobs/latest":
            self._handle_get_latest_cite_job()
            return
        if path.startswith("/api/cite-jobs/"):
            self._handle_get_cite_job(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/static/"):
            relative_path = path.removeprefix("/static/")
            self._serve_static(relative_path)
            return
        self._write_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/send-register-code":
            self._handle_send_register_code()
            return
        if path == "/api/send-reset-code":
            self._handle_send_reset_code()
            return
        if path == "/api/register":
            self._handle_register()
            return
        if path == "/api/reset-password":
            self._handle_reset_password()
            return
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/change-password":
            self._handle_change_password()
            return
        if path == "/api/cite":
            self._handle_cite()
            return
        if path == "/api/cite-jobs":
            self._handle_create_cite_job()
            return
        if path == "/api/export-ris":
            self._handle_export_ris()
            return
        if path == "/api/test-openai":
            self._handle_test_openai()
            return
        self._write_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_session(self) -> None:
        user_context = self._get_user_context(optional=True)
        if user_context is None:
            self._write_json({"authenticated": False})
            return

        self._write_json(
            {
                "authenticated": True,
                "user": {
                    "email": user_context["user"]["email"],
                    "is_owner": user_context["is_owner"],
                },
                "usage": _build_usage_payload(user_context),
            }
        )

    def _handle_register(self) -> None:
        try:
            payload = self._read_json_body()
            client_ip = self._get_client_ip()
            user = create_user_with_verification(
                email=str(payload.get("email", "")).strip(),
                password=str(payload.get("password", "")),
                verification_code=str(payload.get("verification_code", "")).strip(),
                ip_address=client_ip,
            )
            _notify_owner_new_user_async(user["email"])
            token = create_session(user["id"])
            response = self._build_session_response(user)
            self._write_json(
                response,
                headers=[("Set-Cookie", self._build_session_cookie(token))],
            )
        except ValidationError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_login(self) -> None:
        try:
            _ensure_owner_account()
            payload = self._read_json_body()
            user = authenticate_user(
                email=str(payload.get("email", "")).strip(),
                password=str(payload.get("password", "")),
            )
            token = create_session(user["id"])
            response = self._build_session_response(user)
            self._write_json(
                response,
                headers=[("Set-Cookie", self._build_session_cookie(token))],
            )
        except (AuthenticationError, ValidationError) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_send_register_code(self) -> None:
        self._handle_send_verification_code(purpose=REGISTER_CODE_PURPOSE)

    def _handle_send_reset_code(self) -> None:
        self._handle_send_verification_code(purpose=RESET_PASSWORD_CODE_PURPOSE)

    def _handle_send_verification_code(self, *, purpose: str) -> None:
        code_record: dict[str, Any] | None = None
        try:
            payload = self._read_json_body()
            email = str(payload.get("email", "")).strip()
            mailer = _build_mailer()
            code_record = create_email_verification_code(email=email, purpose=purpose)
            subject, text_body, html_body = _build_verification_email(
                purpose=purpose,
                recipient_email=email,
                code=code_record["code"],
                expires_in_minutes=code_record["expires_in_minutes"],
            )
            mailer.send_html_mail(
                recipient=email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
            self._write_json(
                {
                    "status": "ok",
                    "message": "验证码已发送，请检查邮箱，注意垃圾箱。",
                    "cooldown_seconds": code_record["cooldown_seconds"],
                    "expires_in_minutes": code_record["expires_in_minutes"],
                }
            )
        except (ValidationError, MailDeliveryError, ValueError) as exc:
            if code_record is not None:
                delete_email_verification_code(int(code_record["id"]))
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            if code_record is not None:
                delete_email_verification_code(int(code_record["id"]))
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_reset_password(self) -> None:
        try:
            payload = self._read_json_body()
            reset_password_with_verification(
                email=str(payload.get("email", "")).strip(),
                verification_code=str(payload.get("verification_code", "")).strip(),
                new_password=str(payload.get("new_password", "")),
            )
            self._write_json({"status": "ok", "message": "密码已重置，请使用新密码登录。"})
        except (AuthenticationError, ValidationError, ValueError) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_logout(self) -> None:
        token = self._get_session_token()
        delete_session(token)
        self._write_json(
            {"status": "ok"},
            headers=[("Set-Cookie", self._build_clear_cookie())],
        )

    def _handle_change_password(self) -> None:
        try:
            user_context = self._require_user_context()
            payload = self._read_json_body()
            change_user_password(
                user_id=int(user_context["user"]["id"]),
                current_password=str(payload.get("current_password", "")),
                new_password=str(payload.get("new_password", "")),
            )
            self._write_json({"status": "ok", "message": "密码已更新。"})
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except (AuthenticationError, ValidationError, ValueError) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_cite(self) -> None:
        try:
            user_context = self._require_user_context()
            payload = self._read_json_body()
            prepared = _prepare_citation_request(payload=payload, user_context=user_context)
            result = _execute_citation_request(prepared=prepared, user_context=user_context)
            self._write_json(result)
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except (
            CitationPipelineError,
            OpenAICompatError,
            NCBIError,
            OpenAlexError,
            UsagePolicyError,
            ValueError,
        ) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_create_cite_job(self) -> None:
        try:
            user_context = self._require_user_context()
            payload = self._read_json_body()
            prepared = _prepare_citation_request(payload=payload, user_context=user_context)
            job = CITATION_JOB_STORE.create_job(user_id=int(user_context["user"]["id"]))

            worker = threading.Thread(
                target=_run_citation_job_worker,
                kwargs={
                    "job_id": str(job["job_id"]),
                    "prepared": prepared,
                    "user_context": user_context,
                },
                daemon=True,
            )
            worker.start()
            self._write_json(_serialize_job(job), status=HTTPStatus.ACCEPTED)
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except (
            CitationPipelineError,
            OpenAICompatError,
            NCBIError,
            OpenAlexError,
            UsagePolicyError,
            ValueError,
        ) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_get_cite_job(self, job_id: str) -> None:
        try:
            user_context = self._require_user_context()
            normalized_job_id = str(job_id or "").strip()
            if not normalized_job_id:
                raise ValueError("缺少任务 ID。")
            job = CITATION_JOB_STORE.get_job(
                job_id=normalized_job_id,
                user_id=int(user_context["user"]["id"]),
            )
            if job is None:
                self._write_json({"error": "任务不存在。"}, status=HTTPStatus.NOT_FOUND)
                return
            self._write_json(_serialize_job(job))
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_get_latest_cite_job(self) -> None:
        try:
            user_context = self._require_user_context()
            job = CITATION_JOB_STORE.get_latest_job(user_id=int(user_context["user"]["id"]))
            if job is None:
                self._write_json({"error": "暂无最近任务。"}, status=HTTPStatus.NOT_FOUND)
                return
            self._write_json(_serialize_job(job))
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_test_openai(self) -> None:
        try:
            user_context = self._require_user_context()
            payload = self._read_json_body()
            merged = _resolve_effective_configs(payload)
            policy = _evaluate_usage_policy(text="", merged=merged, user_context=user_context, for_test=True)
            openai_config = merged["openai"]

            required_openai = {
                "base_url": "OpenAI 兼容接口 Base URL",
                "api_key": "OpenAI API Key",
                "model": "模型名称",
            }
            for key, label in required_openai.items():
                if not str(openai_config.get(key, "")).strip():
                    raise ValueError(f"缺少配置：{label}")

            llm = OpenAICompatClient(
                OpenAISettings(
                    base_url=str(openai_config.get("base_url", "")).strip(),
                    api_key=str(openai_config.get("api_key", "")).strip(),
                    model=str(openai_config.get("model", "")).strip(),
                    api_mode=str(openai_config.get("api_mode", "auto")).strip() or "auto",
                    timeout_seconds=45,
                )
            )
            test_result = llm.complete_text(
                system_prompt=(
                    "You are performing a connectivity check for an OpenAI-compatible API. "
                    "Reply in one short plain-text sentence."
                ),
                user_prompt="Return a brief confirmation that includes the word OK.",
            )
            response_text = str(test_result.get("text", "")).strip()
            if not response_text:
                raise OpenAICompatError("模型已响应，但返回内容为空。")

            self._write_json(
                {
                    "status": "ok",
                    "message": "模型连接成功。",
                    "response_preview": response_text[:240],
                    "mode_used": test_result.get("mode", ""),
                    "usage": _build_usage_payload(
                        {
                            "is_owner": user_context["is_owner"],
                            "default_used_today": policy["default_used_today"],
                            "default_remaining": policy["default_remaining"],
                        }
                    ),
                }
            )
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except (OpenAICompatError, UsagePolicyError, ValueError) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_default_config(self) -> None:
        defaults = _load_auth_defaults()
        self._write_json(
            {
                "has_auth_json": defaults["has_auth_json"],
                "has_openai_defaults": bool(
                    defaults["openai"]["base_url"]
                    or defaults["openai"]["api_key"]
                    or defaults["openai"]["model"]
                ),
                "has_ncbi_defaults": bool(defaults["ncbi"]["api_key"] or defaults["ncbi"]["email"]),
            }
        )

    def _handle_export_ris(self) -> None:
        try:
            self._require_user_context()
            payload = self._read_json_body()
            references = payload.get("references", [])
            if not isinstance(references, list) or not references:
                raise ValueError("No references provided for RIS export.")

            ris_content = build_ris(references)
            if not ris_content.strip():
                raise ValueError("Unable to generate RIS content from the selected references.")

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-research-info-systems; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="references.ris"')
            self.end_headers()
            self.wfile.write(ris_content.encode("utf-8"))
        except AuthRequiredError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._write_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _serve_static(self, relative_path: str) -> None:
        clean_path = Path(relative_path).as_posix().lstrip("/")
        file_path = (STATIC_DIR / clean_path).resolve()
        if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR:
            self._write_json({"error": "Invalid path."}, status=HTTPStatus.BAD_REQUEST)
            return
        if not file_path.exists() or not file_path.is_file():
            self._write_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        self._serve_file(file_path, content_type or "application/octet-stream")

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        try:
            content = file_path.read_bytes()
        except FileNotFoundError:
            self._write_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body.") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object.")
        return body

    def _write_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _get_session_token(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def _get_client_ip(self) -> str:
        candidates = (
            self.headers.get("CF-Connecting-IP", ""),
            self.headers.get("X-Real-IP", ""),
            self.headers.get("X-Forwarded-For", ""),
        )
        for raw_value in candidates:
            for part in str(raw_value or "").split(","):
                normalized = _normalize_ip(part)
                if normalized:
                    return normalized

        fallback = self.client_address[0] if self.client_address else ""
        return _normalize_ip(fallback) or "unknown"

    def _get_user_context(self, optional: bool) -> dict[str, Any] | None:
        token = self._get_session_token()
        user = get_user_by_session(token)
        if user is None:
            return None if optional else self._raise_auth_required()

        return self._refresh_user_context(user)

    def _require_user_context(self) -> dict[str, Any]:
        context = self._get_user_context(optional=False)
        if context is None:
            raise AuthRequiredError("请先登录。")
        return context

    def _refresh_user_context(self, user: dict[str, Any]) -> dict[str, Any]:
        return _build_user_context(user)

    def _build_session_response(self, user: dict[str, Any]) -> dict[str, Any]:
        context = self._refresh_user_context(user)
        return {
            "authenticated": True,
            "user": {
                "email": context["user"]["email"],
                "is_owner": context["is_owner"],
            },
            "usage": _build_usage_payload(context),
        }

    def _build_session_cookie(self, token: str) -> str:
        max_age = SESSION_TTL_DAYS * 24 * 60 * 60
        return (
            f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        )

    def _build_clear_cookie(self) -> str:
        return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def _raise_auth_required(self) -> None:
        raise AuthRequiredError("请先登录。")


def _build_user_context(user: dict[str, Any]) -> dict[str, Any]:
    defaults = _load_auth_defaults()
    normalized_email = _normalize_email(user["email"])
    is_owner = normalized_email == _normalize_email(defaults["owner_email"])
    default_limit, max_text_length = _resolve_default_service_profile(is_owner=is_owner)
    usage_date = _local_date_string()
    default_used_today = 0 if is_owner else count_successful_default_uses(user["id"], usage_date)
    return {
        "user": user,
        "is_owner": is_owner,
        "usage_date": usage_date,
        "default_limit": default_limit,
        "default_max_text_length": max_text_length,
        "default_used_today": default_used_today,
        "default_remaining": None if default_limit is None else max(0, default_limit - default_used_today),
    }


def _prepare_citation_request(*, payload: dict[str, Any], user_context: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    if not text:
        raise CitationPipelineError("请输入需要插入参考文献的文本。")

    merged = _resolve_effective_configs(payload)
    policy = _evaluate_usage_policy(text=text, merged=merged, user_context=user_context)
    openai_config = merged["openai"]
    ncbi_config = merged["ncbi"]
    max_targets, max_attempts, results_per_query = _resolve_pipeline_limits(payload)
    search_filters = _resolve_search_filters(payload)
    existing_references = _normalize_json_list(payload.get("existing_references"))
    existing_placements = _normalize_json_list(payload.get("existing_placements"))

    required_openai = {
        "base_url": "OpenAI 兼容接口 Base URL",
        "api_key": "OpenAI API Key",
        "model": "模型名称",
    }
    for key, label in required_openai.items():
        if not str(openai_config.get(key, "")).strip():
            raise CitationPipelineError(f"缺少配置：{label}")

    return {
        "text": text,
        "policy": policy,
        "openai_config": openai_config,
        "ncbi_config": ncbi_config,
        "max_targets": max_targets,
        "max_attempts": max_attempts,
        "results_per_query": results_per_query,
        "search_filters": search_filters,
        "existing_references": existing_references,
        "existing_placements": existing_placements,
    }


def _execute_citation_request(
    *,
    prepared: dict[str, Any],
    user_context: dict[str, Any],
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    text = str(prepared["text"])
    policy = prepared["policy"]
    openai_config = prepared["openai_config"]
    ncbi_config = prepared["ncbi_config"]

    llm = OpenAICompatClient(
        OpenAISettings(
            base_url=str(openai_config.get("base_url", "")).strip(),
            api_key=str(openai_config.get("api_key", "")).strip(),
            model=str(openai_config.get("model", "")).strip(),
            api_mode=str(openai_config.get("api_mode", "auto")).strip() or "auto",
        )
    )
    ncbi = NCBIClient(
        api_key=str(ncbi_config.get("api_key", "")).strip(),
        email=str(ncbi_config.get("email", "")).strip(),
    )

    openalex = None
    search_filters: SearchFilters = prepared["search_filters"]
    if search_filters.has_impact_factor_filter():
        openalex = OpenAlexClient(_load_openalex_settings())

    pipeline = CitationPipeline(llm=llm, ncbi=ncbi, openalex=openalex)
    result = pipeline.run(
        text=text,
        max_targets=int(prepared["max_targets"]),
        max_attempts=int(prepared["max_attempts"]),
        results_per_query=int(prepared["results_per_query"]),
        search_filters=search_filters,
        existing_references=prepared["existing_references"],
        existing_placements=prepared["existing_placements"],
        progress_callback=progress_callback,
    )
    citation_success = bool(int(result.get("new_placement_count", 0) or 0))

    if policy["count_default_usage"]:
        record_usage(
            user_id=user_context["user"]["id"],
            usage_date=user_context["usage_date"],
            text_length=len(text),
            success=citation_success,
            used_default_openai=policy["using_default_openai"],
            used_default_ncbi=policy["using_default_ncbi"],
            endpoint="cite",
        )

    refreshed = _build_user_context(user_context["user"])
    result["usage"] = _build_usage_payload(refreshed)
    return result


def _run_citation_job_worker(
    *,
    job_id: str,
    prepared: dict[str, Any],
    user_context: dict[str, Any],
) -> None:
    slot_acquired = False
    try:
        _wait_for_citation_slot(job_id)
        slot_acquired = True
        CITATION_JOB_STORE.update_job(
            job_id,
            status="running",
            stage="initializing",
            progress_percent=2,
            message="开始处理。",
            detail="正在连接模型与检索服务。",
            event_message="开始处理。",
        )
        result = _execute_citation_request(
            prepared=prepared,
            user_context=user_context,
            progress_callback=lambda event: CITATION_JOB_STORE.update_job(  # noqa: B023
                job_id,
                status="running",
                stage=str(event.get("stage", "running") or "running"),
                progress_percent=int(event.get("progress_percent", 0) or 0),
                message=str(event.get("message", "")).strip() or "处理中。",
                detail=str(event.get("detail", "")).strip(),
                event_message=str(event.get("message", "")).strip() or "处理中。",
            ),
        )
        CITATION_JOB_STORE.complete_job(job_id, result=result)
    except (
        CitationPipelineError,
        OpenAICompatError,
        NCBIError,
        OpenAlexError,
        UsagePolicyError,
        ValueError,
    ) as exc:
        CITATION_JOB_STORE.fail_job(job_id, error_message=str(exc))
    except Exception as exc:  # noqa: BLE001
        CITATION_JOB_STORE.fail_job(job_id, error_message=f"Server error: {exc}")
    finally:
        if slot_acquired:
            _release_citation_slot(job_id)


def _wait_for_citation_slot(job_id: str) -> None:
    queued_message_sent = False
    last_ahead = -1
    with _CITATION_QUEUE_CONDITION:
        if job_id not in _CITATION_WAIT_QUEUE:
            _CITATION_WAIT_QUEUE.append(job_id)
        while True:
            ahead = _queue_ahead_count(job_id)
            can_run = ahead == 0 and len(_CITATION_ACTIVE_JOB_IDS) < MAX_CONCURRENT_CITATION_JOBS
            if can_run:
                try:
                    _CITATION_WAIT_QUEUE.remove(job_id)
                except ValueError:
                    pass
                _CITATION_ACTIVE_JOB_IDS.add(job_id)
                _CITATION_QUEUE_CONDITION.notify_all()
                return

            if not queued_message_sent or ahead != last_ahead:
                CITATION_JOB_STORE.update_job(
                    job_id,
                    status="queued",
                    stage="queued",
                    progress_percent=0,
                    message="排队中。",
                    detail=f"前方还有 {ahead} 个任务。当前最多同时处理 {MAX_CONCURRENT_CITATION_JOBS} 个任务。",
                    event_message="任务进入队列。" if not queued_message_sent else "",
                )
                queued_message_sent = True
                last_ahead = ahead
            _CITATION_QUEUE_CONDITION.wait(timeout=1.0)


def _release_citation_slot(job_id: str) -> None:
    with _CITATION_QUEUE_CONDITION:
        _CITATION_ACTIVE_JOB_IDS.discard(job_id)
        _CITATION_QUEUE_CONDITION.notify_all()


def _queue_ahead_count(job_id: str) -> int:
    try:
        return list(_CITATION_WAIT_QUEUE).index(job_id)
    except ValueError:
        return 0


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(job.get("job_id", "")),
        "status": str(job.get("status", "")),
        "progress_percent": int(job.get("progress_percent", 0) or 0),
        "stage": str(job.get("stage", "")),
        "message": str(job.get("message", "")),
        "detail": str(job.get("detail", "")),
        "history": job.get("history", []),
        "result": job.get("result"),
        "error": str(job.get("error", "")),
        "created_at": str(job.get("created_at", "")),
        "updated_at": str(job.get("updated_at", "")),
    }


def _evaluate_usage_policy(
    *,
    text: str,
    merged: dict[str, Any],
    user_context: dict[str, Any],
    for_test: bool = False,
) -> dict[str, Any]:
    defaults = _load_auth_defaults()
    openai_config = merged["openai"]
    ncbi_config = merged["ncbi"]
    default_openai_key = _normalize_string(defaults["openai"]["api_key"])
    default_ncbi_key = _normalize_string(defaults["ncbi"]["api_key"])
    default_ncbi_email = _normalize_email(defaults["ncbi"]["email"])

    using_default_openai = bool(default_openai_key) and (
        _normalize_string(openai_config["api_key"]) == default_openai_key
    )
    using_default_ncbi = False
    if default_ncbi_key and _normalize_string(ncbi_config["api_key"]) == default_ncbi_key:
        using_default_ncbi = True
    if default_ncbi_email and _normalize_email(ncbi_config["email"]) == default_ncbi_email:
        using_default_ncbi = True

    using_default_service = using_default_openai if for_test else (using_default_openai or using_default_ncbi)
    default_used_today = user_context["default_used_today"]
    default_remaining = user_context["default_remaining"]

    if user_context["is_owner"]:
        return {
            "using_default_openai": using_default_openai,
            "using_default_ncbi": using_default_ncbi,
            "count_default_usage": False,
            "default_used_today": default_used_today,
            "default_remaining": default_remaining,
        }

    default_limit = user_context["default_limit"]
    default_max_text_length = user_context["default_max_text_length"]

    if using_default_service and not for_test and len(text) > default_max_text_length:
        raise UsagePolicyError(f"使用默认服务时，文本长度必须小于或等于 {default_max_text_length}。")

    if using_default_service and default_limit is not None and default_used_today >= default_limit:
        raise UsagePolicyError(
            f"默认服务今日已达 {default_limit} 次上限。请改用自己的 OpenAI API Key，并且不要使用默认 NCBI 邮箱或 API Key。"
        )

    return {
        "using_default_openai": using_default_openai,
        "using_default_ncbi": using_default_ncbi,
        "count_default_usage": using_default_service and not for_test,
        "default_used_today": default_used_today,
        "default_remaining": default_remaining,
    }


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:  # noqa: BLE001
        return default
    return max(minimum, min(maximum, parsed))


def _build_usage_payload(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("is_owner"):
        return {
            "default_limit": None,
            "default_used_today": 0,
            "default_remaining": None,
            "default_max_text_length": None,
            "unlimited": True,
        }

    return {
        "default_limit": int(data.get("default_limit", DEFAULT_SERVICE_DAILY_LIMIT) or DEFAULT_SERVICE_DAILY_LIMIT),
        "default_used_today": int(data.get("default_used_today", 0) or 0),
        "default_remaining": int(data.get("default_remaining", 0) or 0),
        "default_max_text_length": int(
            data.get("default_max_text_length", DEFAULT_SERVICE_MAX_TEXT_LENGTH) or DEFAULT_SERVICE_MAX_TEXT_LENGTH
        ),
        "unlimited": False,
    }


def _resolve_default_service_profile(*, is_owner: bool) -> tuple[int | None, int | None]:
    if is_owner:
        return None, None
    return DEFAULT_SERVICE_DAILY_LIMIT, DEFAULT_SERVICE_MAX_TEXT_LENGTH


def _load_auth_defaults() -> dict[str, Any]:
    defaults = {
        "has_auth_json": False,
        "owner_email": "",
        "owner_password": "",
        "openai": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "api_mode": "",
        },
        "ncbi": {
            "api_key": "",
            "email": "",
        },
        "mail": {
            "smtp_host": "",
            "smtp_port": 465,
            "username": "",
            "password": "",
            "sender_email": "",
            "sender_name": "AddRef",
            "use_ssl": True,
        },
        "openalex": {
            "api_key": "",
            "email": "",
        },
        "resend": {
            "api_key": "",
            "sender_email": "",
            "sender_name": "AddRef",
            "api_base_url": "https://api.resend.com",
        },
    }
    if not AUTH_CONFIG_PATH.exists():
        return defaults

    try:
        data = json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return defaults

    if not isinstance(data, dict):
        return defaults

    defaults["has_auth_json"] = True
    defaults["owner_email"] = str(
        data.get("OWNER_email")
        or data.get("owner_email")
        or ""
    ).strip()
    defaults["owner_password"] = str(data.get("OWNER_password") or data.get("owner_password") or "").strip()
    defaults["openai"]["base_url"] = str(data.get("OPENAI_base_url", "")).strip()
    defaults["openai"]["api_key"] = str(data.get("OPENAI_APIkey", "")).strip()
    defaults["openai"]["model"] = str(data.get("model", "")).strip()
    defaults["openai"]["api_mode"] = str(data.get("api_mode", "")).strip()
    defaults["ncbi"]["api_key"] = str(data.get("NCBI_APIkey", "")).strip()
    defaults["ncbi"]["email"] = str(data.get("NCBI_email", "")).strip()
    defaults["openalex"]["api_key"] = str(data.get("OPENALEX_APIkey", "")).strip()
    defaults["openalex"]["email"] = str(data.get("OPENALEX_email", "")).strip()
    defaults["mail"]["smtp_host"] = str(data.get("MAIL_SMTP_HOST") or "smtp.qiye.aliyun.com").strip()
    defaults["mail"]["smtp_port"] = _clamp_int(data.get("MAIL_SMTP_PORT"), default=465, minimum=1, maximum=65535)
    defaults["mail"]["username"] = str(
        data.get("MAIL_username") or data.get("MAIL_account") or data.get("MAIL_sender") or ""
    ).strip()
    defaults["mail"]["password"] = str(data.get("MAIL_password", "")).strip()
    defaults["mail"]["sender_email"] = str(
        data.get("MAIL_sender") or data.get("MAIL_account") or data.get("MAIL_username") or ""
    ).strip()
    defaults["mail"]["sender_name"] = str(data.get("MAIL_sender_name") or "AddRef").strip() or "AddRef"
    defaults["mail"]["use_ssl"] = bool(data.get("MAIL_use_ssl", True))
    defaults["resend"]["api_key"] = str(
        data.get("RESEND_APIkey") or data.get("resend_APIkey") or data.get("RESEND_api_key") or ""
    ).strip()
    defaults["resend"]["sender_email"] = str(
        data.get("RESEND_sender") or data.get("resend_sender") or data.get("MAIL_sender") or ""
    ).strip()
    defaults["resend"]["sender_name"] = str(
        data.get("RESEND_sender_name") or data.get("resend_sender_name") or data.get("MAIL_sender_name") or "AddRef"
    ).strip() or "AddRef"
    defaults["resend"]["api_base_url"] = str(
        data.get("RESEND_base_url") or data.get("resend_base_url") or "https://api.resend.com"
    ).strip()
    return defaults


def _resolve_effective_configs(payload: dict[str, Any]) -> dict[str, Any]:
    defaults = _load_auth_defaults()
    openai_input = payload.get("openai", {}) or {}
    ncbi_input = payload.get("ncbi", {}) or {}
    disable_default_ncbi = bool(ncbi_input.get("disable_defaults"))

    effective_openai: dict[str, str] = {}
    effective_ncbi: dict[str, str] = {}
    sources = {"openai": {}, "ncbi": {}}

    for key in ("base_url", "api_key", "model", "api_mode"):
        user_value = str(openai_input.get(key, "")).strip()
        default_value = str(defaults["openai"].get(key, "")).strip()
        value = user_value or default_value
        source = "request" if user_value else ("auth.json" if default_value else "missing")
        if key == "api_mode" and not value:
            value = "auto"
            source = "fallback"
        effective_openai[key] = value
        sources["openai"][key] = source

    for key in ("api_key", "email"):
        user_value = str(ncbi_input.get(key, "")).strip()
        default_value = "" if disable_default_ncbi else str(defaults["ncbi"].get(key, "")).strip()
        value = user_value or default_value
        if user_value:
            source = "request"
        elif disable_default_ncbi:
            source = "disabled"
        else:
            source = "auth.json" if default_value else "missing"
        effective_ncbi[key] = value
        sources["ncbi"][key] = source

    return {
        "openai": effective_openai,
        "ncbi": effective_ncbi,
        "sources": sources,
        "has_auth_json": defaults["has_auth_json"],
    }


def _local_date_string() -> str:
    return datetime.now().astimezone().date().isoformat()


def _normalize_string(value: str) -> str:
    return (value or "").strip()


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_ip(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _load_mail_settings() -> SMTPSettings:
    defaults = _load_auth_defaults()
    mail = defaults["mail"]
    required_fields = {
        "smtp_host": "SMTP Host",
        "username": "发件账号",
        "password": "发件密码",
        "sender_email": "发件邮箱",
    }
    for key, label in required_fields.items():
        if not str(mail.get(key, "")).strip():
            raise ValueError(f"邮件服务未配置：{label}")

    return SMTPSettings(
        host=str(mail["smtp_host"]).strip(),
        port=_clamp_int(mail.get("smtp_port"), default=465, minimum=1, maximum=65535),
        username=str(mail["username"]).strip(),
        password=str(mail["password"]).strip(),
        sender_email=str(mail["sender_email"]).strip(),
        sender_name=str(mail.get("sender_name", "AddRef")).strip() or "AddRef",
        use_ssl=bool(mail.get("use_ssl", True)),
    )


def _load_resend_settings() -> ResendSettings:
    defaults = _load_auth_defaults()
    resend = defaults["resend"]
    required_fields = {
        "api_key": "Resend API Key",
        "sender_email": "Resend 发件地址",
    }
    for key, label in required_fields.items():
        if not str(resend.get(key, "")).strip():
            raise ValueError(f"邮件服务未配置：{label}")

    return ResendSettings(
        api_key=str(resend["api_key"]).strip(),
        sender_email=str(resend["sender_email"]).strip(),
        sender_name=str(resend.get("sender_name", "AddRef")).strip() or "AddRef",
        api_base_url=str(resend.get("api_base_url", "https://api.resend.com")).strip() or "https://api.resend.com",
    )


def _load_openalex_settings() -> OpenAlexSettings:
    defaults = _load_auth_defaults()
    openalex = defaults["openalex"]
    api_key = str(openalex.get("api_key", "")).strip()
    if not api_key:
        raise ValueError("使用 IF 区间过滤前，请先在 auth.json 配置 OPENALEX_APIkey。")
    return OpenAlexSettings(
        api_key=api_key,
        email=str(openalex.get("email", "")).strip(),
    )


def _build_mailer() -> ResendMailer | SMTPMailer:
    defaults = _load_auth_defaults()
    if str(defaults["resend"].get("api_key", "")).strip():
        return ResendMailer(_load_resend_settings())
    return SMTPMailer(_load_mail_settings())


def _build_verification_email(
    *,
    purpose: str,
    recipient_email: str,
    code: str,
    expires_in_minutes: int,
) -> tuple[str, str, str]:
    if purpose == RESET_PASSWORD_CODE_PURPOSE:
        return _build_reset_password_email(
            recipient_email=recipient_email,
            code=code,
            expires_in_minutes=expires_in_minutes,
        )
    return _build_register_verification_email(
        recipient_email=recipient_email,
        code=code,
        expires_in_minutes=expires_in_minutes,
    )


def _notify_owner_new_user_async(new_user_email: str) -> None:
    thread = threading.Thread(
        target=_send_owner_new_user_notification,
        kwargs={"new_user_email": new_user_email},
        daemon=True,
    )
    thread.start()


def _send_owner_new_user_notification(*, new_user_email: str) -> None:
    try:
        defaults = _load_auth_defaults()
        recipient = _normalize_email(defaults["owner_email"])
        if not recipient:
            return
        subject, text_body, html_body = _build_owner_new_user_email(new_user_email=new_user_email)
        _build_mailer().send_html_mail(
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except Exception:  # noqa: BLE001
        return


def _build_owner_new_user_email(*, new_user_email: str) -> tuple[str, str, str]:
    safe_email = escape(new_user_email)
    registered_at = escape(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
    subject = "【AddRef】新用户注册通知"
    text_body = (
        "你好，\n\n"
        "AddRef 有新用户完成注册。\n"
        f"用户邮箱：{new_user_email}\n"
        f"注册时间：{registered_at}\n"
    )
    html_body = f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f3efe8;font-family:'Segoe UI',sans-serif;color:#1f2832;">
    <div style="max-width:560px;margin:0 auto;background:#fffdfa;border:1px solid #e8e0d3;border-radius:20px;overflow:hidden;">
      <div style="padding:24px 28px;background:linear-gradient(135deg,#e8d8b5,#d8ece5);">
        <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#556270;">AddRef</div>
        <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">新用户注册通知</h1>
      </div>
      <div style="padding:28px;">
        <p style="margin:0 0 14px;font-size:15px;line-height:1.7;">有新用户完成注册。</p>
        <div style="padding:18px 20px;border-radius:18px;background:#18232d;color:#ffffff;">
          <div style="font-size:13px;letter-spacing:0.12em;text-transform:uppercase;color:rgba(255,255,255,0.68);">New User</div>
          <div style="margin-top:8px;font-size:24px;font-weight:700;word-break:break-all;">{safe_email}</div>
        </div>
        <p style="margin:16px 0 0;font-size:13px;line-height:1.7;color:#6d7782;">注册时间：{registered_at}</p>
      </div>
    </div>
  </body>
</html>
""".strip()
    return subject, text_body, html_body


def _build_register_verification_email(
    *,
    recipient_email: str,
    code: str,
    expires_in_minutes: int,
) -> tuple[str, str, str]:
    safe_email = escape(recipient_email)
    safe_code = escape(code)
    subject = "【AddRef】邮箱验证码"
    text_body = (
        "你好，\n\n"
        f"你正在注册 AddRef。\n"
        f"本次验证码：{code}\n"
        f"有效期：{expires_in_minutes} 分钟。\n\n"
        "如果这不是你的操作，请忽略这封邮件。"
    )
    html_body = f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f4efe7;font-family:'Segoe UI',sans-serif;color:#1e2530;">
    <div style="max-width:560px;margin:0 auto;background:#fffdf8;border:1px solid #eadfce;border-radius:20px;overflow:hidden;">
      <div style="padding:24px 28px;background:linear-gradient(135deg,#f3d6a4,#d9efe5);">
        <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#556270;">AddRef</div>
        <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">邮箱验证码</h1>
      </div>
      <div style="padding:28px;">
        <p style="margin:0 0 14px;font-size:15px;line-height:1.7;">你正在为 <strong>{safe_email}</strong> 注册 AddRef。</p>
        <div style="margin:18px 0;padding:18px 20px;border-radius:18px;background:#18232d;color:#ffffff;text-align:center;">
          <div style="font-size:13px;letter-spacing:0.16em;text-transform:uppercase;color:rgba(255,255,255,0.68);">Verification Code</div>
          <div style="margin-top:8px;font-size:34px;font-weight:700;letter-spacing:0.32em;text-indent:0.32em;">{safe_code}</div>
        </div>
        <p style="margin:0 0 10px;font-size:14px;line-height:1.7;">验证码 {expires_in_minutes} 分钟内有效。</p>
        <p style="margin:0;font-size:13px;line-height:1.7;color:#6d7782;">如果这不是你的操作，请直接忽略这封邮件。</p>
      </div>
    </div>
  </body>
</html>
""".strip()
    return subject, text_body, html_body


def _build_reset_password_email(
    *,
    recipient_email: str,
    code: str,
    expires_in_minutes: int,
) -> tuple[str, str, str]:
    safe_email = escape(recipient_email)
    safe_code = escape(code)
    subject = "【AddRef】重置密码验证码"
    text_body = (
        "你好，\n\n"
        f"你正在重置 AddRef 账号 {recipient_email} 的密码。\n"
        f"本次验证码：{code}\n"
        f"有效期：{expires_in_minutes} 分钟。\n\n"
        "如果这不是你的操作，请忽略这封邮件。"
    )
    html_body = f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f3efe8;font-family:'Segoe UI',sans-serif;color:#1f2832;">
    <div style="max-width:560px;margin:0 auto;background:#fffdfa;border:1px solid #e8e0d3;border-radius:20px;overflow:hidden;">
      <div style="padding:24px 28px;background:linear-gradient(135deg,#f0c8a0,#d6e9f0);">
        <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#556270;">AddRef</div>
        <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">重置密码</h1>
      </div>
      <div style="padding:28px;">
        <p style="margin:0 0 14px;font-size:15px;line-height:1.7;">你正在为 <strong>{safe_email}</strong> 重置密码。</p>
        <div style="margin:18px 0;padding:18px 20px;border-radius:18px;background:#17212b;color:#ffffff;text-align:center;">
          <div style="font-size:13px;letter-spacing:0.16em;text-transform:uppercase;color:rgba(255,255,255,0.68);">Reset Code</div>
          <div style="margin-top:8px;font-size:34px;font-weight:700;letter-spacing:0.32em;text-indent:0.32em;">{safe_code}</div>
        </div>
        <p style="margin:0 0 10px;font-size:14px;line-height:1.7;">验证码 {expires_in_minutes} 分钟内有效。</p>
        <p style="margin:0;font-size:13px;line-height:1.7;color:#6d7782;">如果这不是你的操作，请直接忽略这封邮件。</p>
      </div>
    </div>
  </body>
</html>
""".strip()
    return subject, text_body, html_body


def _resolve_pipeline_limits(payload: dict[str, Any]) -> tuple[int, int, int]:
    max_targets = _clamp_int(payload.get("max_targets"), default=4, minimum=1, maximum=80)
    max_attempts = _clamp_int(payload.get("max_attempts"), default=10, minimum=1, maximum=20)
    results_per_query = _clamp_int(payload.get("results_per_query"), default=6, minimum=3, maximum=20)
    if max_targets * max_attempts * results_per_query > 8000:
        raise ValueError("插入条数、每轮结果数、最大轮次的乘积不能超过 8000。")
    return max_targets, max_attempts, results_per_query


def _resolve_search_filters(payload: dict[str, Any]) -> SearchFilters:
    recent_years = _optional_clamp_int(payload.get("recent_years"), minimum=1, maximum=50)
    impact_factor_min = _optional_clamp_float(payload.get("impact_factor_min"), minimum=0, maximum=500)
    impact_factor_max = _optional_clamp_float(payload.get("impact_factor_max"), minimum=0, maximum=500)
    if (
        impact_factor_min is not None
        and impact_factor_max is not None
        and impact_factor_min > impact_factor_max
    ):
        raise ValueError("IF 最小值不能大于最大值。")
    return SearchFilters(
        recent_years=recent_years,
        impact_factor_min=impact_factor_min,
        impact_factor_max=impact_factor_max,
    )


def _optional_clamp_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if str(value or "").strip() == "":
        return None
    return _clamp_int(value, default=minimum, minimum=minimum, maximum=maximum)


def _optional_clamp_float(value: Any, *, minimum: float, maximum: float) -> float | None:
    if str(value or "").strip() == "":
        return None
    try:
        parsed = float(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("请输入合法的 IF 数值。") from exc
    return round(max(minimum, min(maximum, parsed)), 3)


def _normalize_json_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _ensure_owner_account() -> None:
    defaults = _load_auth_defaults()
    owner_email = _normalize_email(defaults["owner_email"])
    owner_password = str(defaults["owner_password"]).strip()
    if not owner_email or not owner_password:
        return
    ensure_user_password(owner_email, owner_password)


init_db()
_ensure_owner_account()
