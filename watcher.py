"""The watch loop: one asyncio task per render job.

Each check:
  1. make sure we have a Phaze connection to the machine (through the local MCP)
  2. take a screenshot and diff it against the last one (free)
  3. only if something interesting happened, ask Claude what the screen shows
  4. turn that into a state, and tell Slack about changes (alerts, milestones, finished)
"""
import asyncio
import time
import traceback

import profiles
import timelapse
from phaze_mcp import NOT_CAPTURING, NOT_READY, PhazeMCPError, agent_connection_to, find_connection, primary_output
from store import ACTIVE, Store
from vision import BIG_CHANGE, SMALL_CHANGE, screen_change

ALERT_STATES = {"stalled", "error_dialog", "not_responding", "app_closed", "offline", "locked"}
MILESTONES = (25, 50, 75)
CONNECT_TIMEOUT_SEC = 45
STALL_VOTES_NEEDED = 2  # with Claude: two stall verdicts in a row before alerting, to ride out slow frames

# What the dashboard shows while Render Buddy can't see the screen.
BLIND_SUMMARY = {
    "locked": "Windows is locked. Connect and sign in so Render Buddy can see the render again.",
    "offline": "The machine is offline or not reachable from this Phaze login.",
}

ALERT_TEXT = {
    "stalled": ":hourglass: *Render looks stalled.* The screen hasn't changed in {mins} min.",
    "error_dialog": ":x: *Render error on screen.*",
    "not_responding": ":warning: *The render app is not responding.*",
    "app_closed": ":ghost: *The render app is gone.* No render window is visible.",
    "offline": ":electric_plug: *{machine} is offline* (or not reachable from this Phaze login).",
    "locked": ":lock: *{machine} is locked at the Windows sign-in screen.* The render may still be running, but "
              "Render Buddy can't see it until someone signs in. Click *Connect and sign in* to open Phaze and log in; "
              "watching picks up again on its own.",
}


class Hub:
    """Fan-out of live job updates to the web page (server-sent events)."""

    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        for q in self.queues:
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)


class ConsoleNotifier:
    """Stands in for Slack (--no-slack): prints what would be posted."""

    async def job_started(self, job: dict) -> str | None:
        print(f"[notify] watching {job['machine_name']} ({profiles.get(job['profile'])['label']})")
        return None

    async def update(self, job: dict, text: str, png: bytes | None = None) -> None:
        print(f"[notify] {job['machine_name']}: {text}" + (" [+screenshot]" if png else ""))

    async def alert(self, job: dict, text: str, png: bytes | None = None) -> None:
        print(f"[ALERT]  {job['machine_name']}: {text}" + (" [+screenshot]" if png else ""))

    async def finished(self, job: dict, text: str, png: bytes | None, gif_path) -> None:
        print(f"[notify] {job['machine_name']}: {text}" + (f" [timelapse: {gif_path}]" if gif_path else ""))


