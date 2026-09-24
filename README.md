# 馒头工具箱后端

为馒头工具箱小程序提供登录、用户资料、小说链接解析与下载接口。后端使用 Node.js 24 和内置 SQLite，无第三方运行时依赖，并兼容前端现有的 `.php` 接口路径。

## 服务器一键部署

以 `root` 用户 SSH 登录服务器，确认已安装 Docker Compose v2，然后执行：

```sh
# 创建部署目录
mkdir -p mantou-toolbox-deploy && cd mantou-toolbox-deploy

# 下载并运行部署准备脚本
curl -sSL https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/deploy/docker-deploy.sh | bash

# 启动服务
docker compose up -d

# 查看日志
docker compose logs -f backend
```

在 root 终端中，部署目录为 `/root/mantou-toolbox-deploy`。准备脚本只下载 Compose 配置，自动生成 `.env`、管理员密码和密钥；应用代码由 GHCR 镜像提供，不会克隆源代码。脚本会在终端显示管理员密码。SQLite 数据保存在 Docker 命名卷中，重建容器不会删除数据。

后台地址和管理员密码由准备脚本输出。新安装已包含更新助手；后台“更新”页可检查 `vX.X.X`、一键更新和回退。GitHub 加速地址用于版本检查，镜像由 Docker 从 GHCR 拉取。默认服务端口为 `8787`。

## 一键更新

新部署直接在后台点击“立即更新”。命令行更新可在部署目录执行：

```sh
cd /root/mantou-toolbox-deploy
docker compose pull
docker compose up -d
```

## 自动发布镜像

GitHub Actions 会在 `main` 更新或 `vX.X.X` 标签发布后构建并发布镜像：

```text
ghcr.io/timshitpig/mantou-toolbox-backend:latest
ghcr.io/timshitpig/mantou-toolbox-backend:v0.0.1
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
