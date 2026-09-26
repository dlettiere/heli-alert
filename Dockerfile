FROM python:3.12-alpine

WORKDIR /app
RUN mkdir -p /app/data /app/web

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY heli_alert.py .
COPY web/ ./web/

EXPOSE 8085
VOLUME ["/app/data"]

CMD ["python", "-u", "heli_alert.py"]
