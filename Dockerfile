FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Якщо хочеш запускати як worker:
CMD ["python", "twitch_faceit_bot.py"]
