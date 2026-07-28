# ── XENIUS DocVault — Frontend ─────────────────────────────────────────────────
#
# Build context: ./frontend
# Multi-stage build:
#   1. builder  – npm install + vite build (produces dist/)
#   2. runtime  – Nginx serving the static bundle + reverse-proxy /api → backend
# ---------------------------------------------------------------------------

# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
# Use npm install instead of npm ci — tolerant of lockfile/node version drift
RUN npm install --legacy-peer-deps

COPY . .

# All /api calls are proxied by Nginx — no VITE_API_BASE_URL needed at build time.
RUN npm run build


# ── Stage 2: Nginx static server ──────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

LABEL maintainer="XENIUS DocVault"
LABEL description="PVVNL DDMS — React SPA served by Nginx"

# Remove default Nginx config and replace with our own.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy the built SPA.
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/api/v1/health || exit 1
