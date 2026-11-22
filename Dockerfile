FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY printwarden ./printwarden

RUN useradd --uid 1000 --create-home app \
    && mkdir -p /config /work /data /output \
    && chown app:app /work /data /output
USER app

EXPOSE 8095
ENV PYTHONUNBUFFERED=1 PRINTWARDEN_CONFIG=/config/config.yaml
ENTRYPOINT ["python", "-m", "printwarden"]
CMD ["serve"]
