ARG NODE_BASE_IMAGE=node:24-bookworm-slim
FROM ${NODE_BASE_IMAGE}

WORKDIR /app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787 \
    APP_BASE_URL=http://127.0.0.1:8787

# The backend has no third-party runtime dependencies. Copy only deployable code.
COPY package.json ./
COPY server.js ./
COPY supervisor.js /usr/local/lib/mantou-supervisor.js
COPY src ./src
COPY public ./public

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/storage/avatars /app/storage/downloads \
    && chown -R node:node /app

USER node

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node --input-type=module -e "const r=await fetch('http://127.0.0.1:8787/healthz'); if (!r.ok) process.exit(1)"

CMD ["node", "--no-warnings", "/usr/local/lib/mantou-supervisor.js"]
