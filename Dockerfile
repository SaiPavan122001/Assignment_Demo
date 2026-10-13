FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" "sqlalchemy>=2" "psycopg[binary]" \
    pydantic pydantic-settings httpx pypdf python-docx python-multipart pyjwt
COPY app app
COPY golden golden
COPY fixtures fixtures
