FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE_PATH=/data/fastidentity.db FASTIDENTITY_PORT=5023
EXPOSE 5023
CMD ["python", "app.py"]
