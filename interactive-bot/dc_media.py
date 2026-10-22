"""媒体转换：动态贴纸转 GIF、视频压缩/转码、图片压缩。

- 依赖 ffmpeg（Docker 镜像内置静态编译版）、rlottie-python、Pillow；缺少时对应功能自动降级。
  只需要 ffmpeg 本体：时长、尺寸也从 `ffmpeg -i` 的输出里读取，不依赖 ffprobe。
- CPU 密集的 Python 操作放到线程里，ffmpeg 用子进程，均不阻塞事件循环。
- 所有函数失败时返回 None，由调用方降级处理。
"""

import asyncio
import gzip
import io
import logging
import os
import re
import shutil
import uuid

logger = logging.getLogger("interactive-bot.media")

FFMPEG = shutil.which("ffmpeg")

_DURATION_RE = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
# 例：Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1280x720 [SAR 1:1 DAR 16:9]
_VIDEO_SIZE_RE = re.compile(r"Stream #[^\n]*?: Video: [^\n]*?(?<![\dx])(\d{2,5})x(\d{2,5})(?!\d)")

try:
    from rlottie_python import LottieAnimation
except Exception:  # 没装或缺少动态库
    LottieAnimation = None

try:
    from PIL import Image
except Exception:
    Image = None

# 贴纸转出来的 GIF 边长和帧率
STICKER_SIZE = 256
STICKER_FPS = 30


def has_ffmpeg():
    return bool(FFMPEG)


def _out_path(workdir, suffix):
    """每次转换使用独立的输出文件，避免同一条消息里多个附件互相覆盖"""
    return os.path.join(workdir, f"out-{uuid.uuid4().hex}{suffix}")


async def _communicate(proc, timeout):
    """等待子进程结束；超时或任务被取消时杀掉子进程并回收，避免残留"""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())


async def _run(args, timeout):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await _communicate(proc, timeout)
    except asyncio.TimeoutError:
        logger.warning(f"{os.path.basename(args[0])} 超时（{timeout}s）")
        return None
    if proc.returncode != 0:
        logger.warning(f"{os.path.basename(args[0])} 失败：{err.decode(errors='replace')[-500:]}")
        return None
    return out


async def _probe(path):
    """`ffmpeg -i` 不指定输出时会把媒体信息打到 stderr 并以非 0 退出，这里只取信息"""
    if not FFMPEG:
        return ""
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-i", path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await _communicate(proc, 60)
    except asyncio.TimeoutError:
        return ""
    return err.decode(errors="replace")


def _parse_duration(info):
    m = _DURATION_RE.search(info)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def probe_duration(path):
    return _parse_duration(await _probe(path))


async def probe_video(path):
    """返回 (时长秒, 宽, 高)"""
    info = await _probe(path)
    size = _VIDEO_SIZE_RE.search(info)
    if not size:
        return None
    return int(_parse_duration(info) or 0), int(size.group(1)), int(size.group(2))


def can_compress(limit, duration):
    """按时长估算压缩到 limit 以内后的视频码率是否还能看"""
    if not duration:
        return True
    return limit * 8 * 0.92 / duration / 1000 - 32 >= 60


# ---------- 动态贴纸 → GIF ----------


def _frames_to_gif(frames, fps):
    if not frames:
        return None
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=max(20, round(1000 / fps)),
        loop=0,
        disposal=2,
        optimize=False,
    )
    return buf.getvalue()


def _lottie_to_gif(anim):
    total = anim.lottie_animation_get_totalframe()
    src_fps = anim.lottie_animation_get_framerate() or 30
    step = max(1, round(src_fps / STICKER_FPS))
    fps = src_fps / step
    frames = [
        anim.render_pillow_frame(frame_num=i, width=STICKER_SIZE, height=STICKER_SIZE)
        for i in range(0, total, step)
    ]
    return _frames_to_gif(frames, fps)


def _tgs_to_gif_sync(data):
    json_data = gzip.decompress(data).decode("utf-8")
    return _lottie_to_gif(LottieAnimation.from_data(json_data))


def _lottie_json_to_gif_sync(data):
    return _lottie_to_gif(LottieAnimation.from_data(data.decode("utf-8")))


async def tgs_to_gif(data):
    """Telegram 动态贴纸（.tgs，gzip 压缩的 Lottie）→ GIF"""
    if LottieAnimation is None or Image is None:
        return None
    try:
        return await asyncio.to_thread(_tgs_to_gif_sync, data)
    except Exception as e:
        logger.warning(f"TGS 转 GIF 失败：{e}")
        return None


