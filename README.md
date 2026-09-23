# Mantou Toolbox Backend

This directory contains a zero-dependency Node 24 backend that preserves the
existing mini-program API paths, including their `.php` suffixes. It uses the
native `node:http` and `node:sqlite` modules, so no package installation is
needed.

## Docker one-click deployment

Docker Desktop must be running. Copy the Docker environment file and start the
service with one command:

```powershell
Copy-Item .env.docker.example .env
docker compose up -d --build
```

On Windows, `deploy.ps1` performs the environment-file copy and the same
Compose startup in one step:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy.ps1
```

The API is then available at `http://127.0.0.1:8787`. Check the container and
health endpoint with:

```powershell
docker compose ps
curl http://127.0.0.1:8787/healthz
```

Stop it with `docker compose down`. Persistent SQLite data, avatars, and
generated files live in the named `mantou-storage` volume. Set `PUBLIC_PORT`
and `APP_BASE_URL` in `.env` when exposing a different port or public domain.

The image is built from `node:24-bookworm-slim`; the backend has no npm runtime
dependencies. `compose.yaml` also mounts `./content` read-only at `/app/content`
for an optional `CONTENT_CATALOG_FILE` provider.

For a direct `docker run` deployment, use the same shape as a single-container
service:

```sh
sudo docker run -itd --restart unless-stopped \
  -p 8787:8787 \
  -v $PWD/data:/app/storage \
  -v $PWD/content:/app/content:ro \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  -e APP_BASE_URL=http://YOUR_SERVER_IP:8787 \
  -e APP_SECRET=REPLACE_WITH_A_LONG_SECRET \
  --name mantou-toolbox \
  ghcr.io/timshitpig/mantou-toolbox-backend:latest
```

The GHCR package currently requires a registry login before the first pull:

```sh
echo "$GHCR_TOKEN" | sudo docker login ghcr.io -u TimShitPig --password-stdin
```

After that login, the `docker run` command above is the only container start
command needed.

The equivalent executable wrappers are `docker-run.sh` and
`docker-update.sh`. For a cloned repository, run `sudo ./docker-update.sh`
after a new image is published. Put production variables such as
`WECHAT_APP_ID` and `WECHAT_APP_SECRET` in `$PWD/.env`; both wrappers reuse that
file on every recreate. For a host that only needs the updater, run:

```sh
curl -fsSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/docker-update.sh \
  | sudo env IMAGE=ghcr.io/timshitpig/mantou-toolbox-backend:latest \
      APP_BASE_URL=http://YOUR_SERVER_IP:8787 \
      APP_SECRET=REPLACE_WITH_A_LONG_SECRET sh
```

It pulls the latest image, recreates only the container, and keeps `$PWD/data`
intact.

## GitHub image and one-click updates

`.github/workflows/docker-publish.yml` publishes `main` to GHCR as:

```text
ghcr.io/timshitpig/mantou-toolbox-backend:latest
```

On a deployment host, clone this repository once, set `BACKEND_IMAGE` in `.env`
to that image, then run `deploy.sh` (Linux) or `deploy.ps1` (Windows). Future
updates are one command after a GitHub push:

```sh
./update.sh
```

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\update.ps1
```

The update script performs `git pull --ff-only`, pulls the new GHCR image, and
recreates the container without deleting the `mantou-storage` volume. Without
`BACKEND_IMAGE`, it pulls source changes and rebuilds locally instead.

## Run locally

```powershell
Copy-Item .env.example .env
npm start
```

The default URL is `http://127.0.0.1:8787`, which is also the frontend default
for the mini-program developer tool. A deployed service can be selected without
rebuilding by writing its HTTPS URL through the existing
`setBackendBaseUrl()` helper; stored URLs take precedence over the default.

```powershell
npm test
```

The service creates `storage/mantou.sqlite`, `storage/avatars`, and
`storage/downloads` on first start. These files are intentionally ignored.

## API surface

The service implements these existing client routes:

- `/api/v1/auth/login.php`, `/me.php`, `/profile.php`
- `/api/v1/upload/avatar.php`, `/api/v1/logs/client.php`
- `/api/download/status.php`, `/parse.php`, `/generate.php`, `/progress.php`
- `/api/download/file.php`, `/api/download/image_proxy.php`
- `/healthz`

Authentication uses signed short-lived bearer tokens. Profile and avatar
routes require a token. Download routes work anonymously as the current
frontend permits anonymous download requests.

## Content providers

`parse.php` recognizes Qimao and Fanqie links and returns a normalized book
record. Remote metadata can be enabled with `REMOTE_METADATA_ENABLED=true`.

`generate.php` creates a background job. For actual book text, configure one
of the following sources:

1. `CONTENT_CATALOG_FILE`: a local JSON array with records such as:

```json
[
  {
    "source": "qimao",
    "sourceBookId": "123",
    "title": "Example",
    "content": "Authorized text goes here."
  }
]
```

2. `CONTENT_PROVIDER_URL`: an HTTP endpoint that receives the requested book
and returns either `{ "text": "..." }` or
`{ "chapters": [{ "title": "Chapter 1", "content": "..." }] }`.

Without a configured content record, local development produces a clearly
marked source-metadata export. This keeps the full client task flow testable
while an external content provider is being connected.

## Production deployment

Set `NODE_ENV=production`, a long random `APP_SECRET`,
`ALLOW_DEVELOPMENT_LOGIN=false`, a public HTTPS `APP_BASE_URL`, and the WeChat
credentials before deploying. The mini program must also allow the deployed
HTTPS domain in its request-domain configuration.
