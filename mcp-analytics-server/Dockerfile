FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

# Headless Chromium for server-side map screenshots — render_geo_analysis's
# downloadable Excel export embeds a picture of the rendered map. --with-deps
# installs the system libraries Chromium needs (fonts, NSS, etc.) via apt.
RUN playwright install --with-deps chromium

COPY server.py .
COPY geo_analysis.py .
COPY geo_html.py .

EXPOSE 8959

CMD ["python", "server.py"]
