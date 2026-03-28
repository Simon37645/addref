from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

import requests


class MailDeliveryError(RuntimeError):
    pass


@dataclass(slots=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    sender_email: str
    sender_name: str = "AddRef"
    use_ssl: bool = True
    timeout_seconds: int = 20


@dataclass(slots=True)
class ResendSettings:
    api_key: str
    sender_email: str
    sender_name: str = "AddRef"
    api_base_url: str = "https://api.resend.com"
    timeout_seconds: int = 20


class SMTPMailer:
    def __init__(self, settings: SMTPSettings) -> None:
        self.settings = settings

    def send_html_mail(
        self,
        *,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.settings.sender_name, self.settings.sender_email))
        message["To"] = recipient
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        try:
            if self.settings.use_ssl:
                context = ssl.create_default_context()
                try:
                    context.set_ciphers("DEFAULT")
                except Exception:  # noqa: BLE001
                    pass
                with smtplib.SMTP_SSL(
                    self.settings.host,
                    self.settings.port,
                    timeout=self.settings.timeout_seconds,
                    context=context,
                ) as client:
                    client.login(self.settings.username, self.settings.password)
                    client.send_message(message)
                return

            with smtplib.SMTP(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout_seconds,
            ) as client:
                client.starttls(context=ssl.create_default_context())
                client.login(self.settings.username, self.settings.password)
                client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailDeliveryError(
                "邮件服务认证失败。请确认阿里邮箱已开启 SMTP/第三方客户端登录，并使用可用于第三方客户端的密码。"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise MailDeliveryError(f"邮件发送失败：{exc}") from exc


class ResendMailer:
    def __init__(self, settings: ResendSettings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def send_html_mail(
        self,
        *,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> None:
        response = self.session.post(
            f"{self.settings.api_base_url.rstrip('/')}/emails",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": formataddr((self.settings.sender_name, self.settings.sender_email)),
                "to": [recipient],
                "subject": subject,
                "text": text_body,
                "html": html_body,
            },
            timeout=self.settings.timeout_seconds,
        )
        if response.ok:
            return

        message = self._extract_error_message(response)
        if response.status_code in {401, 403}:
            raise MailDeliveryError("Resend API Key 无效或不可用。请检查 auth.json 中的 Resend 配置。")
        raise MailDeliveryError(f"Resend 发信失败：{message}")

    def _extract_error_message(self, response: requests.Response) -> str:
        try:
            payload: Any = response.json()
        except Exception:  # noqa: BLE001
            return response.text.strip() or f"HTTP {response.status_code}"

        if isinstance(payload, dict):
            for key in ("message", "error", "name"):
                value = payload.get(key)
                if value:
                    return str(value)
        return response.text.strip() or f"HTTP {response.status_code}"
