FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE_PATH=/data/fastidentity.db FASTIDENTITY_PORT=5023
EXPOSE 5023
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5023/health', timeout=4)" || exit 1
CMD ["sh", "scripts/start.sh"]
