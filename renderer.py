import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import uuid
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import edge_tts
import aiohttp
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
FONT_REGULAR = BASE_DIR / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = BASE_DIR / "fonts" / "DejaVuSans-Bold.ttf"

WIDTH = 720
HEIGHT = 1280
FPS = 30

VI_OFFSET = 0.85
MIN_TICK_OFFSET = 1.95
TICK_DURATION = 2.80
# Keep the completed bar on screen briefly before the answer layer is allowed
# to appear. This removes the one-frame early flash seen on some encoders.
DING_GAP = 0.12
EN_GAP = 0.62
MIN_ITEM_DURATION = 6.0

# Locked 720 x 1280 layout measured from the supplied reference video.
TITLE_CENTER_Y = 253
SUBTITLE_CENTER_Y = 387
FLAG_PANEL_Y = 453
CARD_X0 = 72
CARD_X1 = 648
CARD_Y = 537
CARD_HEIGHT = 334
PROGRESS_X = 201
PROGRESS_Y = 679
PROGRESS_WIDTH = 318
PROGRESS_HEIGHT = 8
ENGLISH_TOP = 735
ENGLISH_SIZE = 46  # 15% larger than the former 40 px answer.
CTA_TOP = 909  # A further 11 px lower (approximately 0.3 cm at 96 dpi).

_EDGE_SSL_LOCK = threading.Lock()
_EDGE_SSL_READY = False
_EDGE_SSL_CONTEXT: ssl.SSLContext | None = None
_FFMPEG_EXE: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class Progress:
    def __init__(self, status_path: Path, payload: dict):
        self.status_path = status_path
        self.status = json.loads(status_path.read_text(encoding="utf-8"))
        self.total = max(1, payload["word_count"] * 2 + payload["script_count"] * 3 + 1)
        self.done = 0

    def update(self, phase: str, increment: int = 0, current_script: int | None = None) -> None:
        self.done += increment
        if current_script is not None:
            self.status["current_script"] = current_script
        self.status.update(
            {
                "state": "running",
                "phase": phase,
                "percent": min(98, max(2, round(self.done / self.total * 100))),
                "updated_at": now_iso(),
                "error": None,
            }
        )
        atomic_json(self.status_path, self.status)

    def add_result(self, name: str, size: int, job_id: str, script_index: int) -> None:
        results = self.status.setdefault("results", [])
        results = [entry for entry in results if entry.get("name") != name]
        results.append(
            {
                "name": name,
                "size_bytes": size,
                "script_index": script_index,
                "download_url": f"/api/jobs/{job_id}/files/{name}",
            }
        )
        self.status["results"] = sorted(results, key=lambda entry: entry["script_index"])
        atomic_json(self.status_path, self.status)

    def complete(self) -> None:
        self.status.update(
            {
                "state": "completed",
                "phase": "Đã render xong toàn bộ video",
                "percent": 100,
                "updated_at": now_iso(),
                "error": None,
            }
        )
        atomic_json(self.status_path, self.status)


def run_command(command: list[str], label: str) -> None:
    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        details = process.stderr.strip().splitlines()
        tail = "\n".join(details[-16:])
        raise RuntimeError(f"{label} thất bại. {tail}"[:1800])


def ffmpeg_executable() -> str:
    """Use the system FFmpeg when present, otherwise the bundled Python wheel."""
    global _FFMPEG_EXE
    if _FFMPEG_EXE:
        return _FFMPEG_EXE
    _FFMPEG_EXE = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    if not _FFMPEG_EXE or not Path(_FFMPEG_EXE).is_file():
        raise RuntimeError("Máy chủ chưa có FFmpeg để render video.")
    return _FFMPEG_EXE