class Watcher:
    def __init__(self, job: dict, mcp, vision, notifier, store: Store, hub: Hub, cfg, clock=time.time):
        self.job = job
        self.mcp, self.vision, self.notifier, self.store, self.hub, self.cfg = mcp, vision, notifier, store, hub, cfg
        self.clock = clock
        self.dir = store.job_dir(job["id"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.connection_id: str | None = job.get("connection_id")
        self.output: int | None = None
        self.last_png: bytes | None = None
        self.last_change_at = clock()
        self.last_vision_at = 0.0
        self.stall_votes = 0
        self.alerted: str | None = job["state"] if job.get("state") in ALERT_STATES else None
        self.milestones = {m for m in MILESTONES if (job.get("progress_pct") or 0) >= m}
        self.snapshot_requested = False
        self.blind = False  # the last check couldn't capture the screen (offline, or the secure sign-in screen)
        self.wake = asyncio.Event()
        self.done = False

    # ---------- loop ----------
    @property
    def interval(self) -> int:
        return self.job.get("check_interval_sec") or self.cfg.check_interval_sec

    @property
    def stall_seconds(self) -> int:
        return (self.job.get("stall_minutes") or self.cfg.stall_minutes) * 60

    async def run(self) -> None:
        while not self.done:
            try:
                await self.step()
            except Exception as e:
                print(f"[watch]  {self.job['machine_name']}: check failed: {type(e).__name__}: {e}")
                traceback.print_exc()
            if self.done:
                break
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def request_snapshot(self) -> None:
        self.snapshot_requested = True
        self.wake.set()

    async def step(self) -> None:
        now = self.clock()
        self._save(last_check_at=now)
        png = await self._capture()
        if png is None:
            return

        back, self.blind = self.blind, False  # can see again after a gap: look fresh
        change = screen_change(self.last_png, png)
        first = self.last_png is None
        self.last_png = png
        (self.dir / "latest.png").write_bytes(png)
        # A locked screen isn't a stall: keep the stall clock fresh until someone signs in.
        if change >= SMALL_CHANGE or back or self.job["state"] == "locked":
            self.last_change_at = now
        unchanged_for = now - self.last_change_at
        suspect_stall = unchanged_for >= self.stall_seconds

        result = None
        if self.vision is None:
            state = "stalled" if suspect_stall else "rendering"
            mins = round(unchanged_for / 60)
            result = {"app": "", "summary": "The screen is changing." if mins < 1 else
                      f"The screen hasn't changed for {mins} min.", "error_text": None,
                      "progress_pct": None, "frame": None, "total_frames": None, "eta": None}
        elif (first or back or change >= BIG_CHANGE or suspect_stall or self.snapshot_requested
              or now - self.last_vision_at >= self.cfg.heartbeat_minutes * 60):
            result = await self.vision.assess(png, self.job["profile"], self._context(unchanged_for))
            self.last_vision_at = now
            state = self._vision_state(result["state"], suspect_stall)
        else:
            # Nothing interesting happened, so don't pay for a look. A small change means it's moving
            # (except on the lock screen, where the clock ticks; signing in is a big change and gets a look).
            state = self.job["state"]
            if back or (state in ("starting", "stalled", "unknown") and change >= SMALL_CHANGE):
                state = "rendering"

        if first:
            await self._notify("update", f"First look: {result['summary'] if result else 'screen captured.'}", png)
        await self._apply(state, result, png, unchanged_for)

    def _vision_state(self, seen: str, suspect_stall: bool) -> str:
        """Combine Claude's verdict with the pixel diff. Only the diff can prove a stall."""
        if seen in ("rendering", "stalled", "unknown"):
            if suspect_stall:
                self.stall_votes += 1
                return "stalled" if self.stall_votes >= STALL_VOTES_NEEDED else self.job["state"]
            self.stall_votes = 0
            return "unknown" if seen == "unknown" else "rendering"
        self.stall_votes = 0
        return seen

    def _context(self, unchanged_for: float) -> str:
        parts = []
        if self.job.get("summary"):
            parts.append(f"Last check said: {self.job['summary']}")
        if unchanged_for >= 60:
            parts.append(f"The screen has not visibly changed for {unchanged_for / 60:.0f} minutes.")
        return " ".join(parts)

    # ---------- state changes ----------
    async def _apply(self, state: str, result: dict | None, png: bytes | None, unchanged_for: float = 0) -> None:
        fields = {"state": state}
        if state in BLIND_SUMMARY and not (result and self.vision):
            fields["summary"] = BLIND_SUMMARY[state]
        if result:
            fields.update({k: result[k] for k in ("app", "summary", "error_text")})
            fields.update({k: result[k] for k in ("progress_pct", "frame", "total_frames", "eta")
                           if result[k] is not None})
        self._save(**fields)

        if png and state not in BLIND_SUMMARY:
            timelapse.save_frame(self.dir, png)  # lock-screen frames would only clutter the timelapse
        if state == "finished":
            await self._finish(png)
            return

        crossed = [m for m in MILESTONES if (self.job.get("progress_pct") or 0) >= m and m not in self.milestones]
        if crossed:
            self.milestones.update(crossed)
            await self._notify("update", f":checkered_flag: *{max(crossed)}%* — {self.job.get('summary') or ''}", png)

        if state in ALERT_STATES and state != self.alerted:
            self.alerted = state
            text = ALERT_TEXT[state].format(mins=round(unchanged_for / 60), machine=self.job["machine_name"])
            detail = self.job.get("error_text") if state == "error_dialog" else None
            detail = detail or (self.job.get("summary") if result and self.vision and state != "locked" else None)
            await self._notify("alert", text + (f"\n> {detail}" if detail else ""), png, state)
        elif self.alerted and state not in ALERT_STATES and (state == "rendering" or self.alerted == "locked"):
            was, self.alerted = self.alerted, None
            lead = (f":unlock: *Signed in* on {self.job['machine_name']}. Watching the render again."
                    if was == "locked" else ":white_check_mark: Back to rendering.")
            await self._notify("update", f"{lead} {self.job.get('summary') or ''}".strip(), png)

        if self.snapshot_requested:
            self.snapshot_requested = False
            await self._notify("update", f":camera: Snapshot: {self.job.get('summary') or state}", png, force=True)

    async def _finish(self, png: bytes | None) -> None:
        gif = await asyncio.to_thread(timelapse.build_gif, self.dir)
        took = _duration(self.clock() - self.job["started_at"])
        text = f":tada: *Render finished* on {self.job['machine_name']} after {took}. {self.job.get('summary') or ''}"
        self.store.add_event(self.job["id"], "finished", "finished", text)
        await self.notifier.finished(self.job, text, png, gif)
        await self.end("finished")

    async def end(self, status: str) -> None:
        """Stop watching and close our connection (never the user's own session)."""
        self.done = True
        self.wake.set()
        if self.connection_id:
            try:
                await self.mcp.disconnect(self.connection_id)
            except Exception as e:
                print(f"[watch]  disconnect failed: {e}")
        self.connection_id = None
        self._save(status=status, ended_at=self.clock(), connection_id=None)

    async def _notify(self, kind: str, text: str, png: bytes | None, state: str | None = None,
                      force: bool = False) -> None:
        self.store.add_event(self.job["id"], kind, state or self.job["state"], text)
        self.hub.publish({"type": "event", "job_id": self.job["id"], "kind": kind, "text": text})
        if not force and (self.job.get("muted_until") or 0) > self.clock():
            return
        try:
            await getattr(self.notifier, kind)(self.job, text, png)
        except Exception as e:
            print(f"[notify] failed to post {kind}: {e}")

    def _save(self, **fields) -> None:
        self.job.update(fields)
        self.store.update_job(self.job["id"], **fields)
        self.hub.publish({"type": "job", "job": self.job})

    # ---------- Phaze ----------
    async def _capture(self) -> bytes | None:
        if not await self._ensure_connected():
            return None
        try:
            return await self.mcp.screenshot(self.connection_id, self.output)
        except PhazeMCPError as e:
            if NOT_CAPTURING in str(e):
                self.blind = True
                await self._apply("locked", None, None)
            elif NOT_READY not in str(e):
                print(f"[watch]  screenshot failed, will reconnect: {e}")
                self.connection_id = None
            return None

    async def _ensure_connected(self) -> bool:
        """True when we have a live connection. Otherwise (re)connect, or record that the machine is offline."""
        status = await self.mcp.status()
        conn = find_connection(status, self.connection_id) if self.connection_id else None
        conn = conn or agent_connection_to(status, self.job["machine_id"])
        if conn and conn.get("connected"):
            self._use(conn)
            return True

        hosts = await self.mcp.list_hosts()
        host = next((h for h in hosts if h.get("machine_id") == self.job["machine_id"]), None)
        if not host or not host.get("online"):
            self.blind = True
            await self._apply("offline", None, None)
            return False
        if not conn:
            print(f"[watch]  connecting to {self.job['machine_name']}")
            self.connection_id = await self.mcp.connect(host["peer_id"], host["machine_id"],
                                                        bool(host.get("requires_relay")))
            self._save(connection_id=self.connection_id)
        deadline = self.clock() + CONNECT_TIMEOUT_SEC
        while self.clock() < deadline:
            await asyncio.sleep(1.5)
            conn = find_connection(await self.mcp.status(), self.connection_id)
            if conn and conn.get("connected"):
                self._use(conn)
                return True
        print(f"[watch]  connection to {self.job['machine_name']} didn't land yet; retrying next check")
        return False

    def _use(self, conn: dict) -> None:
        if conn["connection_id"] != self.connection_id:
            self.connection_id = conn["connection_id"]
            self._save(connection_id=self.connection_id)
        self.output = primary_output(conn)


class Manager:
    """Owns all watchers. The web server and the Slack buttons both go through here."""

    def __init__(self, mcp, vision, store: Store, hub: Hub, cfg, notifier=None):
        self.mcp, self.vision, self.store, self.hub, self.cfg = mcp, vision, store, hub, cfg
        self.notifier = notifier or ConsoleNotifier()
        self.watchers: dict[str, Watcher] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def start(self, machine_id: str, machine_name: str, profile: str,
                    check_interval_sec: int | None = None, stall_minutes: int | None = None) -> dict:
        if any(w.job["machine_id"] == machine_id for w in self.watchers.values()):
            raise ValueError(f"{machine_name} is already being watched.")
        job = self.store.create_job(
            machine_id=machine_id, machine_name=machine_name, profile=profile if profile in profiles.PROFILES
            else "generic", check_interval_sec=check_interval_sec or self.cfg.check_interval_sec,
            stall_minutes=stall_minutes or self.cfg.stall_minutes)
        ts = await self.notifier.job_started(job)
        if ts:
            job["slack_ts"] = ts
            self.store.update_job(job["id"], slack_ts=ts)
        self._spawn(job)
        return job

    async def resume(self) -> None:
        for job in self.store.list_jobs(active=True):
            print(f"[watch]  resuming {job['machine_name']}")
            self._spawn(job)

    def _spawn(self, job: dict) -> None:
        w = Watcher(job, self.mcp, self.vision, self.notifier, self.store, self.hub, self.cfg)
        self.watchers[job["id"]] = w
        task = asyncio.create_task(w.run())
        self.tasks[job["id"]] = task
        task.add_done_callback(lambda _t, jid=job["id"]: self._forget(jid))

    def _forget(self, job_id: str) -> None:
        self.watchers.pop(job_id, None)
        self.tasks.pop(job_id, None)

    def get(self, job_id: str) -> Watcher | None:
        return self.watchers.get(job_id)

    async def stop(self, job_id: str, by: str = "") -> bool:
        w = self.watchers.get(job_id)
        if not w:
            return False
        await w.end("stopped")
        await self.notifier.update(w.job, f":octagonal_sign: Stopped watching{f' ({by})' if by else ''}.")
        return True

    def snapshot(self, job_id: str) -> bool:
        w = self.watchers.get(job_id)
        if w:
            w.request_snapshot()
        return bool(w)

    def mute(self, job_id: str, minutes: int = 60) -> bool:
        w = self.watchers.get(job_id)
        if w:
            w._save(muted_until=time.time() + minutes * 60 if minutes else 0)
        return bool(w)

    async def shutdown(self) -> None:
        """Ctrl+C: stop the loops and close our connections. Jobs stay 'active' and resume next start."""
        for w in list(self.watchers.values()):
            w.done = True
            w.wake.set()
            if w.connection_id:
                try:
                    await self.mcp.disconnect(w.connection_id)
                except Exception:
                    pass
            self.store.update_job(w.job["id"], connection_id=None)
        for t in list(self.tasks.values()):
            t.cancel()


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def is_active(job: dict) -> bool:
    return job.get("status") == ACTIVE
