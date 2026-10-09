"""Slack: one thread per render job, screenshots uploaded into it, and buttons on alerts.

Uses Socket Mode, so no public URL is needed (same setup as the help desk example).
"""
import re

import profiles

BUTTON = re.compile(r"^rb_(snapshot|mute|unmute|stop)$")


def phaze_connect_url(machine_id: str) -> str:
    """Deep link that opens a Phaze session to this machine on the clicker's computer."""
    return f"phaze://connect?id={machine_id}"


def _buttons(job: dict) -> dict:
    muted = bool(job.get("muted_until"))
    return {"type": "actions", "elements": [
        {"type": "button", "action_id": "rb_connect", "style": "primary",
         "text": {"type": "plain_text", "text": "Connect in Phaze"}, "url": phaze_connect_url(job["machine_id"])},
        {"type": "button", "action_id": "rb_snapshot", "value": job["id"],
         "text": {"type": "plain_text", "text": "Snapshot now"}},
        {"type": "button", "action_id": "rb_unmute" if muted else "rb_mute", "value": job["id"],
         "text": {"type": "plain_text", "text": "Unmute" if muted else "Mute 1h"}},
        {"type": "button", "action_id": "rb_stop", "value": job["id"], "style": "danger",
         "text": {"type": "plain_text", "text": "Stop watching"}},
    ]}


def _blocks(text: str, job: dict) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, _buttons(job)]


class SlackNotifier:
    def __init__(self, client, channel: str):
        self.client = client  # slack_sdk AsyncWebClient
        self.channel = channel

    async def job_started(self, job: dict) -> str | None:
        app = profiles.get(job["profile"])["label"]
        text = (f":movie_camera: *Watching a render on {job['machine_name']}* ({app}). "
                f"Checking every {job['check_interval_sec']}s; I'll post here if it stalls, errors or finishes.")
        r = await self.client.chat_postMessage(channel=self.channel, text=text, blocks=_blocks(text, job))
        return r["ts"]

    async def update(self, job: dict, text: str, png: bytes | None = None) -> None:
        if png:
            await self._upload(job, text, png, "screenshot.png")
        else:
            await self.client.chat_postMessage(channel=self.channel, thread_ts=job.get("slack_ts"), text=text)

    async def alert(self, job: dict, text: str, png: bytes | None = None) -> None:
        # Broadcast to the channel so the alert isn't buried in the thread, then attach the screenshot.
        await self.client.chat_postMessage(channel=self.channel, thread_ts=job.get("slack_ts"),
                                           reply_broadcast=True, text=text, blocks=_blocks(text, job))
        if png:
            await self._upload(job, None, png, "screenshot.png")

    async def finished(self, job: dict, text: str, png: bytes | None, gif_path) -> None:
        await self.client.chat_postMessage(channel=self.channel, thread_ts=job.get("slack_ts"),
                                           reply_broadcast=True, text=text)
        if gif_path:
            await self._upload(job, "Timelapse of the render", gif_path.read_bytes(), "timelapse.gif")
        elif png:
            await self._upload(job, None, png, "final.png")

    async def _upload(self, job: dict, comment: str | None, data: bytes, filename: str) -> None:
        await self.client.files_upload_v2(channel=self.channel, thread_ts=job.get("slack_ts"), file=data,
                                          filename=filename, initial_comment=comment)


def build_slack_app(manager, bot_token: str):
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)

    @app.action(BUTTON)
    async def on_button(ack, body, action, client):
        await ack()
        user, job_id, verb = body["user"]["id"], action.get("value"), action["action_id"][3:]
        channel = body["channel"]["id"]
        w = manager.get(job_id)
        if not w:
            await client.chat_postEphemeral(channel=channel, user=user, text="That render is no longer being watched.")
            return
        if verb == "snapshot":
            manager.snapshot(job_id)
            reply = f"<@{user}> asked for a snapshot."
        elif verb in ("mute", "unmute"):
            manager.mute(job_id, 60 if verb == "mute" else 0)
            reply = f"<@{user}> {'muted alerts for 1 hour' if verb == 'mute' else 'unmuted alerts'}."
        else:
            await manager.stop(job_id, by=f"<@{user}>")
            return
        await client.chat_postMessage(channel=channel, thread_ts=w.job.get("slack_ts"), text=reply)

    @app.action("rb_connect")
    async def on_connect(ack):
        await ack()  # Slack opens the phaze:// link itself; the ack just stops Slack showing an error

    return app
