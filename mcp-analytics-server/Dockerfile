FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

COPY server.py .
COPY geo_analysis.py .
COPY geo_html.py .

EXPOSE 8959

CMD ["python", "server.py"]
