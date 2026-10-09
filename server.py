"""The local web page and its JSON API.

The browser only ever talks to this server. This server is what talks to Phaze:
  - the Phaze REST API (with your API key) for the org's machines, names and online status
  - the local Phaze MCP for what this PC's Phaze login can reach, and for the watching itself
It binds to 127.0.0.1 only, and only answers requests addressed to localhost.
"""
import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import profiles
from config import ROOT
from phaze_api import PhazeAPI, PhazeAPIError
from store import to_json


class SessionIn(BaseModel):
    api_key: str


class JobIn(BaseModel):
    machine_id: str
    profile: str = "generic"
    check_interval_sec: int | None = None
    stall_minutes: int | None = None


def create_app(manager, cfg) -> FastAPI:
    app = FastAPI(title="Render Buddy", docs_url=None, redoc_url=None)
    # Blocks DNS-rebinding: a web page on another site can't reach this server through a hostname it controls.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    state = {"api": None, "orgs": []}

    def connect_api(key: str) -> None:
        api = PhazeAPI(key)
        orgs = api.list_orgs()  # also validates the key
        if state["api"]:
            state["api"].close()
        state.update(api=api, orgs=orgs)

    if cfg.phaze_api_key:
        try:
            connect_api(cfg.phaze_api_key)
        except Exception as e:
            print(f"[web]    PHAZE_API_KEY from .env didn't work: {e}")

    # ---------- session / status ----------
    @app.get("/api/status")
    async def status():
        try:
            phaze = await manager.mcp.status()
            mcp = {"ok": True, "app_version": phaze.get("app_version"), "authenticated": phaze.get("authenticated")}
        except Exception as e:
            mcp = {"ok": False, "error": str(e)}
        return {"api_key": bool(state["api"]), "orgs": [{"id": o["id"], "name": o.get("name")} for o in state["orgs"]],
                "mcp": mcp, "vision": cfg.vision_enabled, "model": cfg.model if cfg.vision_enabled else None,
                "slack": cfg.slack_enabled, "single_connection": manager.gate.single_connection_at is not None}

    @app.post("/api/session")
    async def set_session(body: SessionIn):
        try:
            await asyncio.to_thread(connect_api, body.api_key.strip())
        except PhazeAPIError as e:
            raise HTTPException(401 if e.status in (401, 403) else 502, "Phaze rejected that API key." if
                                e.status in (401, 403) else str(e))
        return await status()

    @app.delete("/api/session")
    async def clear_session():
        if state["api"]:
            state["api"].close()
        state.update(api=None, orgs=[])
        return {"ok": True}

    # ---------- machines ----------
    @app.get("/api/machines")
    async def machines():
        """REST machines merged with MCP hosts. Only MCP-reachable machines can be watched."""
        try:
            hosts = {h["machine_id"]: h for h in await manager.mcp.list_hosts()}
            mcp_error = None
        except Exception as e:
            hosts, mcp_error = {}, f"Can't reach the Phaze app's MCP at {cfg.phaze_mcp_url}: {e}"

        rows: dict[str, dict] = {}
        if state["api"]:
            for org in state["orgs"]:
                for m in await asyncio.to_thread(state["api"].list_machines, org["id"]):
                    rows[m["id"]] = {
                        "machine_id": m["id"], "name": m.get("name"), "online": m.get("is_online"),
                        "os": m.get("os"), "org": org.get("name"),
                        "assignee": (m.get("assignee") or {}).get("name") or (m.get("assignee") or {}).get("email"),
                        "group": (m.get("group") or {}).get("name"),
                    }
        for mid, h in hosts.items():
            row = rows.setdefault(mid, {"machine_id": mid, "name": h.get("name")})
            row["online"] = h.get("online")

        watching = {w.job["machine_id"]: w.job["id"] for w in manager.watchers.values()}
        for mid, row in rows.items():
            row["reachable"] = mid in hosts
            row["job_id"] = watching.get(mid)
        ordered = sorted(rows.values(), key=lambda r: (not r["reachable"], not r.get("online"), (r["name"] or "").lower()))
        return {"machines": ordered, "mcp_error": mcp_error, "source": "api+mcp" if state["api"] else "mcp"}

    @app.get("/api/profiles")
    async def list_profiles():
        return [{"id": k, "label": v["label"]} for k, v in profiles.PROFILES.items()]

    # ---------- jobs ----------
    @app.get("/api/jobs")
    async def jobs():
        return {"active": manager.store.list_jobs(active=True), "history": manager.store.list_jobs(active=False, limit=50)}

    @app.post("/api/jobs")
    async def start_job(body: JobIn):
        hosts = {h["machine_id"]: h for h in await manager.mcp.list_hosts()}
        host = hosts.get(body.machine_id)
        if not host:
            raise HTTPException(400, "This PC's Phaze login can't reach that machine.")
        try:
            return await manager.start(body.machine_id, host.get("name") or body.machine_id[:8], body.profile,
                                       body.check_interval_sec, body.stall_minutes)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.delete("/api/jobs/{job_id}")
    async def stop_job(job_id: str):
        if not await manager.stop(job_id, by="from the web page"):
            raise HTTPException(404, "Not being watched.")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/snapshot")
    async def snapshot(job_id: str):
        if not manager.snapshot(job_id):
            raise HTTPException(404, "Not being watched.")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/mute")
    async def mute(job_id: str, minutes: int = 60):
        if not manager.mute(job_id, minutes):
            raise HTTPException(404, "Not being watched.")
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str):
        return manager.store.events(job_id)

    @app.get("/api/jobs/{job_id}/latest.png")
    async def latest(job_id: str):
        return _file(manager.store.job_dir(job_id) / "latest.png", "image/png")

    @app.get("/api/jobs/{job_id}/timelapse.gif")
    async def gif(job_id: str):
        return _file(manager.store.job_dir(job_id) / "timelapse.gif", "image/gif")

    @app.get("/api/events")
    async def stream():
        """Server-sent events: live job updates for the dashboard."""
        q = manager.hub.subscribe()

        async def gen():
            try:
                yield ": hello\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {to_json(msg)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                manager.hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


def _file(path, media_type):
    if not path.exists():
        raise HTTPException(404, "Not there yet.")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

