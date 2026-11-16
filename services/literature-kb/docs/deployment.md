# Rootless Docker 部署

本项目的生产 Compose 只向宿主机开放 `127.0.0.1:3000`。PostgreSQL 和 Redis 位于 Docker 内网，论文原文件及解析结果保存在仓库下的 `storage/`。

## 首次部署

在服务器上克隆仓库后，创建仅保存在服务器上的环境文件：

```bash
cd ~/apps/paper_res
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少替换 `POSTGRES_PASSWORD`、`API_AUTH_TOKEN`、`ADMIN_API_TOKEN` 和 `ADMIN_SECRETS_MASTER_KEY`。`POSTGRES_PASSWORD` 请使用字母数字或其他 URL-safe 字符；推荐命令：

```bash
openssl rand -hex 32
```

确认当前 Docker context 是 rootless，并启动全部服务：

```bash
docker context show
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
```

`docker context show` 应输出 `rootless`。启动时 API 会自动执行数据库迁移；各角色 worker
会等 API 健康后再启动，`worker-remote` 默认启动两个副本；Pipeline 另有 ingest、parse、
content、embedding、index 五类独立 worker。部署前保持 `PIPELINE_ENABLED=false`，
确认迁移、worker 和 dispatcher 健康后再打开。
初始副本数为 remote、parse、content、embedding 各 2 个，interactive、maintenance、
index 和 ingest 各 1 个。

升级到 Import Pipeline V3 前，先暂停新导入并确认旧版活动任务已经排空：

```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select task_id, operation, status from paper_tasks where pipeline_version = 2 and status in ('queued','running','retrying');"
```

查询结果为空后，在停机窗口执行迁移并打开新 Pipeline：

```bash
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
```

迁移 `0015_import_pipeline_v3` 会拒绝在存在活动 V2 Import task 时执行，避免跨版本任务被
静默接管。执行后设置 `PIPELINE_ENABLED=true` 并重建启动服务。

## 验证与日常更新

```bash
curl -I http://127.0.0.1:3000/health
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 api worker-interactive worker-ingest worker-maintenance worker-remote worker-parse worker-content worker-embedding worker-index web
```

本机健康检查正常后，使用 `https://friend.chatghx.com` 访问前端。更新采用停机窗口，
先记录当前发布 commit 和容器状态，再停止旧服务：

```bash
git rev-parse HEAD
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml down --remove-orphans
git pull --ff-only
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
```

`down` 命令禁止附加 `-v`，否则会删除 PostgreSQL 和 Redis 数据卷。启动后再次执行
`ps` 和日志命令，确认没有旧全队列 `worker` 容器，并且 interactive、ingest、maintenance、
index 各一个，remote、parse、content、embedding 各两个。健康检查或 smoke
失败时，先设置 `PIPELINE_ENABLED=false` 停止新调度，再执行不带 `-v` 的 `down`，
修复后使用相同的 `up -d --build --remove-orphans` 命令恢复。不要在已经执行 0015 后直接
切回只理解旧 Import stage 的版本；迁移不删除论文数据，但阶段契约已经硬切换。

不要提交 `.env`、`storage/`、`backend/storage/` 或 Docker 数据卷，也不要将端口改为
`0.0.0.0`、`80`、`443`。
