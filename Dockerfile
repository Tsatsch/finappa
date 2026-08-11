FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY stats ./stats
RUN pip install --no-cache-dir .

COPY recurring.yml ./

RUN useradd --create-home --uid 1000 finappa \
    && mkdir -p /app/data \
    && chown -R finappa:finappa /app
USER finappa

CMD ["python", "-m", "app.main"]
