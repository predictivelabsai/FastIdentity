FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE_PATH=/data/fastidentity.db FASTIDENTITY_PORT=5023
EXPOSE 5023
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:5023/health || exit 1
CMD ["sh", "scripts/start.sh"]
