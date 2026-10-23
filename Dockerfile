FROM python:3.11-slim

# PORT 默认 7860 以适配 Hugging Face Spaces; docker-compose 里会覆盖为 8000
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=7860

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860 5020
CMD ["python", "run.py"]
