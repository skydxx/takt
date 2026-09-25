# Public WSGI app — the real default surface for end users. Built from an
# explicit allow-list of files (never `COPY . .`): this is what actually
# guarantees admin_app.py, admin_routes.py, and templates/admin/ never end
# up in this image, not just "unused at runtime." See Dockerfile.admin for
# the separate operator/admin image and docker-compose.example.yml for how
# the two are wired together with a reverse proxy in front.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Public runtime package. Deliberately omits admin_app.py, admin_routes.py,
# and backup_manager.py — the public process never imports them.
COPY config.py core.py database.py platform_store.py platform_database.py \
     public_i18n.py legal_i18n.py utils.py web_app.py ./
COPY templates/ ./templates/
# Belt-and-suspenders: a future `COPY templates/ ./templates/` that starts
# picking up templates/admin/ (e.g. someone drops a shared template there
# by mistake) still gets stripped out of this image.
RUN rm -rf ./templates/admin
COPY static/ ./static/

RUN useradd --create-home --uid 10001 takt && chown -R takt:takt /app
USER takt

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["python", "web_app.py"]
