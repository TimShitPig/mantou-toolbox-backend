FROM docker:29.8.1-cli AS docker-tools

FROM node:24-bookworm-slim

ARG APP_BUILD_REVISION=unknown
ARG APP_BUILD_VERSION=unknown

WORKDIR /app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787 \
    APP_BASE_URL=http://127.0.0.1:8787 \
    APP_BUILD_REVISION=${APP_BUILD_REVISION} \
    APP_BUILD_VERSION=${APP_BUILD_VERSION}

# The backend has no third-party runtime dependencies. Copy only deployable code.
COPY package.json ./
COPY server.js ./
COPY src ./src
COPY public ./public
COPY docker-update.sh ./docker-update.sh
COPY --from=docker-tools /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-tools /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/storage/avatars /app/storage/downloads \
    && chown -R node:node /app

USER node

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node --input-type=module -e "const r=await fetch('http://127.0.0.1:8787/healthz'); if (!r.ok) process.exit(1)"

CMD ["node", "--no-warnings", "server.js"]
