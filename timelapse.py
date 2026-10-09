"""Keep a small JPEG of each check, and stitch them into a GIF when the render ends."""
import io
import time
from pathlib import Path

from PIL import Image

FRAME_WIDTH = 640
MAX_GIF_FRAMES = 60


def save_frame(job_dir: Path, png: bytes) -> None:
    frames = job_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img = img.resize((FRAME_WIDTH, round(img.height * FRAME_WIDTH / img.width)))
    img.save(frames / f"{int(time.time() * 1000)}.jpg", quality=70)


def build_gif(job_dir: Path) -> Path | None:
    """Evenly sample up to MAX_GIF_FRAMES frames into timelapse.gif. None if there's nothing to show."""
    paths = sorted((job_dir / "frames").glob("*.jpg"))
    if len(paths) < 2:
        return None
    step = max(1, len(paths) / MAX_GIF_FRAMES)
    picked = [paths[int(i * step)] for i in range(min(len(paths), MAX_GIF_FRAMES))]
    if picked[-1] != paths[-1]:
        picked[-1] = paths[-1]
    frames = [Image.open(p).convert("P", palette=Image.ADAPTIVE) for p in picked]
    durations = [150] * (len(frames) - 1) + [1500]  # linger on the final frame
    out = job_dir / "timelapse.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return out
