FROM python:3.13.5-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml /app/
COPY anonbot /app/anonbot
COPY knowledge/anon/corpus.json knowledge/anon/unknown_claims.json /app/knowledge/anon/
COPY deploy/requirements-server.txt /app/deploy/requirements-server.txt
RUN pip install --no-cache-dir -r deploy/requirements-server.txt . \
    && groupadd --gid 10001 anonbot \
    && useradd --uid 10001 --gid 10001 --no-create-home anonbot
USER 10001:10001
EXPOSE 8788
CMD ["python", "-m", "anonbot.release", "dashboard", "--config", "/config/release.json"]
