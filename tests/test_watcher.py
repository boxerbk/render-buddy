"""Offline tests: fake Phaze MCP, fake Claude, fake Slack. Run with `pytest`."""
import asyncio
import io
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phaze_mcp import PhazeMCPError  # noqa: E402
from store import Store  # noqa: E402
from watcher import Hub, Manager, Watcher  # noqa: E402

MACHINE = "m1"


def png(label: str, big: bool = False) -> bytes:
    """A fake 1280x720 screenshot. `label` changes a little text; `big` changes most of the screen."""
    img = Image.new("RGB", (1280, 720), (200, 30, 30) if big else (30, 30, 50))
    ImageDraw.Draw(img).text((40, 40), label, fill=(255, 255, 255), font_size=48)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


class FakeMCP:
    def __init__(self):
        self.online = True
        self.screens: list = []
        self.disconnected: list[str] = []

    async def status(self):
        return {"connections": [{"connection_id": "c1", "owner": "agent", "connected": True,
                                 "host_machine_id": MACHINE, "outputs": [{"id": 7, "primary": True}]}]
                if self.online else []}

    async def list_hosts(self):
        return [{"machine_id": MACHINE, "peer_id": "p1", "name": "Render-01", "online": self.online}]

    async def connect(self, peer_id, machine_id, relay=False):
        return "c1"

    async def screenshot(self, connection_id, output=None):
        item = self.screens.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def disconnect(self, connection_id):
        self.disconnected.append(connection_id)


class FakeVision:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    async def assess(self, png, profile, context=""):
        self.calls += 1
        state, pct = self.states.pop(0) if self.states else ("rendering", None)
        return {"app": "Blender", "state": state, "progress_pct": pct, "frame": None, "total_frames": None,
                "eta": None, "error_text": "CUDA out of memory" if state == "error_dialog" else None,
                "summary": f"Blender is {state}."}


class Notes:
    def __init__(self):
        self.posts: list[tuple[str, str, bool]] = []

    async def job_started(self, job):
        return "ts1"

    async def update(self, job, text, png=None):
        self.posts.append(("update", text, png is not None))

    async def alert(self, job, text, png=None):
        self.posts.append(("alert", text, png is not None))

    async def finished(self, job, text, png, gif):
        self.posts.append(("finished", text, gif is not None))

    def kinds(self):
        return [k for k, _, _ in self.posts]


@dataclass
class Cfg:
    check_interval_sec: int = 60
    stall_minutes: int = 10
    heartbeat_minutes: int = 15


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, vision=None):
    store, clock, mcp, notes = Store(tmp_path), Clock(), FakeMCP(), Notes()
    job = store.create_job(machine_id=MACHINE, machine_name="Render-01", profile="blender",
                           check_interval_sec=60, stall_minutes=10)
    w = Watcher(job, mcp, vision, notes, store, Hub(), Cfg(), clock=clock)
    return w, mcp, notes, clock, store


def run_checks(w, clock, mcp, screens, minutes_apart=1):
    async def go():
        for s in screens:
            mcp.screens.append(s)
            await w.step()
            clock.t += minutes_apart * 60
    asyncio.run(go())


def test_stall_without_claude_alerts_once_after_stall_minutes(tmp_path):
    w, mcp, notes, clock, _ = make(tmp_path)
    frozen = png("frame 10")
    run_checks(w, clock, mcp, [png("frame 9")] + [frozen] * 14)
    assert notes.kinds().count("alert") == 1
    assert "stalled" in next(t for k, t, _ in notes.posts if k == "alert")
    assert w.job["state"] == "stalled"


def test_stall_recovers_when_screen_moves_again(tmp_path):
    w, mcp, notes, clock, _ = make(tmp_path)
    frozen = png("frame 10")
    run_checks(w, clock, mcp, [frozen] * 12 + [png("frame 11")])
    assert w.job["state"] == "rendering"
    assert "Back to rendering" in notes.posts[-1][1]


