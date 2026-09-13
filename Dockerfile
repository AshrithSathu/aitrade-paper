FROM cloudflare/cloudflared:latest AS tunnel
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-psycopg2 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@0.154.0
COPY --from=tunnel /usr/local/bin/cloudflared /usr/local/bin/cloudflared
WORKDIR /app
COPY backend/ ./backend/
COPY web/ ./web/
COPY feeds/ ./feeds/
COPY infra/ ./infra/
RUN mkdir -p /app/data /home/node/.codex && chown -R node:node /app /home/node/.codex
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status',timeout=3)" || exit 1
CMD ["python3", "-m", "infra.railway_start"]
