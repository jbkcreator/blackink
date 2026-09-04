FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary avoids needing libpq-dev at build time; kept minimal
# otherwise since Cloud Build image size affects cold-start latency.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT; the app must bind 0.0.0.0 on exactly that port,
# not a hardcoded one — see CLAUDE.md's Cloud Run section.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT}
