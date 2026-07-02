# hardening TODO: pin to a digest (e.g. python:3.12-slim@sha256:...)
FROM python:3.12-slim

# Run as a non-root user with a fixed UID. The /data volume is a bind-mounted
# host dir, so the host dir must be chown'd to this UID (see DEPLOY.md Step 3).
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py maps.py schema.sql ./
COPY templates/ templates/
COPY static/ static/
# scripts/ carries the staging seed helper (scripts/seed_staging.py), invoked via
# `docker compose ... exec staging-app python scripts/seed_staging.py --reset`.
COPY scripts/ scripts/

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Ensure /app and the /data mount point are owned by the non-root user so the
# container can write the SQLite DB and any app files.
RUN mkdir -p /data && chown -R appuser:appuser /app /data

EXPOSE 8080

USER appuser

ENTRYPOINT ["./entrypoint.sh"]
