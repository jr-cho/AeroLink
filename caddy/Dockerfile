# Caddy + the built web page baked in (CI image `aerolink-caddy`). Build context: repo root.
#   docker build -f caddy/Dockerfile -t aerolink-caddy .
# The basemap is NOT baked in (large, git-ignored): compose mounts ./basemap at /srv/basemap.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM caddy:2
COPY caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv/web