async def lottie_to_gif(data):
    """Discord Lottie 贴纸（JSON）→ GIF"""
    if LottieAnimation is None or Image is None:
        return None
    try:
        return await asyncio.to_thread(_lottie_json_to_gif_sync, data)
    except Exception as e:
        logger.warning(f"Lottie 转 GIF 失败：{e}")
        return None


async def video_to_gif(src, workdir, *, alpha=True):
    """视频贴纸（webm，带透明通道）→ GIF"""
    if not FFMPEG:
        return None
    out = _out_path(workdir, ".gif")
    vf = (
        f"fps={STICKER_FPS},"
        f"scale={STICKER_SIZE}:{STICKER_SIZE}:force_original_aspect_ratio=decrease:flags=lanczos,"
        "split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse=alpha_threshold=128"
    )
    # VP9 自带解码器会丢掉透明通道，必须指定 libvpx-vp9 解码
    decoder = ["-c:v", "libvpx-vp9"] if alpha else []
    if await _run([FFMPEG, "-v", "error", "-y", *decoder, "-i", src, "-vf", vf, "-loop", "0", out], 120) is None:
        if not alpha:
            return None
        return await video_to_gif(src, workdir, alpha=False)
    with open(out, "rb") as f:
        return f.read()


def is_apng(data):
    head = data[:4096]
    return head.startswith(b"\x89PNG") and b"acTL" in head.split(b"IDAT")[0]


def _apng_to_gif_sync(data):
    im = Image.open(io.BytesIO(data))
    frames, durations = [], []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        frames.append(im.convert("RGBA"))
        durations.append(im.info.get("duration", 50) or 50)
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2
    )
    return buf.getvalue()


async def apng_to_gif(data):
    if Image is None:
        return None
    try:
        return await asyncio.to_thread(_apng_to_gif_sync, data)
    except Exception as e:
        logger.warning(f"APNG 转 GIF 失败：{e}")
        return None


# ---------- 视频压缩 / 转码 ----------


async def compress_video(src, limit, workdir):
    """把视频压缩到 limit 字节以内（按时长计算码率），返回输出路径；做不到返回 None"""
    if not has_ffmpeg():
        return None
    duration = await probe_duration(src)
    if not duration or duration <= 0:
        return None
    out = _out_path(workdir, ".mp4")
    budget = limit * 8 * 0.92 / duration / 1000  # 总码率 kbps
    audio = 64 if budget > 400 else 32
    video = budget - audio
    for _ in range(2):
        if video < 60:
            logger.info(f"视频太长（{duration:.0f}s），压缩到 {limit} 字节后画质不可用，放弃")
            return None
        height = 720 if video >= 1500 else 480 if video >= 600 else 360 if video >= 250 else 240
        args = [
            FFMPEG, "-v", "error", "-y", "-i", src,
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{int(video)}k", "-maxrate", f"{int(video)}k", "-bufsize", f"{int(video * 2)}k",
            "-vf", f"scale=-2:'min({height},ih)'",
            "-c:a", "aac", "-b:a", f"{audio}k", "-ac", "2",
            "-movflags", "+faststart", out,
        ]
        if await _run(args, max(300, duration * 3)) is None:
            return None
        if os.path.getsize(out) <= limit:
            return out
        video *= 0.75
    return None


async def to_mp4(src, workdir, limit):
    """webm / mkv / avi 等转成 Telegram 能直接播放的 mp4；超过 limit 时顺便压缩"""
    if not has_ffmpeg():
        return None
    out = _out_path(workdir, ".mp4")
    args = [
        FFMPEG, "-v", "error", "-y", "-i", src,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out,
    ]
    duration = await probe_duration(src) or 600
    if await _run(args, max(300, duration * 3)) is None:
        return None
    if os.path.getsize(out) <= limit:
        return out
    return await compress_video(out, limit, workdir)


# ---------- 图片 ----------


def _shrink_image_sync(data, limit):
    """先降质量，不够再逐步缩小尺寸，直到放得下或边长缩到 320 像素"""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    while True:
        for quality in (85, 70, 55):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= limit:
                return buf.getvalue()
        if max(im.size) <= 320:
            return None
        im = im.resize((max(1, int(im.width * 0.75)), max(1, int(im.height * 0.75))), Image.LANCZOS)


async def shrink_image(data, limit):
    if Image is None:
        return None
    try:
        return await asyncio.to_thread(_shrink_image_sync, data, limit)
    except Exception as e:
        logger.warning(f"压缩图片失败：{e}")
        return None
