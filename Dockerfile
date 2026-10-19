FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Перед запуском задайте TELEGRAM_BOT_TOKEN и OWNER_TELEGRAM_ID
# в переменных окружения платформы. Не копируйте .env в образ.
EXPOSE 10000
CMD ["python", "main.py"]
