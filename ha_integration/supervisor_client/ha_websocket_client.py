"""
Asynchroner Home Assistant WebSocket-Client.

Implementiert den vollständigen HA WebSocket-Protokoll-Lifecycle:
  auth_required → auth → auth_ok → subscribe/query

Dient als Datenquelle für alle Agenten: Log-Streaming, State-Abfragen,
Event-Subscription, Service-/Action-Calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

logger = logging.getLogger(__name__)


class HAWebSocketError(Exception):
    """Basisklasse für WebSocket-Fehler."""


class HAAuthError(HAWebSocketError):
    """Authentifizierung fehlgeschlagen."""


class HAWebSocketClient:
    """
    Vollständiger async HA WebSocket-Client.

    Unterstützt:
    - Authentifizierung mit Long-Lived Access Token
    - Event-Subscriptions (state_changed, call_service usw.)
    - One-shot-Anfragen (get_states, get_config, render_template)
    - Log-Streaming via Supervisor API
    - Service/Action Calls
    """

    def __init__(
        self,
        ha_url: str,
        token: str,
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        self._ws: Any = None
        self._msg_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._connected = False
        self._reader_task: asyncio.Task | None = None

    # -------------------------------------------------------------------------
    # Verbindungsmanagement
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Stellt die WebSocket-Verbindung her und authentifiziert sich."""
        try:
            import websockets
        except ImportError as exc:
            raise ImportError(
                "websockets package nicht installiert. "
                "Installation: pip install websockets"
            ) from exc

        ws_url = self.ha_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/websocket"

        logger.info("Verbinde mit HA WebSocket: %s", ws_url)

        ssl_context = None
        if not self.verify_ssl and ws_url.startswith("wss://"):
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        self._ws = await websockets.connect(
            ws_url,
            ssl=ssl_context,
            open_timeout=self.timeout,
        )

        # Auth-Handshake
        auth_required = await self._recv_raw()
        if auth_required.get("type") != "auth_required":
            raise HAWebSocketError(f"Unerwartete erste Nachricht: {auth_required}")

        await self._send_raw({"type": "auth", "access_token": self.token})
        auth_result = await self._recv_raw()

        if auth_result.get("type") == "auth_invalid":
            raise HAAuthError(f"Authentifizierung fehlgeschlagen: {auth_result.get('message')}")
        if auth_result.get("type") != "auth_ok":
            raise HAWebSocketError(f"Unerwartetes Auth-Ergebnis: {auth_result}")

        # Message-Coalescing aktivieren (Performance)
        await self._send_raw({
            "id": self._next_id(),
            "type": "supported_features",
            "features": {"coalesce_messages": 1},
        })

        self._connected = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("HA WebSocket verbunden und authentifiziert")

    async def disconnect(self) -> None:
        """Trennt die WebSocket-Verbindung sauber."""
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("HA WebSocket getrennt")

    async def __aenter__(self) -> HAWebSocketClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # -------------------------------------------------------------------------
    # One-Shot-Anfragen
    # -------------------------------------------------------------------------

    async def get_states(self) -> list[dict[str, Any]]:
        """Gibt alle aktuellen Entity-States zurück."""
        result = await self._request({"type": "get_states"})
        return result.get("result", [])

    async def get_config(self) -> dict[str, Any]:
        """Gibt die HA-Konfiguration zurück."""
        result = await self._request({"type": "get_config"})
        return result.get("result", {})

    async def get_services(self) -> dict[str, Any]:
        """Gibt alle registrierten Services/Actions zurück."""
        result = await self._request({"type": "get_services"})
        return result.get("result", {})

    async def render_template(self, template: str) -> str:
        """Rendert ein Jinja2-Template in HA und gibt das Ergebnis zurück."""
        msg_id = self._next_id()
        result = await self._request({
            "id": msg_id,
            "type": "render_template",
            "template": template,
        })
        return result.get("result", "")

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ruft einen HA Service/Action auf."""
        payload: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if service_data:
            payload["service_data"] = service_data
        if target:
            payload["target"] = target
        return await self._request(payload)

    async def fetch_logs(self, source: str = "core", lines: int = 500) -> str:
        """
        Holt Logs über die HA REST API (nicht WebSocket).

        source: core | supervisor | addon/<slug>
        """
        import httpx

        base = self.ha_url
        headers = {"Authorization": f"Bearer {self.token}"}

        endpoints = {
            "core": f"{base}/api/error_log",
            "supervisor": f"{base}/api/hassio/supervisor/logs",
        }
        url = endpoints.get(source, f"{base}/api/hassio/addons/{source}/logs")

        async with httpx.AsyncClient(
            headers=headers, verify=self.verify_ssl, timeout=self.timeout
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    # -------------------------------------------------------------------------
    # Event-Subscriptions
    # -------------------------------------------------------------------------

    async def subscribe_events(
        self,
        event_type: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> int:
        """Abonniert einen Event-Typ und ruft callback bei jedem Event auf."""
        msg_id = self._next_id()
        self._subscriptions[msg_id] = callback
        await self._send({
            "id": msg_id,
            "type": "subscribe_events",
            "event_type": event_type,
        })
        return msg_id

    async def unsubscribe(self, subscription_id: int) -> None:
        """Beendet ein Event-Abonnement."""
        self._subscriptions.pop(subscription_id, None)
        await self._send({
            "id": self._next_id(),
            "type": "unsubscribe_events",
            "subscription": subscription_id,
        })

    async def stream_log_entries(
        self,
        source: str = "core",
    ) -> AsyncIterator[str]:
        """
        Streamt neue Log-Zeilen als AsyncIterator.

        Nutzt Polling der REST API (WebSocket unterstützt kein Log-Streaming direkt).
        """
        last_content = ""
        while self._connected:
            try:
                current = await self.fetch_logs(source)
                if current != last_content:
                    new_lines = current[len(last_content):]
                    if new_lines.strip():
                        yield new_lines
                    last_content = current
            except Exception as exc:
                logger.warning("Log-Streaming Fehler: %s", exc)
            await asyncio.sleep(5)

    # -------------------------------------------------------------------------
    # Interner Protokoll-Layer
    # -------------------------------------------------------------------------

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send_raw(self, data: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(data))

    async def _send(self, data: dict[str, Any]) -> None:
        if "id" not in data:
            data["id"] = self._next_id()
        await self._send_raw(data)

    async def _recv_raw(self) -> dict[str, Any]:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.timeout)
        return json.loads(raw)

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sendet eine Anfrage und wartet auf die Antwort mit passendem ID."""
        if "id" not in payload:
            payload["id"] = self._next_id()
        msg_id: int = payload["id"]

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        await self._send_raw(payload)

        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise HAWebSocketError(f"Timeout bei Anfrage {msg_id}") from exc

    async def _reader_loop(self) -> None:
        """Hintergrund-Task: liest eingehende Nachrichten und dispatcht sie."""
        try:
            while self._connected and self._ws:
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                    data: dict[str, Any] = json.loads(raw)
                    await self._dispatch(data)
                except asyncio.TimeoutError:
                    continue
        except Exception as exc:
            logger.error("WebSocket Reader-Loop unterbrochen: %s", exc)
            self._connected = False

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """Verteilt eine eingehende Nachricht an pending Futures oder Subscriptions."""
        msg_id = data.get("id")
        msg_type = data.get("type")

        if msg_type == "result" and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if data.get("success"):
                    future.set_result(data)
                else:
                    future.set_exception(
                        HAWebSocketError(f"HA Fehler: {data.get('error', {})}")
                    )

        elif msg_type == "event" and msg_id in self._subscriptions:
            callback = self._subscriptions[msg_id]
            try:
                callback(data.get("event", {}))
            except Exception as exc:
                logger.warning("Event-Callback Fehler: %s", exc)