def media_duration(path: Path) -> float:
    process = subprocess.run(
        [
            ffmpeg_executable(),
            "-hide_banner",
            "-i",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", process.stderr)
    if not match:
        raise RuntimeError(f"Không đọc được file media: {path.name}")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    if duration <= 0.05:
        raise RuntimeError(f"File media không hợp lệ: {path.name}")
    return duration


def _sound_sample(value: float) -> int:
    return max(-32767, min(32767, int(value * 32767)))


def ensure_sound_effects() -> tuple[Path, Path]:
    sound_dir = RUNTIME_DIR / "sounds"
    sound_dir.mkdir(parents=True, exist_ok=True)
    tick_path = sound_dir / "countdown-ticks-fast-2p8s.wav"
    ding_path = sound_dir / "answer-ting.wav"
    sample_rate = 48000

    if not tick_path.exists():
        duration = TICK_DURATION
        samples = []
        # Restore the fast 0.2-second rhythm from the earlier approved build,
        # extended across the full thinking period.
        tick_times = [index * 0.20 for index in range(14)]
        for index in range(int(sample_rate * duration)):
            t = index / sample_rate
            value = 0.0
            for tick_time in tick_times:
                local = t - tick_time
                if 0 <= local < 0.035:
                    envelope = math.exp(-local * 95)
                    value += 0.34 * envelope * (
                        math.sin(2 * math.pi * 1500 * local)
                        + 0.45 * math.sin(2 * math.pi * 2350 * local)
                    )
            samples.append(_sound_sample(value))
        with wave.open(str(tick_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"".join(struct.pack("<h", value) for value in samples))

    if not ding_path.exists():
        duration = 0.90
        samples = []
        for index in range(int(sample_rate * duration)):
            t = index / sample_rate
            attack = min(1.0, t / 0.012)
            envelope = attack * math.exp(-t * 4.4)
            shimmer = (
                math.sin(2 * math.pi * 1046.50 * t)
                + 0.48 * math.sin(2 * math.pi * 1567.98 * t)
                + 0.22 * math.sin(2 * math.pi * 2093.00 * t)
            )
            samples.append(_sound_sample(0.42 * envelope * shimmer))
        with wave.open(str(ding_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"".join(struct.pack("<h", value) for value in samples))

    return tick_path, ding_path


def _tts_cache_path(text: str, voice: str, rate: str) -> Path:
    cache_dir = RUNTIME_DIR / "tts-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{voice}\0{rate}\0{text}".encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.mp3"


def configure_edge_ssl() -> ssl.SSLContext:
    """Honor a host-provided CA bundle without disabling TLS verification."""
    global _EDGE_SSL_READY, _EDGE_SSL_CONTEXT
    if _EDGE_SSL_READY and _EDGE_SSL_CONTEXT is not None:
        return _EDGE_SSL_CONTEXT
    with _EDGE_SSL_LOCK:
        if _EDGE_SSL_READY and _EDGE_SSL_CONTEXT is not None:
            return _EDGE_SSL_CONTEXT
        import certifi
        import edge_tts.communicate

        context = ssl.create_default_context(cafile=certifi.where())
        extra_ca = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
        if extra_ca and Path(extra_ca).is_file():
            context.load_verify_locations(cafile=extra_ca)
        edge_tts.communicate._SSL_CTX = context
        _EDGE_SSL_CONTEXT = context
        _EDGE_SSL_READY = True
        return context


def trim_tts_audio(source: Path, target: Path) -> None:
    run_command(
        [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB:"
            "start_silence=0.04:stop_periods=1:stop_duration=0.12:"
            "stop_threshold=-50dB:stop_silence=0.10",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(target),
        ],
        "Chuẩn hóa giọng đọc",
    )


async def _synthesize_one(text: str, voice: str, rate: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 800:
        return
    last_error: Exception | None = None
    max_attempts = 6
    retry_delays = (2.0, 4.0, 7.0, 11.0, 16.0)
    for attempt in range(max_attempts):
        token = uuid.uuid4().hex
        temp = target.with_name(f"{target.stem}.{token}.tmp.mp3")
        normalized = target.with_name(f"{target.stem}.{token}.normalized.mp3")
        try:
            configure_edge_ssl()
            connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                ttl_dns_cache=180,
                enable_cleanup_closed=True,
            )
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                connector=connector,
                connect_timeout=25,
                receive_timeout=90,
            )
            await asyncio.wait_for(communicate.save(str(temp)), timeout=115)
            if not temp.exists() or temp.stat().st_size <= 800:
                raise RuntimeError("Edge TTS không trả về dữ liệu âm thanh")
            await asyncio.to_thread(trim_tts_audio, temp, normalized)
            if not normalized.exists() or normalized.stat().st_size <= 800:
                raise RuntimeError("Không chuẩn hóa được dữ liệu âm thanh")
            os.replace(normalized, target)
            return
        except Exception as exc:
            last_error = exc
            temp.unlink(missing_ok=True)
            normalized.unlink(missing_ok=True)
            if attempt < max_attempts - 1:
                await asyncio.sleep(retry_delays[attempt] + random.uniform(0.0, 0.8))
    error_name = type(last_error).__name__ if last_error else "UnknownError"
    if isinstance(last_error, asyncio.TimeoutError):
        error_name = "Timeout"
    status = getattr(last_error, "status", None)
    if status:
        error_name = f"{error_name} HTTP {status}"
    raise RuntimeError(
        f"Edge TTS tạm thời không phản hồi cho '{text}' ({error_name}). "
        "App đã tự thử 6 lần. Bấm “Thử lại render” để tiếp tục mà không cần tải footage lại."
    ) from last_error


async def synthesize_all(
    payload: dict,
    on_progress: Callable[[str, int], None],
) -> dict[str, Path]:
    config = payload["config"]
    specs: dict[tuple[str, str, str], list[str]] = {}
    for script_index, script in enumerate(payload["scripts"]):
        for item_index, item in enumerate(script["items"]):
            vi_key = (item["vi"], config["vi_voice"], "+0%")
            en_key = (item["en"], config["en_voice"], config["en_rate"])
            specs.setdefault(vi_key, []).append(f"{script_index}:{item_index}:vi")
            specs.setdefault(en_key, []).append(f"{script_index}:{item_index}:en")

    # Edge's free websocket endpoint can throttle parallel handshakes. A single
    # stream is slower but much more reliable for unattended PandaStack jobs.
    semaphore = asyncio.Semaphore(1)
    result: dict[str, Path] = {}

    async def task(spec: tuple[str, str, str], refs: list[str]) -> tuple[list[str], Path, str]:
        text, voice, rate = spec
        target = _tts_cache_path(text, voice, rate)
        async with semaphore:
            await _synthesize_one(text, voice, rate, target)
        return refs, target, text

    tasks = [asyncio.create_task(task(spec, refs)) for spec, refs in specs.items()]
    for future in asyncio.as_completed(tasks):
        refs, target, text = await future
        for ref in refs:
            result[ref] = target
        on_progress(f"Đã tạo giọng đọc: {text}", len(refs))
    return result


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, selected_font: ImageFont.FreeTypeFont) -> float:
    box = draw.textbbox((0, 0), text, font=selected_font)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, selected_font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(draw, candidate, selected_font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fitted_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_lines: int,
    start_size: int,
    min_size: int,
    bold: bool = True,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(start_size, min_size - 1, -2):
        selected = font(size, bold=bold)
        lines = wrap_text(draw, text, selected, max_width)
        if len(lines) <= max_lines and all(text_width(draw, line, selected) <= max_width for line in lines):
            return selected, lines
    selected = font(min_size, bold=bold)
    return selected, wrap_text(draw, text, selected, max_width)[:max_lines]


def draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    selected_font: ImageFont.FreeTypeFont,
    center_x: int,
    top_y: int,
    fill: tuple[int, int, int, int],
    spacing: int = 8,
) -> int:
    y = top_y
    for line in lines:
        box = draw.textbbox((0, 0), line, font=selected_font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        draw.text((center_x - width / 2, y - box[1]), line, font=selected_font, fill=fill)
        y += height + spacing
    return y


def draw_vietnam_flag(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=3, fill=(218, 33, 39, 255))
    cx, cy = x + width / 2, y + height / 2
    outer, inner = height * 0.28, height * 0.11
    points = []
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = -math.pi / 2 + index * math.pi / 5
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    draw.polygon(points, fill=(255, 229, 35, 255))


def draw_us_flag(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=3, fill=(255, 255, 255, 255))
    stripe_h = height / 13
    for stripe in range(0, 13, 2):
        y0 = y + round(stripe * stripe_h)
        y1 = y + round((stripe + 1) * stripe_h)
        draw.rectangle((x, y0, x + width, y1), fill=(194, 33, 48, 255))
    canton_w, canton_h = round(width * 0.43), round(stripe_h * 7)
    draw.rectangle((x, y, x + canton_w, y + canton_h), fill=(36, 59, 115, 255))
    for row in range(3):
        for col in range(4):
            px = x + 4 + col * (canton_w - 8) / 3
            py = y + 4 + row * (canton_h - 8) / 2
            draw.ellipse((px - 1, py - 1, px + 1, py + 1), fill=(255, 255, 255, 255))


def clue_for(english: str) -> str:
    """Text-only representation used by previews/tests; never invent dashes."""
    letters = [character for character in english if character.isalpha()]
    first = letters[0].upper() if letters else "?"
    return first + ("_" * max(0, len(letters) - 1))


def _single_line_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    min_size: int,
    bold: bool = True,
) -> ImageFont.FreeTypeFont:
    """Keep answer glyphs on one shared line so clues can map 1:1 to them."""
    for size in range(start_size, min_size - 1, -1):
        selected = font(size, bold=bold)
        if text_width(draw, text, selected) <= max_width:
            return selected
    return font(min_size, bold=bold)


def english_answer_layout(draw: ImageDraw.ImageDraw, english: str) -> dict:
    """Return the one geometry used by both the clue and revealed answer.

    Every yellow dash is centered on the advance slot of the letter that will
    replace it. This prevents the word from jumping when the answer appears.
    """
    text = re.sub(r"\s+", " ", english.strip()) or "?"
    selected_font = _single_line_font(draw, text, 515, ENGLISH_SIZE, 27, bold=True)
    bbox = draw.textbbox((0, 0), text, font=selected_font)
    visual_width = bbox[2] - bbox[0]
    origin_x = WIDTH / 2 - visual_width / 2 - bbox[0]
    origin_y = ENGLISH_TOP - bbox[1]
    visual_height = bbox[3] - bbox[1]

    slots = []
    for index, character in enumerate(text):
        if not character.isalpha():
            continue
        before = draw.textlength(text[:index], font=selected_font)
        through = draw.textlength(text[: index + 1], font=selected_font)
        slots.append(
            {
                "index": index,
                "character": character,
                "start_x": origin_x + before,
                "end_x": origin_x + through,
                "center_x": origin_x + (before + through) / 2,
            }
        )
    return {
        "text": text,
        "font": selected_font,
        "font_size": selected_font.size,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "visual_top": ENGLISH_TOP,
        "visual_height": visual_height,
        "slots": slots,
    }


def draw_english_clue(draw: ImageDraw.ImageDraw, english: str) -> dict:
    layout = english_answer_layout(draw, english)
    slots = layout["slots"]
    if not slots:
        draw.text(
            (layout["origin_x"], layout["origin_y"]),
            "?",
            font=layout["font"],
            fill=(225, 242, 43, 255),
        )
        return layout

    first = slots[0]
    draw.text(
        (first["start_x"], layout["origin_y"]),
        first["character"].upper(),
        font=layout["font"],
        fill=(225, 242, 43, 255),
    )
    dash_y = ENGLISH_TOP + layout["visual_height"] - 1
    for slot in slots[1:]:
        slot_width = max(1.0, slot["end_x"] - slot["start_x"])
        dash_width = min(22.0, max(10.0, slot_width * 0.72))
        draw.rounded_rectangle(
            (
                round(slot["center_x"] - dash_width / 2),
                round(dash_y),
                round(slot["center_x"] + dash_width / 2),
                round(dash_y + 4),
            ),
            radius=2,
            fill=(225, 242, 43, 255),
        )
    return layout


def draw_english_answer(draw: ImageDraw.ImageDraw, english: str) -> dict:
    layout = english_answer_layout(draw, english)
    draw.text(
        (layout["origin_x"], layout["origin_y"]),
        layout["text"],
        font=layout["font"],
        fill=(225, 242, 43, 255),
    )
    return layout


def _italic_text_layer(
    text: str,
    selected_font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    highlight_phrase: str = "",
    highlight_fill: tuple[int, int, int, int] = (231, 244, 48, 255),
) -> Image.Image:
    """Create a synthetic oblique cut of the same DejaVu Sans face."""
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    probe_draw = ImageDraw.Draw(probe)
    bbox = probe_draw.textbbox((0, 0), text, font=selected_font)
    pad = 4
    width = max(1, bbox[2] - bbox[0] + pad * 2)
    height = max(1, bbox[3] - bbox[1] + pad * 2)
    source = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    source_draw = ImageDraw.Draw(source)
    source_draw.text(
        (pad - bbox[0], pad - bbox[1]),
        text,
        font=selected_font,
        fill=fill,
    )
    match_index = text.casefold().find(highlight_phrase.casefold()) if highlight_phrase else -1
    if match_index >= 0:
        prefix = text[:match_index]
        highlighted = text[match_index : match_index + len(highlight_phrase)]
        highlight_x = pad - bbox[0] + source_draw.textlength(prefix, font=selected_font)
        source_draw.text(
            (highlight_x, pad - bbox[1]),
            highlighted,
            font=selected_font,
            fill=highlight_fill,
        )
    shear = 0.20
    extra = math.ceil(height * shear)
    slanted = source.transform(
        (width + extra, height),
        Image.Transform.AFFINE,
        (1, shear, -shear * height, 0, 1, 0),
        resample=Image.Resampling.BICUBIC,
    )
    alpha_box = slanted.getchannel("A").getbbox()
    return slanted.crop(alpha_box) if alpha_box else slanted


def italic_centered_layout(
    draw: ImageDraw.ImageDraw,
    text: str,
    center_x: int,
    top_y: int,
) -> tuple[list[tuple[Image.Image, int, int]], tuple[int, int, int, int]]:
    selected_font, lines = fitted_lines(draw, text, 620, 2, 29, 22, bold=False)
    layers: list[tuple[Image.Image, int, int]] = []
    y = top_y
    for line in lines:
        layer = _italic_text_layer(
            line,
            selected_font,
            (255, 255, 255, 248),
            highlight_phrase="Vào nhóm",
        )
        x = round(center_x - layer.width / 2)
        layers.append((layer, x, y))
        y += layer.height + 7
    left = min(x for _, x, _ in layers)
    right = max(x + layer.width for layer, x, _ in layers)
    bottom = max(y + layer.height for layer, _, y in layers)
    return layers, (left, top_y, right, bottom)


def draw_down_hand(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Small emoji-style pointing hand for fonts that do not contain color emoji."""
    skin = (247, 193, 122, 255)
    edge = (194, 132, 67, 255)
    cuff = (238, 62, 69, 255)
    draw.rounded_rectangle((x + 4, y + 2, x + 24, y + 21), radius=7, fill=skin, outline=edge, width=2)
    draw.rounded_rectangle((x + 10, y + 14, x + 19, y + 38), radius=4, fill=skin, outline=edge, width=2)
    draw.rounded_rectangle((x, y + 8, x + 11, y + 16), radius=4, fill=skin, outline=edge, width=2)
    draw.rounded_rectangle((x + 19, y + 8, x + 29, y + 16), radius=4, fill=skin, outline=edge, width=2)
    draw.rectangle((x + 8, y - 2, x + 22, y + 5), fill=cuff)


def draw_subtitle_badge(draw: ImageDraw.ImageDraw, subtitle: str) -> None:
    """Draw only the TEST NHANH badge so FFmpeg can animate it separately."""
    if not subtitle:
        return
    sub_font, sub_lines = fitted_lines(draw, subtitle.upper(), 350, 1, 33, 24, bold=True)
    sub = sub_lines[0]
    sub_box = draw.textbbox((0, 0), sub, font=sub_font)
    sub_w = sub_box[2] - sub_box[0]
    draw.rounded_rectangle(
        (WIDTH / 2 - sub_w / 2 - 16, 356, WIDTH / 2 + sub_w / 2 + 16, 416),
        radius=7,
        fill=(10, 10, 10, 205),
    )
    draw.text(
        (WIDTH / 2 - (sub_box[0] + sub_box[2]) / 2, SUBTITLE_CENTER_Y - (sub_box[1] + sub_box[3]) / 2),
        sub,
        font=sub_font,
        fill=(234, 244, 51, 255),
    )


def render_subtitle_overlay(target: Path, subtitle: str) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_subtitle_badge(ImageDraw.Draw(image), subtitle)
    image.save(target, format="PNG", optimize=True)


def draw_header(draw: ImageDraw.ImageDraw, title: str, subtitle: str, show_subtitle: bool) -> None:
    title_font, title_lines = fitted_lines(draw, title.upper(), 560, 1, 43, 31, bold=True)
    label = title_lines[0]
    box = draw.textbbox((0, 0), label, font=title_font)
    label_w, label_h = box[2] - box[0], box[3] - box[1]
    panel_x0 = WIDTH / 2 - label_w / 2 - 21
    panel_y0 = 214
    panel_x1 = WIDTH / 2 + label_w / 2 + 21
    panel_y1 = 292
    draw.rounded_rectangle((panel_x0 + 4, panel_y0 + 7, panel_x1 + 4, panel_y1 + 7), radius=12, fill=(0, 0, 0, 105))
    draw.rounded_rectangle((panel_x0, panel_y0, panel_x1, panel_y1), radius=12, fill=(235, 19, 26, 255))
    draw.text(
        (WIDTH / 2 - (box[0] + box[2]) / 2, TITLE_CENTER_Y - (box[1] + box[3]) / 2),
        label,
        font=title_font,
        fill=(255, 244, 43, 255),
    )

    if show_subtitle:
        draw_subtitle_badge(draw, subtitle)

    flag_y = FLAG_PANEL_Y
    draw.rounded_rectangle((238, flag_y + 7, 490, flag_y + 83), radius=9, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((234, flag_y, 486, flag_y + 76), radius=9, fill=(8, 8, 8, 225))
    draw_vietnam_flag(draw, 250, flag_y + 16, 69, 44)
    arrow_font = font(36, bold=True)
    arrow = "→"
    arrow_box = draw.textbbox((0, 0), arrow, font=arrow_font)
    draw.text(
        (359 - (arrow_box[0] + arrow_box[2]) / 2, flag_y + 38 - (arrow_box[1] + arrow_box[3]) / 2),
        arrow,
        font=arrow_font,
        fill=(224, 242, 49, 255),
    )
    draw_us_flag(draw, 397, flag_y + 16, 73, 44)


def render_overlay(
    target: Path,
    item: dict,
    config: dict,
    answer: bool,
    show_subtitle: bool,
    show_cta: bool | None = None,
) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw_header(draw, config["title"], config["subtitle"], show_subtitle)

    card_y = CARD_Y
    card_x0, card_x1 = CARD_X0, CARD_X1
    card_h = CARD_HEIGHT
    draw.rounded_rectangle((card_x0 + 7, card_y + 10, card_x1 + 7, card_y + card_h + 10), radius=14, fill=(0, 0, 0, 100))
    draw.rounded_rectangle((card_x0, card_y, card_x1, card_y + card_h), radius=14, fill=(5, 7, 9, 230))

    vi_font, vi_lines = fitted_lines(draw, item["vi"], 510, 2, 55, 38, bold=True)
    vi_height = sum((draw.textbbox((0, 0), line, font=vi_font)[3] - draw.textbbox((0, 0), line, font=vi_font)[1]) for line in vi_lines)
    vi_height += max(0, len(vi_lines) - 1) * 6
    vi_top = 597 - max(0, vi_height - 60) / 2
    draw_centered_lines(draw, vi_lines, vi_font, WIDTH // 2, int(vi_top), (248, 248, 248, 255), spacing=6)

    track_color = (56, 57, 6, 225)
    draw.rounded_rectangle(
        (PROGRESS_X, PROGRESS_Y, PROGRESS_X + PROGRESS_WIDTH, PROGRESS_Y + PROGRESS_HEIGHT),
        radius=3,
        fill=(225, 242, 43, 255) if answer else track_color,
    )

    if answer:
        draw_english_answer(draw, item["en"])
    else:
        draw_english_clue(draw, item["en"])

    if show_cta is None:
        show_cta = answer
    if show_cta and config.get("cta"):
        has_hand = False
        cta_text = config["cta"].replace("👇", "").strip()
        cta_layers, cta_bounds = italic_centered_layout(draw, cta_text, WIDTH // 2, CTA_TOP)
        last_layer, last_x, last_y = cta_layers[-1]
        hand_x = min(WIDTH - 35, last_x + last_layer.width + 8)
        hand_y = last_y + max(0, round((last_layer.height - 38) / 2))
        content_left = min(cta_bounds[0], hand_x if has_hand else cta_bounds[0])
        content_right = max(cta_bounds[2], hand_x + 29 if has_hand else cta_bounds[2])
        content_bottom = max(cta_bounds[3], hand_y + 38 if has_hand else cta_bounds[3])
        panel = (
            max(16, content_left - 16),
            CTA_TOP - 11,
            min(WIDTH - 16, content_right + 16),
            content_bottom + 11,
        )
        draw.rounded_rectangle(panel, radius=10, fill=(0, 0, 0, 150), outline=(255, 255, 255, 48), width=1)
        for layer, x, y in cta_layers:
            image.alpha_composite(layer, (x, y))
        if has_hand:
            draw = ImageDraw.Draw(image)
            draw_down_hand(draw, hand_x, hand_y)

    watermark = config.get("watermark", "").strip()
    if watermark:
        mark_font, mark_lines = fitted_lines(draw, watermark, 560, 1, 32, 22, bold=False)
        mark = mark_lines[0]
        mark_box = draw.textbbox((0, 0), mark, font=mark_font)
        draw.text(
            (WIDTH / 2 - (mark_box[0] + mark_box[2]) / 2, 1174 - mark_box[1]),
            mark,
            font=mark_font,
            fill=(255, 255, 255, 190),
        )

    image.save(target, format="PNG", optimize=True)


def build_timeline(script: dict, audio_paths: dict[str, Path], script_index: int) -> list[dict]:
    timeline = []
    cursor = 0.0
    for item_index, item in enumerate(script["items"]):
        vi_path = audio_paths[f"{script_index}:{item_index}:vi"]
        en_path = audio_paths[f"{script_index}:{item_index}:en"]
        vi_duration = media_duration(vi_path)
        en_duration = media_duration(en_path)
        tick = max(MIN_TICK_OFFSET, VI_OFFSET + vi_duration + 0.18)
        wait_end = tick + TICK_DURATION
        reveal = wait_end + DING_GAP
        english = reveal + EN_GAP
        item_duration = max(MIN_ITEM_DURATION, english + en_duration + 0.45)
        timeline.append(
            {
                "item": item,
                "start": cursor,
                "vi": cursor + VI_OFFSET,
                "tick": cursor + tick,
                "wait_end": cursor + wait_end,
                "reveal": cursor + reveal,
                "english": cursor + english,
                "end": cursor + item_duration,
                "duration": item_duration,
                "vi_path": vi_path,
                "en_path": en_path,
            }
        )
        cursor += item_duration
    return timeline


def render_audio(timeline: list[dict], tick_path: Path, ding_path: Path, target: Path) -> None:
    total_duration = timeline[-1]["end"]
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{total_duration:.3f}",
        "-i",
        "anullsrc=r=48000:cl=stereo",
    ]
    for event in timeline:
        command.extend(["-i", str(event["vi_path"]), "-i", str(event["en_path"])])
    command.extend(["-i", str(tick_path), "-i", str(ding_path)])

    count = len(timeline)
    tick_input = 1 + count * 2
    ding_input = tick_input + 1
    filters = [f"[0:a]atrim=0:{total_duration:.3f},asetpts=PTS-STARTPTS[base]"]
    mix_labels = ["[base]"]

    for index, event in enumerate(timeline):
        vi_input = 1 + index * 2
        en_input = vi_input + 1
        vi_delay = round(event["vi"] * 1000)
        en_delay = round(event["english"] * 1000)
        filters.append(
            f"[{vi_input}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={vi_delay}:all=1,volume=1.18[vi{index}]"
        )
        filters.append(
            f"[{en_input}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={en_delay}:all=1,volume=1.18[en{index}]"
        )
        mix_labels.extend([f"[vi{index}]", f"[en{index}]"])

    if count == 1:
        tick_sources = [f"[{tick_input}:a]"]
        ding_sources = [f"[{ding_input}:a]"]
    else:
        tick_labels = "".join(f"[ticksrc{index}]" for index in range(count))
        ding_labels = "".join(f"[dingsrc{index}]" for index in range(count))
        filters.append(f"[{tick_input}:a]asplit={count}{tick_labels}")
        filters.append(f"[{ding_input}:a]asplit={count}{ding_labels}")
        tick_sources = [f"[ticksrc{index}]" for index in range(count)]
        ding_sources = [f"[dingsrc{index}]" for index in range(count)]

    for index, event in enumerate(timeline):
        tick_delay = round(event["tick"] * 1000)
        ding_delay = round(event["reveal"] * 1000)
        filters.append(
            f"{tick_sources[index]}aresample=48000,aformat=channel_layouts=stereo,adelay={tick_delay}:all=1,volume=0.34[tick{index}]"
        )
        filters.append(
            f"{ding_sources[index]}aresample=48000,aformat=channel_layouts=stereo,adelay={ding_delay}:all=1,volume=0.72[ding{index}]"
        )
        mix_labels.extend([f"[tick{index}]", f"[ding{index}]"])

    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[aout]",
            "-t",
            f"{total_duration:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(target),
        ]
    )
    run_command(command, "Ghép giọng đọc")


def render_video(
    timeline: list[dict],
    footage: Path,
    footage_offset: float,
    overlay_pairs: list[tuple[Path, Path]],
    target: Path,
    subtitle_overlay: Path | None = None,
) -> None:
    total_duration = timeline[-1]["end"]
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-ss",
        f"{footage_offset:.3f}",
        "-i",
        str(footage),
    ]
    for pre_path, answer_path in overlay_pairs:
        command.extend(["-framerate", "1", "-loop", "1", "-i", str(pre_path)])
        command.extend(["-framerate", "1", "-loop", "1", "-i", str(answer_path)])
    subtitle_input = None
    if subtitle_overlay is not None:
        subtitle_input = 1 + len(overlay_pairs) * 2
        # 30 fps is required here so the alpha fade receives fresh timestamps;
        # the other still overlays can remain at 1 fps.
        command.extend(["-framerate", str(FPS), "-loop", "1", "-i", str(subtitle_overlay)])

    filters = [
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,eq=brightness=-0.035:saturation=0.92[bg]"
    ]
    previous = "bg"
    for index, event in enumerate(timeline):
        pre_input = 1 + index * 2
        answer_input = pre_input + 1
        pre_label = f"v{index}a"
        bar_label = f"bar{index}"
        progress_label = f"v{index}p"
        answer_label = f"v{index}b"
        bar_alpha = (
            f"if(between(T,{event['tick']:.3f},{event['reveal']:.3f})*"
            f"lte(X,max(0,min({PROGRESS_WIDTH},"
            f"(T-{event['tick']:.3f})/{TICK_DURATION:.3f}*{PROGRESS_WIDTH}))),255,0)"
        )
        filters.append(
            f"color=c=0xE1F22B@1:s={PROGRESS_WIDTH}x{PROGRESS_HEIGHT}:r={FPS}:"
            f"d={total_duration:.3f},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{bar_alpha}'[{bar_label}]"
        )
        filters.append(
            f"[{previous}][{pre_input}:v]overlay=0:0:enable='between(t,{event['start']:.3f},{event['reveal'] - 0.001:.3f})'[{pre_label}]"
        )
        filters.append(
            f"[{pre_label}][{bar_label}]overlay=x={PROGRESS_X}:y={PROGRESS_Y}:"
            f"enable='between(t,{event['tick']:.3f},{event['reveal'] - 0.001:.3f})'[{progress_label}]"
        )
        filters.append(
            f"[{progress_label}][{answer_input}:v]overlay=0:0:enable='between(t,{event['reveal']:.3f},{event['end']:.3f})'[{answer_label}]"
        )
        previous = answer_label

    if subtitle_input is not None:
        first_event = timeline[0]
        fly_duration = 0.24
        fly_start = first_event["reveal"] - fly_duration
        subtitle_y = (
            f"if(lt(t,{fly_start:.3f}),0,"
            f"-120*pow((t-{fly_start:.3f})/{fly_duration:.3f},2))"
        )
        filters.append(
            f"[{subtitle_input}:v]format=rgba,fade=t=out:st={fly_start:.3f}:"
            f"d={fly_duration:.3f}:alpha=1[subtitlefly]"
        )
        filters.append(
            f"[{previous}][subtitlefly]overlay=x=0:y='{subtitle_y}':"
            f"enable='between(t,{first_event['start']:.3f},{first_event['reveal'] - 0.001:.3f})'[vsubtitle]"
        )
        previous = "vsubtitle"
    filters.append(f"[{previous}]format=yuv420p[vout]")

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-an",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    run_command(command, "Dựng hình")


def mux_video(video_path: Path, audio_path: Path, target: Path) -> None:
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-shortest",
        "-movflags",
        "+faststart",
        str(target),
    ]
    run_command(command, "Xuất MP4")


def render_job(job_dir: Path, payload: dict, status_path: Path) -> None:
    if not FONT_REGULAR.exists() or not FONT_BOLD.exists():
        raise RuntimeError("Thiếu font chữ của ứng dụng")

    progress = Progress(status_path, payload)
    progress.update("Đang chuẩn bị giọng Hoài My và Jenny…")
    audio_paths = asyncio.run(
        synthesize_all(payload, lambda phase, count: progress.update(phase, increment=count))
    )
    tick_path, ding_path = ensure_sound_effects()

    footage_paths = [job_dir / path for path in payload["footage"]]
    footage_durations = [media_duration(path) for path in footage_paths]
    output_dir = job_dir / "output"
    temp_root = job_dir / "working"
    temp_root.mkdir(parents=True, exist_ok=True)
    completed_outputs: list[Path] = []

    for script_index, script in enumerate(payload["scripts"]):
        display_index = script_index + 1
        output_name = f"tu-vung-hay-bai-{display_index:02d}.mp4"
        output_target = output_dir / output_name
        if output_target.exists() and output_target.stat().st_size > 100_000:
            try:
                media_duration(output_target)
                completed_outputs.append(output_target)
                progress.add_result(
                    output_name,
                    output_target.stat().st_size,
                    payload["job_id"],
                    display_index,
                )
                progress.update(
                    f"Đã khôi phục bài {display_index}/{payload['script_count']} từ checkpoint",
                    increment=3,
                    current_script=display_index,
                )
                continue
            except RuntimeError:
                output_target.unlink(missing_ok=True)

        current_dir = temp_root / f"script-{display_index:02d}"
        if current_dir.exists():
            shutil.rmtree(current_dir)
        current_dir.mkdir(parents=True)

        timeline = build_timeline(script, audio_paths, script_index)
        subtitle_path = current_dir / "overlay-subtitle.png"
        render_subtitle_overlay(subtitle_path, payload["config"].get("subtitle", ""))
        overlay_pairs = []
        for item_index, item in enumerate(script["items"]):
            pre_path = current_dir / f"overlay-{item_index:03d}-question.png"
            answer_path = current_dir / f"overlay-{item_index:03d}-answer.png"
            render_overlay(
                pre_path,
                item,
                payload["config"],
                answer=False,
                show_subtitle=False,
                show_cta=item_index > 0,
            )
            render_overlay(
                answer_path,
                item,
                payload["config"],
                answer=True,
                show_subtitle=False,
                show_cta=True,
            )
            overlay_pairs.append((pre_path, answer_path))

        progress.update(
            f"Bài {display_index}/{payload['script_count']}: đang ghép giọng và tiếng ting…",
            current_script=display_index,
        )
        audio_target = current_dir / "audio.m4a"
        render_audio(timeline, tick_path, ding_path, audio_target)
        progress.update(
            f"Bài {display_index}/{payload['script_count']}: đang dựng hình…",
            increment=1,
            current_script=display_index,
        )

        footage_index = script_index % len(footage_paths)
        footage = footage_paths[footage_index]
        duration = footage_durations[footage_index]
        rng = random.Random(f"{payload['job_id']}:{script_index}")
        offset = rng.uniform(0, max(0.0, duration - 1.0)) if duration > 1.2 else 0.0
        video_target = current_dir / "video-only.mp4"
        render_video(
            timeline,
            footage,
            offset,
            overlay_pairs,
            video_target,
            subtitle_overlay=subtitle_path,
        )

        progress.update(
            f"Bài {display_index}/{payload['script_count']}: đang đóng gói MP4…",
            increment=1,
            current_script=display_index,
        )
        mux_video(video_target, audio_target, output_target)
        completed_outputs.append(output_target)
        progress.add_result(output_name, output_target.stat().st_size, payload["job_id"], display_index)
        progress.update(
            f"Đã xong bài {display_index}/{payload['script_count']}",
            increment=1,
            current_script=display_index,
        )
        shutil.rmtree(current_dir, ignore_errors=True)

    progress.update("Đang tạo file tải tất cả…")
    zip_target = output_dir / "tu-vung-hay-tat-ca.zip"
    with zipfile.ZipFile(zip_target, "w", compression=zipfile.ZIP_STORED) as archive:
        for output in completed_outputs:
            archive.write(output, arcname=output.name)
    progress.done += 1
    shutil.rmtree(temp_root, ignore_errors=True)
    progress.complete()
