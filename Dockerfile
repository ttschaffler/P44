FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WAYPOINT_DB=/data/waypoint.db
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY waypoint ./waypoint
RUN useradd --uid 10001 --create-home waypoint && mkdir -p /data && chown waypoint /data
USER waypoint
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "waypoint.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