def test_claude_needs_two_stall_votes_and_is_only_called_when_needed(tmp_path):
    vision = FakeVision()
    w, mcp, notes, clock, _ = make(tmp_path, vision)
    # Small changes every minute for 5 checks: only the first check should ask Claude.
    run_checks(w, clock, mcp, [png(f"frame {i}") for i in range(5)])
    assert vision.calls == 1
    # Then freeze. Claude is asked once the stall threshold is hit, and the alert needs two verdicts.
    frozen = png("frame 5")
    run_checks(w, clock, mcp, [frozen] * 11)
    assert notes.kinds().count("alert") == 0
    run_checks(w, clock, mcp, [frozen])
    assert notes.kinds().count("alert") == 1


def test_error_dialog_alerts_with_screenshot_and_error_text(tmp_path):
    vision = FakeVision(("rendering", 10), ("error_dialog", None))
    w, mcp, notes, clock, _ = make(tmp_path, vision)
    run_checks(w, clock, mcp, [png("frame 1"), png("CRASH", big=True)])
    kind, text, has_png = notes.posts[-1]
    assert kind == "alert" and has_png
    assert "CUDA out of memory" in text


def test_milestones_and_finish_build_a_timelapse(tmp_path):
    vision = FakeVision(("rendering", 10), ("rendering", 55), ("finished", 100))
    w, mcp, notes, clock, store = make(tmp_path, vision)
    run_checks(w, clock, mcp, [png("a"), png("b", big=True), png("c")], minutes_apart=20)
    milestones = [t for k, t, _ in notes.posts if "%*" in t]
    assert len(milestones) == 1 and "50%" in milestones[0]  # 25 and 50 crossed at once: one post
    assert notes.posts[-1][0] == "finished" and notes.posts[-1][2]  # with a GIF
    assert (store.job_dir(w.job["id"]) / "timelapse.gif").exists()
    assert store.get_job(w.job["id"])["status"] == "finished"
    assert mcp.disconnected == ["c1"]


def test_offline_and_locked_screen(tmp_path):
    w, mcp, notes, clock, _ = make(tmp_path)
    mcp.online = False
    run_checks(w, clock, mcp, [None, None])
    mcp.screens.clear()
    assert notes.kinds() == ["alert"] and "offline" in notes.posts[0][1]
    mcp.online = True
    run_checks(w, clock, mcp, [PhazeMCPError("phaze_screenshot: connection is not capturing")])
    kind, text, has_png = notes.posts[-1]
    assert kind == "alert" and "Connect and sign in" in text and not has_png  # nothing to show: Phaze can't capture it
    assert "Connect and sign in" in w.job["summary"]
    run_checks(w, clock, mcp, [png("back")])
    assert w.job["state"] == "rendering"
    assert "Signed in" in notes.posts[-1][1]


def test_mute_suppresses_alerts_but_snapshot_still_posts(tmp_path):
    w, mcp, notes, clock, _ = make(tmp_path)
    w._save(muted_until=clock.t + 3600)
    run_checks(w, clock, mcp, [png("x")] * 12)
    assert notes.kinds().count("alert") == 0
    w.request_snapshot()
    run_checks(w, clock, mcp, [png("x")])
    assert "Snapshot" in notes.posts[-1][1]


def test_jobs_resume_after_restart(tmp_path):
    store = Store(tmp_path)
    store.create_job(machine_id=MACHINE, machine_name="Render-01", profile="blender",
                     check_interval_sec=60, stall_minutes=10)

    async def go():
        m = Manager(FakeMCP(), None, Store(tmp_path), Hub(), Cfg(), notifier=Notes())
        await m.resume()
        assert len(m.watchers) == 1
        for t in m.tasks.values():
            t.cancel()
    asyncio.run(go())


