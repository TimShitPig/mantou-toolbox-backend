ARG NODE_BASE_IMAGE=node:24-bookworm-slim
FROM ${NODE_BASE_IMAGE}

ARG MANTOU_RUNTIME_FINGERPRINT=unknown
LABEL com.timshitpig.mantou-toolbox.managed="true"
LABEL com.timshitpig.mantou-toolbox.runtime-fingerprint="${MANTOU_RUNTIME_FINGERPRINT}"

ARG APT_MIRROR=mirrors.aliyun.com

WORKDIR /app

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787 \
    APP_BASE_URL=http://127.0.0.1:8787 \
    PYTHON_BIN=/usr/bin/python3 \
    PYTHONPATH=/usr/lib/python3/dist-packages

# Copy runtime files for the server and novel App adapters.
COPY package.json ./
COPY server.js ./server.js
COPY supervisor.js /usr/local/lib/mantou-supervisor.js
COPY src ./src
COPY public ./public

USER root
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tar python3 python3-pycryptodome python3-aiohttp python3-cryptography python3-numpy python3-gmpy2 python3-bcrypt libheif-examples \
    && command -v heif-convert >/dev/null \
    && PYTHONPATH="/usr/lib/python3/dist-packages:/usr/local/lib/python3.11/dist-packages:/usr/local/lib/python3.11/site-packages:/app/src/小说下载" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -c "import glob,sys; print('Python module paths:',sys.path); print('Crypto package locations:',glob.glob('/usr/lib/python3/dist-packages/Crypto*')); import json,importlib; providers=json.load(open('/app/src/小说下载/小说平台.json')); [importlib.import_module('平台.'+p['module']) for p in providers if p.get('module')]" \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/storage/avatars /app/storage/downloads \
    && chown -R node:node /app

USER node

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node --input-type=module -e "const r=await fetch('http://127.0.0.1:8787/healthz'); if (!r.ok) process.exit(1)"

CMD ["node", "--no-warnings", "/usr/local/lib/mantou-supervisor.js"]
