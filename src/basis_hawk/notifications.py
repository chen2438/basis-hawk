from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import smtplib
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Protocol

import httpx

from basis_hawk.config import AppConfig
from basis_hawk.storage import Database, NotificationOutboxItem, TradeIntentRow

logger = logging.getLogger(__name__)


class NotificationDeliveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class NotificationSender(Protocol):
    async def send(self, item: NotificationOutboxItem) -> None: ...


class TelegramSender:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not bot_token or "/" in bot_token or any(character.isspace() for character in bot_token):
            raise ValueError("Telegram bot token is invalid")
        if not chat_id or any(character in "\r\n" for character in chat_id):
            raise ValueError("Telegram chat ID is invalid")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def send(self, item: NotificationOutboxItem) -> None:
        text = f"{item.subject}\n\n{item.body}"
        if len(text) > 4096:
            text = f"{text[:4095]}…"
        try:
            response = await self._client.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "protect_content": True,
                },
            )
        except httpx.TimeoutException as exc:
            raise NotificationDeliveryError("transport_timeout") from exc
        except httpx.RequestError as exc:
            raise NotificationDeliveryError("transport_error") from exc
        if response.status_code == 429:
            raise NotificationDeliveryError("rate_limited")
        if response.status_code in {401, 403}:
            raise NotificationDeliveryError("authentication_failed")
        if response.status_code >= 500:
            raise NotificationDeliveryError("remote_unavailable")
        if response.status_code >= 400:
            raise NotificationDeliveryError("remote_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotificationDeliveryError("invalid_response") from exc
        if payload.get("ok") is not True:
            raise NotificationDeliveryError("remote_rejected")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class SmtpSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: str,
        username: str | None,
        password: str | None,
        sender: str,
        recipients: tuple[str, ...],
        timeout_seconds: float,
    ) -> None:
        text_values = (host, sender, *recipients)
        if (
            not recipients
            or any(
                not value or any(character in "\r\n" for character in value)
                for value in text_values
            )
        ):
            raise ValueError("SMTP address configuration is invalid")
        if security not in {"starttls", "smtps"}:
            raise ValueError("SMTP security must be starttls or smtps")
        if not 1 <= port <= 65535:
            raise ValueError("SMTP port is invalid")
        self._host = host
        self._port = port
        self._security = security
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = recipients
        self._timeout_seconds = timeout_seconds

    async def send(self, item: NotificationOutboxItem) -> None:
        try:
            await asyncio.to_thread(self._send_sync, item)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotificationDeliveryError("authentication_failed") from exc
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
        ) as exc:
            raise NotificationDeliveryError("remote_rejected") from exc
        except (TimeoutError, OSError) as exc:
            raise NotificationDeliveryError("transport_error") from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise NotificationDeliveryError("tls_unavailable") from exc
        except smtplib.SMTPResponseException as exc:
            code = (
                "remote_unavailable"
                if 400 <= exc.smtp_code < 500
                else "remote_rejected"
            )
            raise NotificationDeliveryError(code) from exc
        except smtplib.SMTPException as exc:
            raise NotificationDeliveryError("smtp_error") from exc

    def _send_sync(self, item: NotificationOutboxItem) -> None:
        message = EmailMessage()
        message["Subject"] = item.subject.replace("\r", " ").replace("\n", " ")
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(item.body)
        context = ssl.create_default_context()
        if self._security == "smtps":
            with smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout_seconds,
                context=context,
            ) as client:
                self._authenticate_and_send(client, message)
            return
        with smtplib.SMTP(
            self._host,
            self._port,
            timeout=self._timeout_seconds,
        ) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            self._authenticate_and_send(client, message)

    def _authenticate_and_send(
        self,
        client: smtplib.SMTP,
        message: EmailMessage,
    ) -> None:
        if self._username:
            client.login(self._username, self._password or "")
        client.send_message(
            message,
            from_addr=self._sender,
            to_addrs=list(self._recipients),
        )


