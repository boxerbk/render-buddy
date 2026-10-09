"""Direct client for the local Phaze MCP server.

The Phaze app serves the MCP on 127.0.0.1:41010. It has no key: it acts as whoever is signed in
to the app on this PC. It speaks plain JSON-RPC over HTTP, so no MCP SDK is needed.

Render Buddy is observe-only, so this client deliberately has no mouse or keyboard methods.
"""
import asyncio
import base64
import itertools
import json

import httpx


class PhazeMCPError(RuntimeError):
    pass


# The Phaze app answers this while its main loop is busy (e.g. a connection is still landing).
TRANSIENT = ("timed out",)
# phaze_screenshot errors worth recognising.
NOT_READY = "not ready"           # first frame hasn't arrived yet; clears within seconds
NOT_CAPTURING = "not capturing"   # Windows sign-in / lock screen, which Phaze can't capture


class PhazeMCP:
    def __init__(self, url: str):
        self.http = httpx.AsyncClient(
            timeout=30,
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, name: str, retries: int = 4, **arguments) -> dict:
        """Call a tool, retrying transient failures with backoff. Returns the parsed JSON text."""
        result = await self.call_raw(name, retries, **arguments)
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"text": text}

    async def call_raw(self, name: str, retries: int = 4, **arguments) -> dict:
        for attempt in range(retries + 1):
            try:
                return await self._call_once(name, arguments)
            except (PhazeMCPError, httpx.TransportError) as e:
                transient = isinstance(e, httpx.TransportError) or any(t in str(e) for t in TRANSIENT)
                if not transient or attempt == retries:
                    raise
                await asyncio.sleep(2 * (attempt + 1))

    async def _call_once(self, name: str, arguments: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        r = await self.http.post(self.url, json=body)
        r.raise_for_status()
        msg = r.json()
        if "error" in msg:
            raise PhazeMCPError(f"{name}: {msg['error']}")
        result = msg.get("result", {})
        if result.get("isError"):
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            raise PhazeMCPError(f"{name}: {text}")
        return result

    async def status(self) -> dict:
        return await self.call("phaze_status")

    async def list_hosts(self) -> list[dict]:
        """Machines the signed-in account can reach: machine_id, peer_id, name, online, requires_relay."""
        return (await self.call("phaze_list_hosts")).get("hosts", [])

    async def connect(self, peer_id: str, machine_id: str, relay: bool = False) -> str:
        res = await self.call("phaze_connect", host_peer_id=peer_id, machine_id=machine_id, relay=relay)
        return res["connection_id"]

    async def screenshot(self, connection_id: str, output: int | None = None) -> bytes:
        """PNG bytes of one monitor (the primary when output is None)."""
        args = {"connection_id": connection_id}
        if output:
            args["output"] = output
        result = await self.call_raw("phaze_screenshot", retries=1, **args)
        for c in result.get("content", []):
            if c.get("type") == "image" and c.get("data"):
                return base64.b64decode(c["data"])
        raise PhazeMCPError("phaze_screenshot: no image in the response")

    async def disconnect(self, connection_id: str) -> dict:
        return await self.call("phaze_disconnect", connection_id=connection_id)

    async def aclose(self):
        await self.http.aclose()


# ---- helpers over a phaze_status payload ----

def find_connection(status: dict, connection_id: str) -> dict | None:
    return next((c for c in status.get("connections", []) if c.get("connection_id") == connection_id), None)


def agent_connection_to(status: dict, machine_id: str) -> dict | None:
    """An MCP-opened ("agent") connection to this machine that we can reuse, e.g. after a restart."""
    return next((c for c in status.get("connections", [])
                 if c.get("owner") == "agent" and c.get("host_machine_id") == machine_id), None)


def primary_output(conn: dict) -> int | None:
    outputs = conn.get("outputs") or []
    out = next((o for o in outputs if o.get("primary")), outputs[0] if outputs else None)
    return out.get("id") if out else None
