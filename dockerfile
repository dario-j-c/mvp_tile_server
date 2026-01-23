FROM python:3.13-slim as base

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

EXPOSE 1107

HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://localhost:1107/health || exit 1

CMD ["python", "main.py", "config.json", "-p", "1107", "-b", "0.0.0.0", "--no-scan"]
