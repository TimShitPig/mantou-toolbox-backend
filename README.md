# 馒头工具箱后端

为馒头工具箱小程序提供登录、用户资料、小说链接解析与下载接口。后端使用 Node.js 24 和内置 SQLite，无第三方运行时依赖，并兼容前端现有的 `.php` 接口路径。

## 服务器一键部署

服务器需要先安装 Docker。执行下面一条命令即可部署：

```sh
sudo mkdir -p /opt/mantou-toolbox && cd /opt/mantou-toolbox && curl -fsSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/docker-run.sh | sudo sh
```

脚本会自动拉取镜像并启动容器。首次运行会创建 `.env`、数据目录和内容目录，生成随机 `APP_SECRET`，并探测服务器公网 IPv4 来设置 `APP_BASE_URL`。配置文件权限为仅 root 可读写，无需手工填写服务器 IP 或随机密钥。脚本也会将数据目录权限调整为容器运行用户可写，避免 SQLite 无法打开数据库。

后台管理页地址为 `APP_BASE_URL/admin`。首次部署会自动生成管理员口令并保存到 `.env`，脚本会打印读取命令：

```sh
sudo grep '^ADMIN_PASSWORD=' /opt/mantou-toolbox/.env
```

更新部署会保留原管理员口令、服务器地址和端口。

微信 AppID 会从本项目配置自动写入。微信 AppSecret 由微信公众平台单独发放，不能自动生成；未配置时服务仍会部署并提供下载接口，微信资料登录保持关闭。需要登录时再将 AppSecret 写入 `/opt/mantou-toolbox/.env` 并重启容器。

后台面板支持服务总开关、解析开关、七猫/番茄来源开关、每日下载上限、激励广告和直链设置；同时显示用户数、下载任务和客户端错误日志。

管理会话有效期为 8 小时。正式使用请通过 HTTPS 反向代理访问后台。

后台顶部的“更新”会检查当前镜像与最近成功构建的版本，并提供最新版和最多 3 个较早版本的更新/回退命令。复制后在服务器的 `/opt/mantou-toolbox` 目录执行，原 `.env` 和 `data` 会保留。

检查服务：

```sh
sudo docker ps
curl http://127.0.0.1:8787/healthz
```

服务器防火墙还需要放行 TCP `8787`。如果公网 IP 探测失败，脚本会使用本机回环地址并提示；此时将 `/opt/mantou-toolbox/.env` 中的 `APP_BASE_URL` 改成服务器公网 IP 或域名。小程序正式环境应配置 HTTPS 域名和微信合法请求域名。

## 一键更新

在原部署目录执行以下命令。更新脚本会比较运行容器的代码版本与 GitHub 最新版本；版本未变化时直接退出，不拉镜像也不重启容器。发现新版本后才拉取镜像并重建容器，`.env` 和 `data` 会保留：

```sh
cd /opt/mantou-toolbox
curl -fsSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/docker-update.sh | sudo sh
```

需要强制重拉并重建时，设置 `FORCE_UPDATE=true`：

```sh
curl -fsSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/docker-update.sh | sudo env FORCE_UPDATE=true sh
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
使用 Compose 时，请在 `.env` 中设置随机 `APP_SECRET` 和 `ADMIN_PASSWORD`，否则管理面板会保持关闭。

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

本地运行管理面板时，在 `.env` 中设置 `ADMIN_PASSWORD`。

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
