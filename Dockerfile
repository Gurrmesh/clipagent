# ClipAgent Studio — everything it needs, including ffmpeg.
#
# Written and reviewed but not built: the machine this was authored on has no
# Docker daemon. If the build trips on a system library, the fix is almost
# always one more apt package on the line below.
FROM python:3.11-slim

# ffmpeg does the rendering. libglib is the one system library the headless
# OpenCV build still links against on a slim image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY templates ./templates
COPY fonts ./fonts
COPY models ./models

# Clips, sources and the database live here. Mount a volume on it so they
# survive a container rebuild.
VOLUME ["/app/data"]
ENV DATA_DIR=/app/data RENDER_WORKERS=3
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