class NotificationDeliveryService:
    def __init__(
        self,
        database: Database,
        senders: Mapping[str, NotificationSender],
        *,
        batch_size: int = 20,
    ) -> None:
        if not 1 <= batch_size <= 100:
            raise ValueError("notification batch size must be between 1 and 100")
        self.database = database
        self.senders = dict(senders)
        self.batch_size = batch_size

    @classmethod
    def from_config(
        cls,
        database: Database,
        config: AppConfig,
    ) -> NotificationDeliveryService:
        senders: dict[str, NotificationSender] = {}
        if config.telegram_bot_token is not None and config.telegram_chat_id:
            senders["telegram"] = TelegramSender(
                bot_token=config.telegram_bot_token.get_secret_value(),
                chat_id=config.telegram_chat_id,
                timeout_seconds=config.http_timeout_seconds,
            )
        recipients = tuple(
            value.strip()
            for value in (config.smtp_to or "").split(",")
            if value.strip()
        )
        if config.smtp_host and config.smtp_from and recipients:
            senders["email"] = SmtpSender(
                host=config.smtp_host,
                port=config.smtp_port,
                security=config.smtp_security,
                username=config.smtp_username,
                password=(
                    config.smtp_password.get_secret_value()
                    if config.smtp_password is not None
                    else None
                ),
                sender=config.smtp_from,
                recipients=recipients,
                timeout_seconds=config.http_timeout_seconds,
            )
        return cls(
            database,
            senders,
            batch_size=config.notification_batch_size,
        )

    async def run_once(self) -> int:
        items = await self.database.claim_notifications(limit=self.batch_size)
        for item in items:
            sender = self.senders.get(item.channel)
            if sender is None:
                await self.database.mark_notification_failed(
                    item.id,
                    error_code="channel_unconfigured",
                )
                continue
            try:
                await sender.send(item)
            except NotificationDeliveryError as exc:
                await self.database.mark_notification_failed(
                    item.id,
                    error_code=exc.error_code,
                )
            except Exception:
                await self.database.mark_notification_failed(
                    item.id,
                    error_code="internal_error",
                )
            else:
                await self.database.mark_notification_sent(item.id)
        return len(items)

    async def run_forever(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except Exception:
                logger.warning("notification delivery iteration failed")
                await asyncio.sleep(5)
                continue
            if processed == 0:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0)

    async def close(self) -> None:
        for sender in self.senders.values():
            close = getattr(sender, "close", None)
            if close is not None:
                await close()


class NotificationProjectionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def run_once(
        self,
        *,
        emit_initial_alerts: bool = True,
    ) -> int:
        projected = 0
        control = await self.database.execution_control()
        if control is not None:
            projected += int(
                await self.database.project_notification(
                    source_key="execution",
                    fingerprint=_fingerprint(control.state, control.reason),
                    notify=emit_initial_alerts
                    and control.state in {"paused", "blocked"},
                    event_type=f"execution.{control.state}",
                    severity="critical",
                    channels={"telegram", "email"},
                    subject="Basis Hawk execution safety state",
                    body=(
                        f"Execution is {control.state}. "
                        "Review system health and reconciliation before resuming."
                    ),
                )
            )
        for account in await self.database.reconciliation_states():
            abnormal = account.status != "ready"
            projected += int(
                await self.database.project_notification(
                    source_key=(
                        f"account:{account.exchange}:{account.environment}"
                    ),
                    fingerprint=_fingerprint(account.status, account.reason),
                    notify=emit_initial_alerts and abnormal,
                    event_type=f"account.{account.status}",
                    severity="critical",
                    channels={"telegram", "email"},
                    subject=(
                        f"Basis Hawk account {account.status}: "
                        f"{account.exchange} {account.environment}"
                    ),
                    body=(
                        f"{account.exchange} {account.environment} account "
                        f"reconciliation is {account.status}. "
                        "Review the account health page before trading."
                    ),
                )
            )
        updated_since = datetime.now(UTC) - timedelta(hours=1)
        for intent in await self.database.notification_trade_intents(
            updated_since=updated_since
        ):
            notification = _trade_notification(intent)
            projected += int(
                await self.database.project_notification(
                    source_key=f"trade:{intent.id}",
                    fingerprint=_fingerprint(intent.status),
                    notify=emit_initial_alerts and notification is not None,
                    event_type=(
                        notification[0]
                        if notification is not None
                        else f"trade.{intent.status}"
                    ),
                    severity=(
                        notification[1]
                        if notification is not None
                        else "info"
                    ),
                    channels=(
                        notification[2]
                        if notification is not None
                        else {"telegram"}
                    ),
                    subject=(
                        notification[3]
                        if notification is not None
                        else "Basis Hawk trade update"
                    ),
                    body=(
                        notification[4]
                        if notification is not None
                        else "Trade state updated."
                    ),
                )
            )
        return projected

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.warning("notification projection iteration failed")
            await asyncio.sleep(1)


def _fingerprint(*values: str) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _trade_notification(
    intent: TradeIntentRow,
) -> tuple[str, str, set[str], str, str] | None:
    status = intent.status
    exchange = intent.exchange
    environment = intent.environment
    base_asset = intent.base_asset
    action = intent.action
    label = f"{exchange} {environment} {base_asset} {action}"
    if status in {"hedged", "closed"}:
        return (
            f"trade.{status}",
            "info",
            {"telegram"},
            f"Basis Hawk trade {status}",
            f"{label} completed with status {status}.",
        )
    if status == "compensating":
        return (
            "trade.imbalance",
            "critical",
            {"telegram", "email"},
            "Basis Hawk fill imbalance",
            (
                f"{label} has imbalanced fills. A protection order is pending "
                "and execution remains paused."
            ),
        )
    if status in {"failed", "manual_review"}:
        return (
            f"trade.{status}",
            "critical",
            {"telegram", "email"},
            f"Basis Hawk trade {status}",
            (
                f"{label} ended with status {status}. "
                "Review orders, fills, and account exposure."
            ),
        )
    return None
