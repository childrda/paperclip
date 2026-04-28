# Multi-stage build for the Paperclip frontend.
#
# Stage 1 (`builder`): Node + npm produce the production React bundle.
# Stage 2 (`runtime`): nginx-alpine serves the static files.
#
# Build context is the repo root (see docker-compose.yml's
# `build.context: .`) so we can copy both `frontend/` source and
# `deploy/nginx.conf` in one image.

# ---------- Stage 1: build the bundle ----------
FROM node:20-alpine AS builder

WORKDIR /app

# Install deps first so they're cached when only source changes.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install

# Copy the source and build.
COPY frontend/ ./
RUN npm run build


# ---------- Stage 2: serve ----------
FROM nginx:alpine AS runtime

# nginx config: proxy /api → backend, serve / from the bundle, no
# buffering on the SSE route.
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf

# Drop the bundled default page so nothing leaks if the static path
# can't satisfy a request.
RUN rm -f /usr/share/nginx/html/index.html

# The built bundle.
COPY --from=builder /app/dist/ /usr/share/nginx/html/

EXPOSE 80

# Healthcheck: nginx serves /index.html for any unmatched path; we ask
# for / and expect 200. Keeps Docker / Compose aware if the container
# wedges.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1
