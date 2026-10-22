# 静态编译的 ffmpeg（只取 ffmpeg 本体；时长、尺寸也用它读取，不需要 ffprobe）
# 用于：视频贴纸转 GIF、超限视频压缩、视频转码。按 digest 锁定版本
FROM mwader/static-ffmpeg:9.0.2@sha256:7d9bdaaf887f7e6ce6151f67325c344074b5ff1fb75316011c3376503e449a7b AS ffmpeg

FROM python:3.11-slim

WORKDIR /app

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "interactive-bot"]
