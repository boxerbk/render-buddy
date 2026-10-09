"""Two ways of looking at a screenshot.

1. `screen_change`: a free pixel diff. Runs on every check and decides whether Claude needs to look.
2. `Vision.assess`: Claude reads the screenshot and reports what the render is doing.
"""
import base64
import io
import json

from PIL import Image, ImageChops

import profiles

STATES = ["rendering", "stalled", "error_dialog", "not_responding", "app_closed", "locked", "finished", "unknown"]

# Diff thresholds, as the fraction of pixels that changed (on a 320x180 grayscale thumbnail).
SMALL_CHANGE = 0.0005   # ~30 pixels: a frame counter or progress bar ticking over
BIG_CHANGE = 0.25       # a quarter of the screen: a dialog appeared, the app closed, etc.


def screen_change(before: bytes | None, after: bytes) -> float:
    """Fraction of pixels (0..1) that visibly changed between two PNG screenshots."""
    if before is None:
        return 1.0

    def thumb(png: bytes) -> Image.Image:
        return Image.open(io.BytesIO(png)).convert("L").resize((320, 180))

    diff = ImageChops.difference(thumb(before), thumb(after))
    changed = sum(diff.point(lambda p: 255 if p > 24 else 0).histogram()[255:])
    return changed / (320 * 180)


SCHEMA = {
    "type": "object",
    "properties": {
        "app": {"type": "string", "description": "Renderer you can see, e.g. 'Blender', or '' if none"},
        "state": {"type": "string", "enum": STATES},
        "progress_pct": {"type": ["number", "null"], "description": "0-100, or null if not visible"},
        "frame": {"type": ["integer", "null"]},
        "total_frames": {"type": ["integer", "null"]},
        "eta": {"type": ["string", "null"], "description": "Time remaining exactly as shown, e.g. '00:12:30'"},
        "error_text": {"type": ["string", "null"], "description": "Text of any error dialog or message"},
        "summary": {"type": "string", "description": "One short sentence for a Slack message"},
    },
    "required": ["app", "state", "progress_pct", "frame", "total_frames", "eta", "error_text", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You watch screenshots of Windows PCs that are rendering video or 3D scenes, and report the \
state of the render. Read only what is visible; never guess numbers.

States:
- rendering: a render is visibly in progress.
- stalled: a render window is open but nothing indicates progress (e.g. the same frame and elapsed \
time as described in the context, or a frozen progress bar), and nothing else explains it.
- error_dialog: an error, crash, warning or "failed" message is on screen, or a crash reporter.
- not_responding: a window title says "(Not Responding)" or Windows shows a hung-app dialog.
- app_closed: no render app is visible (desktop, another app, or the renderer has quit).
- locked: the Windows lock screen (a full-screen photo with a large clock) or the sign-in screen \
(user name and password or PIN box). This wins over every other state.
- finished: the render says it is done/complete, or progress is at 100% with nothing left to render.
- unknown: you can't tell.

The summary is one plain sentence, e.g. "Blender is rendering frame 412 of 1200 (34%), about 2h left."."""


class Vision:
    def __init__(self, api_key: str, model: str):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def assess(self, png: bytes, profile: str, context: str = "") -> dict:
        """Ask Claude what the screenshot shows. Returns a dict matching SCHEMA."""
        image = base64.standard_b64encode(_shrink(png)).decode()
        prompt = f"Renderer hint: {profiles.get(profile)['hint']}"
        if context:
            prompt += f"\n\nContext from earlier checks: {context}"
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}},
                {"type": "text", "text": prompt},
            ]}],
        )
        if response.stop_reason == "refusal":
            return _unknown("Claude declined to read this screenshot.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return _unknown("Couldn't read Claude's answer.")


def _unknown(summary: str) -> dict:
    return {"app": "", "state": "unknown", "progress_pct": None, "frame": None, "total_frames": None,
            "eta": None, "error_text": None, "summary": summary}


def _shrink(png: bytes, max_width: int = 1568) -> bytes:
    """Claude downsizes big images anyway; doing it here saves upload time."""
    img = Image.open(io.BytesIO(png))
    if img.width <= max_width:
        return png
    img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
