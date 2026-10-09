"""See the dashboard without Phaze, Claude or Slack: fake machines and fake render jobs.

    python demo.py            # http://127.0.0.1:8766, data in ./demo-data (deleted and re-seeded each run)

Also used to make the images in docs/.
"""
import argparse
import asyncio
import io
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import timelapse
from server import create_app
from store import Store
from watcher import Hub, Manager

ROOT = Path(__file__).parent
W, H = 1920, 1080


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


# ---------- fake screenshots ----------

def scene(w: int, h: int, done: float, hue: int = 0) -> Image.Image:
    """A 'rendered' 3D scene, filled in top-down up to `done` (0..1) like Cycles tiles."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):  # sky to floor gradient
        t = y / h
        sky = (int(40 + 60 * t), int(50 + 70 * t), int(110 + 80 * t)) if hue == 0 else (int(120 + 80 * t), int(70 + 50 * t), int(60 + 30 * t))
        d.line([(0, y), (w, y)], fill=sky if t < .62 else (58, 52, 60))
    for cx, cy, r, col in ((.30, .60, .16, (230, 120, 70)), (.58, .66, .11, (90, 200, 160)), (.76, .55, .19, (120, 120, 250))):
        x, y, rr = int(cx * w), int(cy * h), int(r * w)
        d.ellipse([x - rr, y + rr * .75, x + rr, y + rr * 1.1], fill=(35, 32, 40))  # shadow
        for i in range(rr, 0, -2):  # cheap shading
            k = i / rr
            c = tuple(min(255, int(v * (1.25 - .6 * k))) for v in col)
            off = int((1 - k) * rr * .35)
            d.ellipse([x - i - off, y - i - off, x + i - off, y + i - off], fill=c)
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    cut = int(h * done)  # unrendered tiles below the cut
    tiles = Image.new("RGB", (w, h - cut), (28, 28, 28))
    td = ImageDraw.Draw(tiles)
    for x in range(0, w, 64):
        td.line([(x, 0), (x, h)], fill=(40, 40, 40))
    for y in range(0, h, 64):
        td.line([(0, y), (w, y)], fill=(40, 40, 40))
    td.rectangle([0, 0, 63, 63], outline=(240, 160, 40), width=2)  # active tile
    img.paste(tiles, (0, cut))
    return img


def blender(frame: int, total: int, remaining: str, error: str | None = None, hue: int = 0) -> bytes:
    img = Image.new("RGB", (W, H), (48, 48, 48))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill=(35, 35, 35))
    d.text((14, 6), "Blender   File   Edit   Render   Window   Help        Layout   Modeling   Shading   Animation   Rendering   Compositing",
           fill=(200, 200, 200), font=font(15))
    # the render window
    x0, y0, x1, y1 = 160, 70, W - 160, H - 90
    d.rectangle([x0, y0, x1, y1], fill=(40, 40, 40), outline=(20, 20, 20), width=2)
    d.rectangle([x0, y0, x1, y0 + 34], fill=(56, 56, 56))
    d.text((x0 + 14, y0 + 7), "Blender Render", fill=(220, 220, 220), font=font(16))
    d.text((x0 + 14, y0 + 44),
           f"Frame:{frame} | Time:00:41.20 | Remaining:{remaining} | Mem:2.14G, Peak:2.31G | Scene | Sample 64/128",
           fill=(225, 225, 225), font=font(17))
    img.paste(scene(x1 - x0 - 40, y1 - y0 - 100, .58 if not error else .31, hue), (x0 + 20, y0 + 80))
    d.rectangle([0, H - 30, W, H], fill=(35, 35, 35))
    pct = frame / total * 100
    d.text((14, H - 25), f"Rendering frame {frame} / {total}   ({pct:.0f}%)", fill=(200, 200, 200), font=font(15))
    d.rectangle([W - 420, H - 21, W - 20, H - 10], fill=(70, 70, 70))
    d.rectangle([W - 420, H - 21, W - 420 + int(4 * pct), H - 10], fill=(80, 140, 230))
    if error:
        dim = Image.new("RGBA", (W, H), (0, 0, 0, 90))
        img.paste(dim, (0, 0), dim)
        bx, by = W // 2 - 330, H // 2 - 110
        d.rounded_rectangle([bx, by, bx + 660, by + 220], 8, fill=(62, 62, 62), outline=(20, 20, 20), width=2)
        d.ellipse([bx + 30, by + 50, bx + 76, by + 96], fill=(220, 60, 60))
        d.text((bx + 46, by + 52), "!", fill="white", font=font(32, True))
        d.text((bx + 100, by + 40), "Render failed", fill="white", font=font(24, True))
        d.text((bx + 100, by + 82), error, fill=(230, 230, 230), font=font(18))
        d.rounded_rectangle([bx + 540, by + 160, bx + 640, by + 200], 6, fill=(80, 120, 220))
        d.text((bx + 576, by + 168), "OK", fill="white", font=font(18, True))
    return png(img)


def resolve(pct: int, remaining: str) -> bytes:
    img = Image.new("RGB", (W, H), (30, 30, 34))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 34], fill=(22, 22, 25))
    d.text((16, 7), "DaVinci Resolve   File   Edit   Trim   Timeline   Clip   Mark   View   Playback   Workspace", fill=(190, 190, 195), font=font(15))
    img.paste(scene(1180, 664, 1.0, hue=1), (40, 70))  # viewer
    d.rectangle([1250, 70, W - 40, 740], fill=(38, 38, 43))
    d.text((1272, 86), "Render Queue", fill=(230, 230, 235), font=font(20, True))
    d.rounded_rectangle([1266, 130, W - 56, 280], 6, fill=(48, 48, 54))
    d.text((1286, 146), "Job 1  |  Timeline 1  |  H.265 Master", fill=(220, 220, 225), font=font(17))
    d.text((1286, 182), f"Rendering {pct}%   ·   Time remaining {remaining}", fill=(240, 180, 70), font=font(17))
    d.rectangle([1286, 228, W - 76, 240], fill=(70, 70, 76))
    d.rectangle([1286, 228, 1286 + int((W - 76 - 1286) * pct / 100), 240], fill=(240, 160, 50))
    d.rounded_rectangle([1266, 296, W - 56, 380], 6, fill=(44, 44, 50))
    d.text((1286, 312), "Job 2  |  Timeline 1  |  ProRes Proxy", fill=(170, 170, 175), font=font(17))
    d.text((1286, 342), "Queued", fill=(150, 150, 155), font=font(16))
    d.rectangle([0, 760, W, H], fill=(26, 26, 30))  # timeline
    for i, c in enumerate(((80, 110, 200), (90, 160, 120), (170, 100, 160))):
        d.rounded_rectangle([60 + i * 560, 820, 560 + i * 560, 880], 4, fill=c)
        d.rounded_rectangle([60 + i * 560, 900, 560 + i * 560, 940], 4, fill=(60, 120, 90))
    return png(img)


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ---------- fake Phaze + seeded jobs ----------

MACHINES = [
    ("a1" * 32, "RENDER-01", True), ("b2" * 32, "RENDER-02", True), ("c3" * 32, "RENDER-03", True),
    ("d4" * 32, "EDIT-BAY-1", True), ("e5" * 32, "RENDER-04", False),
]


class FakeMCP:
    async def status(self):
        return {"connected": True, "authenticated": True, "app_version": "0.8.0", "connections": []}

    async def list_hosts(self):
        return [{"machine_id": m, "peer_id": "p", "name": n, "online": o} for m, n, o in MACHINES]


@dataclass
class Cfg:
    phaze_api_key: str = ""
    phaze_mcp_url: str = "http://127.0.0.1:41010/mcp"
    vision_enabled: bool = True
    model: str = "claude-haiku-5-5"
    slack_enabled: bool = True
    check_interval_sec: int = 60
    stall_minutes: int = 10
    heartbeat_minutes: int = 15


@dataclass
class Placeholder:
    job: dict


def seed(data: Path) -> Manager:
    shutil.rmtree(data, ignore_errors=True)
    store, now = Store(data), time.time()
    m = Manager(FakeMCP(), None, store, Hub(), Cfg())

    def job(machine: int, profile: str, shot: bytes, **fields) -> dict:
        mid, name, _ = MACHINES[machine]
        j = store.create_job(machine_id=mid, machine_name=name, profile=profile, check_interval_sec=60, stall_minutes=10)
        fields.setdefault("last_check_at", now - 20)
        store.update_job(j["id"], **fields)
        d = store.job_dir(j["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "latest.png").write_bytes(shot)
        j.update(fields)
        m.watchers[j["id"]] = Placeholder(j)
        return j

    job(2, "resolve", resolve(61, "00:42:10"), state="stalled", app="DaVinci Resolve", progress_pct=61, eta="00:42:10",
        started_at=now - 3.4 * 3600, summary="The Resolve render has sat at 61% for 14 minutes with the same time remaining.")
    job(1, "blender", blender(388, 960, "00:02.55", error="CUDA error: Out of memory in cuMemAlloc_v2"),
        state="error_dialog", app="Blender", progress_pct=40, frame=388, total_frames=960, started_at=now - 2.1 * 3600,
        error_text="CUDA error: Out of memory in cuMemAlloc_v2",
        summary="A 'Render failed' dialog says CUDA ran out of memory on frame 388.")
    job(0, "blender", blender(412, 1200, "00:03.10"), state="rendering", app="Blender", progress_pct=34, frame=412,
        total_frames=1200, eta="2h 10m", started_at=now - 1.2 * 3600,
        summary="Blender is rendering frame 412 of 1200 (34%), about 2h 10m left.")

    # history: a finished render with a timelapse, and one someone stopped
    done = store.create_job(machine_id=MACHINES[3][0], machine_name="EDIT-BAY-1", profile="blender",
                            check_interval_sec=60, stall_minutes=10)
    frames = store.job_dir(done["id"]) / "frames"
    frames.mkdir(parents=True)
    for i in range(24):
        t = i / 23
        f = 1 + int(t * 239)
        shot = Image.open(io.BytesIO(blender(f, 240, f"{int((1 - t) * 95):02d}:{int((1 - t) * 3540) % 60:02d}.00", hue=i % 2 * 0)))
        shot = shot.resize((timelapse.FRAME_WIDTH, round(shot.height * timelapse.FRAME_WIDTH / shot.width)))
        shot.convert("RGB").save(frames / f"{1000 + i}.jpg", quality=70)
    timelapse.build_gif(store.job_dir(done["id"]))
    store.update_job(done["id"], status="finished", state="finished", app="Blender", progress_pct=100,
                     started_at=now - 26 * 3600, ended_at=now - 20.5 * 3600, summary="Blender finished all 240 frames.")
    stopped = store.create_job(machine_id=MACHINES[4][0], machine_name="RENDER-04", profile="unreal",
                               check_interval_sec=60, stall_minutes=10)
    store.update_job(stopped["id"], status="stopped", state="rendering", app="Unreal Engine",
                     started_at=now - 30 * 3600, ended_at=now - 29.2 * 3600)
    return m


async def main_async(port: int, data: Path):
    m = seed(data)
    server = uvicorn.Server(uvicorn.Config(create_app(m, Cfg()), host="127.0.0.1", port=port, log_level="warning"))
    print(f"[demo]   open http://127.0.0.1:{port}  (fake data in {data})")
    await server.serve()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--data", type=Path, default=ROOT / "demo-data")
    a = p.parse_args()
    try:
        asyncio.run(main_async(a.port, a.data))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
