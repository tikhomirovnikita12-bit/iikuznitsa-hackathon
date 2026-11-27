FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY mechanic/speech.py mechanic/speech.py
RUN python -m mechanic.speech

COPY . .
RUN mkdir -p artifacts

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/catalog', timeout=4).close()"

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