def test_one_watch_per_machine(tmp_path):
    async def go():
        m = Manager(FakeMCP(), None, Store(tmp_path), Hub(), Cfg(), notifier=Notes())
        await m.start(MACHINE, "Render-01", "blender")
        try:
            await m.start(MACHINE, "Render-01", "blender")
            raise AssertionError("expected a duplicate watch to be refused")
        except ValueError:
            pass
        for t in m.tasks.values():
            t.cancel()
    asyncio.run(go())


def test_vision_request_shape_and_parsing():
    """Vision.assess sends an image + JSON schema and parses the reply (no network: mocked transport)."""
    import json

    import httpx2 as httpx  # anthropic 1.x is built on httpx2
    from anthropic import AsyncAnthropic

    from vision import Vision

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        answer = {"app": "Blender", "state": "rendering", "progress_pct": 40, "frame": 40, "total_frames": 100,
                  "eta": "00:10:00", "error_text": None, "summary": "Blender is rendering frame 40 of 100."}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    v = Vision("test-key", "claude-haiku-5-5")
    v.client = AsyncAnthropic(api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(v.assess(png("frame 40"), "blender"))
    assert result["frame"] == 40 and result["state"] == "rendering"
    body = seen["body"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert "locked" in body["output_config"]["format"]["schema"]["properties"]["state"]["enum"]


def test_visible_lock_screen_alerts_once_and_recovers_when_signed_in(tmp_path):
    """The Windows lock screen (photo + clock) can be captured; Claude recognises it."""
    vision = FakeVision(("rendering", 30), ("locked", None), ("locked", None), ("rendering", 31))
    w, mcp, notes, clock, store = make(tmp_path, vision)
    lock = [png(f"12:0{i}", big=True) for i in range(6)]  # the clock ticks: small changes only
    run_checks(w, clock, mcp, [png("frame 1")] + lock)
    alerts = [(t, has_png) for k, t, has_png in notes.posts if k == "alert"]
    assert len(alerts) == 1 and "Connect and sign in" in alerts[0][0] and alerts[0][1]
    assert vision.calls == 2  # the ticking clock doesn't cost a Claude call every minute
    assert w.job["state"] == "locked"
    # 15 minutes on the lock screen isn't a stall (the heartbeat look still sees the lock screen)
    run_checks(w, clock, mcp, [lock[-1]] * 10)
    assert [k for k, _, _ in notes.posts].count("alert") == 1
    # Someone signs in: the screen changes completely, Claude looks, and the thread hears about it
    run_checks(w, clock, mcp, [png("frame 2")])
    assert w.job["state"] == "rendering"
    assert "Signed in" in notes.posts[-1][1]
    frames = list((store.job_dir(w.job["id"]) / "frames").glob("*.jpg"))
    assert len(frames) == 2  # lock-screen frames stay out of the timelapse


def test_slack_connect_button_says_sign_in_when_locked():
    from slack_bot import _buttons

    job = {"id": "j1", "machine_id": MACHINE, "state": "locked"}
    connect = _buttons(job)["elements"][0]
    assert connect["text"]["text"] == "Connect and sign in"
    assert connect["url"] == f"phaze://connect?id={MACHINE}"
    assert _buttons({**job, "state": "stalled"})["elements"][0]["text"]["text"] == "Connect in Phaze"


# ---------- several machines at once ----------

class PhazeApp:
    """A fake Phaze app with real connection bookkeeping. With multi=False it behaves like Phaze with
    "Connect to multiple computers at once" turned off: a new connection closes the existing ones."""

    def __init__(self, multi: bool, machines=("m1", "m2")):
        self.multi = multi
        self.machines = list(machines)
        self.conns: dict[str, dict] = {}
        self.ids = 0

    def user_session(self, machine: str) -> None:
        self.conns["user-1"] = {"connection_id": "user-1", "owner": "user", "connected": True,
                                "host_machine_id": machine, "outputs": [{"id": 1, "primary": True}]}

    async def status(self):
        return {"connections": [dict(c) for c in self.conns.values()]}

    async def list_hosts(self):
        return [{"machine_id": m, "peer_id": f"p-{m}", "name": m.upper(), "online": True} for m in self.machines]

    async def connect(self, peer_id, machine_id, relay=False):
        if not self.multi:
            self.conns.clear()
        self.ids += 1
        cid = f"c{self.ids}"
        self.conns[cid] = {"connection_id": cid, "owner": "agent", "connected": True,
                           "host_machine_id": machine_id, "outputs": [{"id": 1, "primary": True}]}
        return cid

    async def screenshot(self, connection_id, output=None):
        if connection_id not in self.conns:
            raise PhazeMCPError("phaze_screenshot: unknown connection")
        return png(f"{self.conns[connection_id]['host_machine_id']} {self.ids}")

    async def disconnect(self, connection_id):
        self.conns.pop(connection_id, None)


def two_watchers(tmp_path, app, monkeypatch):
    import watcher as watcher_mod
    from watcher import ConnectGate

    monkeypatch.setattr(watcher_mod, "CONNECT_POLL_SEC", 0)
    monkeypatch.setattr(watcher_mod, "SETTLE_SEC", 0)
    store, clock, notes, gate = Store(tmp_path), Clock(), Notes(), ConnectGate()
    ws = []
    for m in ("m1", "m2"):
        job = store.create_job(machine_id=m, machine_name=m.upper(), profile="blender",
                               check_interval_sec=60, stall_minutes=10)
        ws.append(Watcher(job, app, None, notes, store, Hub(), Cfg(), clock=clock, gate=gate))
    return ws, notes, gate, store


def test_watches_several_machines_at_once_when_phaze_allows_it(tmp_path, monkeypatch):
    app = PhazeApp(multi=True)
    (a, b), notes, gate, _ = two_watchers(tmp_path, app, monkeypatch)

    async def go():
        for _ in range(3):
            await asyncio.gather(a.step(), b.step())  # connects are serialized by the gate
    asyncio.run(go())
    assert {c["host_machine_id"] for c in app.conns.values()} == {"m1", "m2"}
    assert app.ids == 2  # each connected once, nobody knocked the other off
    assert "alert" not in notes.kinds()
    assert gate.single_connection_at is None


def test_single_connection_phaze_stops_the_new_watch_and_explains(tmp_path, monkeypatch):
    app = PhazeApp(multi=False)
    (a, b), notes, gate, store = two_watchers(tmp_path, app, monkeypatch)

    async def go():
        await a.step()           # RENDER m1 connects and is watched
        await b.step()           # m2's connection closes m1's: m2 backs off
        await a.step()           # m1 reconnects on its next check
        await a.step()
    asyncio.run(go())

    alerts = [t for k, t, _ in notes.posts if k == "alert"]
    assert len(alerts) == 1
    assert "Connect to multiple computers at once" in alerts[0] and "*M1*" in alerts[0]
    assert "help.phaze.app/articles/5766820272" in alerts[0]
    assert b.done and store.get_job(b.job["id"])["status"] == "failed"
    assert not a.done and [c["host_machine_id"] for c in app.conns.values()] == ["m1"]
    assert gate.single_connection_at is not None
    assert app.ids == 3  # m1, m2 (then dropped), m1 again: no back-and-forth fight


def test_single_connection_phaze_protects_your_own_session(tmp_path, monkeypatch):
    app = PhazeApp(multi=False)
    app.user_session("m2")
    (a, _), notes, _, _ = two_watchers(tmp_path, app, monkeypatch)
    asyncio.run(a.step())
    alert = next(t for k, t, _ in notes.posts if k == "alert")
    assert "your own Phaze session to *M2*" in alert
    assert a.done and not app.conns  # Render Buddy backed off rather than keep fighting your session
