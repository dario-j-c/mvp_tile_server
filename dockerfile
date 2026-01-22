from python:3.9-slim as base

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

copy . .

EXPOSE 1107

CMD python main.py config.json -p 1107 -b 0.0.0.0  --no-scan