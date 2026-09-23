# 馒头工具箱后端

为馒头工具箱小程序提供登录、用户资料、小说链接解析与下载接口。后端使用 Node.js 24 和内置 SQLite，无第三方运行时依赖，并兼容前端现有的 `.php` 接口路径。

## 服务器一键部署

服务器需要安装 Docker。先准备数据目录并进入部署目录：

```sh
sudo mkdir -p /opt/mantou-toolbox/data /opt/mantou-toolbox/content
cd /opt/mantou-toolbox
```

创建服务器配置文件 `.env`：

```sh
sudo tee .env >/dev/null <<'EOF'
NODE_ENV=production
HOST=0.0.0.0
PORT=8787
APP_BASE_URL=http://YOUR_SERVER_IP:8787
APP_SECRET=请替换为一段足够长的随机密钥
ALLOW_DEVELOPMENT_LOGIN=true
WECHAT_APP_ID=
WECHAT_APP_SECRET=
EOF
```

将 `YOUR_SERVER_IP` 替换为服务器公网 IP 或域名。配置好微信小程序凭据后，把 `ALLOW_DEVELOPMENT_LOGIN` 改为 `false`。

启动容器：

```sh
sudo docker run -itd --restart unless-stopped \
  --env-file "$PWD/.env" \
  -p 8787:8787 \
  -v "$PWD/data:/app/storage" \
  -v "$PWD/content:/app/content:ro" \
  -v /etc/localtime:/etc/localtime:ro \
  -v /etc/timezone:/etc/timezone:ro \
  --name mantou-toolbox \
  ghcr.io/timshitpig/mantou-toolbox-backend:latest
```

检查服务：

```sh
sudo docker ps
curl http://127.0.0.1:8787/healthz
```

服务器防火墙还需要放行 TCP `8787`。小程序正式环境应配置 HTTPS 域名和微信合法请求域名。

## 一键更新

在原部署目录执行以下命令。更新脚本会拉取新镜像并重建容器，继续使用当前目录下的 `.env` 和 `data` 数据：

```sh
cd /opt/mantou-toolbox
curl -fsSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/docker-update.sh | sudo sh
```

也可以克隆仓库后使用：

```sh
sudo ./docker-update.sh
```

## Docker Compose 部署

克隆仓库后复制配置样例，再启动服务：

```sh
git clone https://github.com/TimShitPig/mantou-toolbox-backend.git
cd mantou-toolbox-backend
cp .env.docker.example .env
docker compose up -d --build
```

Windows 可运行 `deploy.ps1`；Linux/macOS 可运行 `./deploy.sh`。Compose 会将数据库、头像和生成文件保存在 `mantou-storage` 命名卷中。

## 自动发布镜像

GitHub Actions 会在 `main` 分支更新后构建并发布镜像：

```text
ghcr.io/timshitpig/mantou-toolbox-backend:latest
```

工作流配置见 [docker-publish.yml](.github/workflows/docker-publish.yml)。GHCR 镜像包为公开状态，无需执行 `docker login` 即可拉取。

## 本地开发

需要 Node.js 24 或更高版本：

```powershell
Copy-Item .env.example .env
npm start
npm test
```

默认地址为 `http://127.0.0.1:8787`。首次启动会创建 `storage/mantou.sqlite`、`storage/avatars` 和 `storage/downloads`。

## 接口

- 认证与资料：`/api/v1/auth/login.php`、`/api/v1/auth/me.php`、`/api/v1/auth/profile.php`
- 头像与客户端日志：`/api/v1/upload/avatar.php`、`/api/v1/logs/client.php`
- 小说下载：`/api/download/status.php`、`parse.php`、`generate.php`、`progress.php`
- 文件与图片：`/api/download/file.php`、`/api/download/image_proxy.php`
- 健康检查：`/healthz`

认证接口使用短期签名 Bearer Token。用户资料和头像接口要求登录；下载接口支持匿名请求，与现有前端行为一致。

## 内容来源配置

解析接口可识别七猫和番茄链接。设置 `REMOTE_METADATA_ENABLED=true` 后会尝试请求平台元数据；平台响应可能随时变化。

下载生成接口会创建后台任务。需要生成正文时，可以配置本地授权内容目录 `CONTENT_CATALOG_FILE`，或配置返回正文的 `CONTENT_PROVIDER_URL`。

本地 JSON 内容目录示例：

```json
[
  {
    "source": "qimao",
    "sourceBookId": "123",
    "title": "示例书名",
    "content": "已授权的正文内容。"
  }
]
```

内容提供接口接收书籍信息，并返回 `{ "text": "..." }` 或 `{ "chapters": [{ "title": "第一章", "content": "..." }] }`。未配置内容来源时，后端会生成包含书籍资料和来源链接的说明文件，便于验证下载链路。

## 生产配置

生产环境请设置强随机 `APP_SECRET`、公网 HTTPS `APP_BASE_URL`、微信 `WECHAT_APP_ID` 和 `WECHAT_APP_SECRET`，并将 `ALLOW_DEVELOPMENT_LOGIN=false`。HTTPS 域名还需要配置到微信小程序的合法请求域名中。
