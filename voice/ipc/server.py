"""WebSocket IPC server for Serena UI communication."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection

logger = logging.getLogger(__name__)


class IPCServer:
    """WebSocket server that bridges the Python backend with the Electron overlay.

    The server broadcasts state changes, transcriptions, responses, and dashboard
    data to all connected Electron clients.
    """

    def __init__(self) -> None:
        self._clients: set[ServerConnection] = set()
        self._server: Server | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # --- Server lifecycle ---

    async def start(self, port: int = 8765) -> None:
        """Start the WebSocket server on the given port."""
        self._server = await websockets.serve(
            self._handler,
            "localhost",
            port,
        )
        logger.info("IPC server listening on ws://localhost:%d", port)

    async def stop(self) -> None:
        """Shut down the server and disconnect all clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("IPC server stopped")

    # --- Connection handling ---

    async def _handler(self, ws: ServerConnection) -> None:
        """Handle a single client connection."""
        self._clients.add(ws)
        logger.info("Client connected (%d total)", len(self._clients))

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._handle_client_message(ws, msg)
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON message from client")
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Client disconnected (%d remaining)", len(self._clients))

    async def _handle_client_message(
        self, ws: ServerConnection, msg: dict[str, Any]
    ) -> None:
        """Process messages sent from the Electron UI to the backend."""
        msg_type = msg.get("type")

        if msg_type == "focus_mode":
            logger.info("Focus mode %s", "enabled" if msg.get("enabled") else "disabled")
            # The daemon's budget system can listen for this
        else:
            logger.debug("Received client message: %s", msg_type)

    # --- Broadcasting ---

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all connected clients."""
        if not self._clients:
            return

        payload = json.dumps(message)
        dead: list[ServerConnection] = []

        for client in self._clients:
            try:
                await client.send(payload)
            except websockets.ConnectionClosed:
                dead.append(client)

        for client in dead:
            self._clients.discard(client)

    # --- Convenience senders ---

    async def send_state(self, state: str) -> None:
        """Broadcast a state change (idle, listening, thinking, speaking)."""
        await self.broadcast({"type": "state_change", "state": state})

    async def send_transcription(self, text: str) -> None:
        """Broadcast a transcription of the user's speech."""
        await self.broadcast({"type": "transcription", "text": text})

    async def send_response(self, text: str) -> None:
        """Broadcast Serena's response text."""
        await self.broadcast({"type": "response", "text": text})

    async def send_dashboard_data(self, data: dict[str, Any]) -> None:
        """Broadcast dashboard data (calendar, weather, notifications)."""
        await self.broadcast({"type": "dashboard", "data": data})
