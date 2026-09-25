# 馒头工具箱后端

为馒头工具箱小程序提供登录、用户资料、小说链接解析与下载接口。后端使用 Node.js 24、内置 SQLite 和 Python 番茄正文下载器，并兼容前端现有的 `.php` 接口路径；Node 部分无第三方运行时依赖。

## 服务器一键部署

以 `root` 用户 SSH 登录服务器，确认已安装 Docker Compose v2，然后执行：

```sh
# 创建部署目录
mkdir -p mantou-toolbox-deploy && cd mantou-toolbox-deploy

# 下载并运行部署准备脚本
curl -sSL https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/%E9%83%A8%E7%BD%B2/%E4%B8%80%E9%94%AE%E9%83%A8%E7%BD%B2.sh | bash

# 启动单容器服务
docker compose up -d --remove-orphans

# 查看日志
docker compose logs -f backend
```

在 root 终端中，部署目录为 `/root/mantou-toolbox-deploy`。准备脚本下载 Compose 配置和运行源码到 `source/`，并自动生成 `.env`、管理员密码和密钥；终端会显示管理员密码。Compose 只启动一个后端容器，源码目录以可写挂载方式保留在 root 下，SQLite 命名卷和现有密钥会保留。Node 基础镜像通过 DaoCloud 拉取，Debian 软件包通过阿里云镜像拉取。

后台地址和管理员密码由准备脚本输出。后台“更新”页可检查 `vX.X.X`，点击“立即更新”或“立即回退”后会由当前容器下载对应源码、校验版本并直接更新 root 下的 `source/`，随后重启后端进程并进行健康检查。检查失败时会自动恢复旧源码并再次启动旧版本。更新过程不会重建或拉取应用镜像，也不会增加容器；进度、完成和回退结果会显示在更新窗口，并记录在后台“运行日志”中。默认服务端口为 `8787`。

## 首次启用后台更新

旧部署若没有将 `source/` 挂载到 `/app`，需在服务器终端运行一次新版部署准备脚本，然后重建当前服务以启用容器内源码更新：

```sh
cd /root/mantou-toolbox-deploy
curl -sSL https://gh-proxy.com/https://raw.githubusercontent.com/TimShitPig/mantou-toolbox-backend/main/%E9%83%A8%E7%BD%B2/%E4%B8%80%E9%94%AE%E9%83%A8%E7%BD%B2.sh | bash
docker compose up -d --remove-orphans
```

准备脚本会保留 `.env` 密钥和 SQLite 数据卷；之后的常规更新与回退都从后台页面点击完成。

首次启用番茄正文和封面转换时，需要执行一次 `docker compose up -d --build`，把 Python 3、PyCryptodome 和 HEIC 转换工具加入运行镜像；之后的源码更新仍由后台直接更新，不需要重建镜像。

## 自动发布镜像

GitHub Actions 会在 `main` 更新或 `vX.X.X` 标签发布后构建并发布镜像：

```text
ghcr.io/timshitpig/mantou-toolbox-backend:latest
ghcr.io/timshitpig/mantou-toolbox-backend:v0.0.11
```

工作流配置见 [发布镜像.yml](.github/workflows/发布镜像.yml)。GHCR 镜像包仍公开发布；服务器首次部署时从源码构建基础运行容器，后台更新直接覆盖挂载源码，不会每次更新都构建新的本地镜像。

## 本地开发

需要 Node.js 24 或更高版本。使用番茄正文下载还需要 Python 3 和 PyCryptodome；Docker 镜像会自动安装它们：

```powershell
Copy-Item .env.example .env
npm start
```

业务代码按 `src/服务端`、`public/后台界面` 和 `部署` 分类，模块文件均使用中文名。`src/`、`public/`、`server.js`、`supervisor.js` 是已部署版本的更新器入口，必须保留这些兼容路径；Docker、npm 和 GitHub Actions 必需的清单也保留标准文件名。单元测试目录已移除。

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

解析接口可识别七猫和番茄链接。番茄详情、章节目录和正文分别通过番茄畅听 App 的 `/novelfm/bookapi/detail/v1/`、`/novelfm/bookapi/directory/all_items_v2/v1/`（失败时改用 App 的 v1 目录接口）和 `/novelfm/playerapi/full/mget/v1/` 获取，不请求小说网页；封面 URL 由 App 详情返回，并通过后端图片代理读取 `novelfmpic.com` 图片 CDN。七猫元数据仍由 `REMOTE_METADATA_ENABLED=true` 控制。

番茄下载任务会调用内置正文下载器，读取目录、每次最多批量请求 1500 章、下载并解密章节，最后生成合并 TXT；章节总数和完成数会回报到现有下载进度接口。Docker 镜像会安装 Python 3、PyCryptodome 和 HEIC 转换工具，封面图片会以 JPEG 返回小程序。七猫正文仍可通过本地内容目录或 `CONTENT_PROVIDER_URL` 接入。

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

内容提供接口接收书籍信息，并返回 `{ "text": "..." }` 或 `{ "chapters": [{ "title": "第一章", "content": "..." }] }`。七猫未配置正文来源时，后端会生成包含书籍资料和来源链接的说明文件，便于验证下载链路。

## 生产配置

生产环境请设置强随机 `APP_SECRET`、公网 HTTPS `APP_BASE_URL`、微信 `WECHAT_APP_ID` 和 `WECHAT_APP_SECRET`，并将 `ALLOW_DEVELOPMENT_LOGIN=false`。HTTPS 域名还需要配置到微信小程序的合法请求域名中。
