FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY timelapse_offload ./timelapse_offload

RUN useradd --uid 1000 --create-home app \
    && mkdir -p /config /work /data /output \
    && chown app:app /work /data /output
USER app

ENV PYTHONUNBUFFERED=1 TIMELAPSE_CONFIG=/config/config.yaml
ENTRYPOINT ["python", "-m", "timelapse_offload"]
CMD ["serve"]
