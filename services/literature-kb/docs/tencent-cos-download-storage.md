# 腾讯云 COS 论文下载暂存配置

本文说明如何配置腾讯云 COS，使外部论文发现接口下载的 PDF 暂存到对象存储。

## 适用范围

配置影响 `POST /api/v1/discovery/downloads` 的低层暂存，以及
`POST /api/v1/literature/imports` 正式远程导入中的下载暂存。正式导入随后会从 COS 流式回读
PDF，登记到本地 `STORAGE_ROOT`，并继续执行解析、Evidence、Embedding 与索引。

对象默认使用以下键：

```text
literature-kb/downloads/{sha256}.pdf
literature-kb/downloads/{sha256}.json
```

JSON sidecar 保存论文来源、许可证、SHA-256 和大小，不保存下载 URL 的查询参数或临时签名。

## 配置步骤

进入后端目录：

```sh
cd code/Literature_KB/backend
```

首次配置时，从模板创建本机配置文件：

```sh
cp .env.example .env
```

在 `.env` 中填写以下内容：

```dotenv
DISCOVERY_STORAGE_PROVIDER=cos

COS_BUCKET=gibbs-papers-1334037784
COS_REGION=ap-shanghai
COS_ENDPOINT=gibbs-papers-1334037784.cos.ap-shanghai.myqcloud.com
COS_PREFIX=literature-kb

COS_SECRET_ID=从管理员处通过安全渠道获取
COS_SECRET_KEY=从管理员处通过安全渠道获取
```

`COS_ENDPOINT` 可以直接使用桶的请求域名。后端会为腾讯 SDK 自动规范化为地域端点。

同步依赖：

```sh
uv sync
```

然后按项目既有方式重启 API 和 worker。

## 密钥安全

- `.env` 不提交 Git，也不通过群聊、Issue、PR 或仓库传递。
- `COS_SECRET_ID` 和 `COS_SECRET_KEY` 应通过密码管理器、加密文件或其他私密渠道分发。
- 子账号只授予该桶所需的对象读取、写入权限，不使用主账号密钥。
- 若部署到服务器或 CI，使用部署平台的密钥管理功能注入同名环境变量。

## 验证

调用 `POST /api/v1/discovery/downloads` 成功后，响应中的 `storage_key` 形如：

```json
{
  "storage_key": "downloads/{sha256}.pdf",
  "metadata_key": "downloads/{sha256}.json",
  "duplicate": false
}
```

在 COS 控制台中对应为 `{COS_PREFIX}/downloads/...`。同一 PDF 再次下载时按 SHA-256 去重，响应会
返回 `duplicate: true`，但 sidecar 元数据会更新。

准备好 API、worker、PostgreSQL、Redis、模型和 COS 配置后，可以先执行无副作用预检：

```powershell
.\backend\scripts\smoke_remote_literature_import.ps1 -PreflightOnly
```

再执行真实端到端测试。该命令会下载一篇明确允许的开放论文、写入 COS 和本地库、运行完整
处理链，并重复导入一次验证幂等：

```powershell
.\backend\scripts\smoke_remote_literature_import.ps1 `
  -Query "retrieval augmented generation" `
  -TimeoutMinutes 30
```

若 API 配置了 Bearer Token，额外传入 `-ApiToken`。测试脚本不会读取、打印或保存 COS 密钥。
只有明确要验证本地暂存模式时才传 `-AllowLocalStaging`。

建议在 COS 控制台为 `{COS_PREFIX}/downloads/` 配置 7 天生命周期。COS 在这条链路中是可恢复的
短期暂存层，`STORAGE_ROOT` 才是本地论文的长期规范原件。

## 本地网络说明

若本机使用 Clash 等 Fake-IP 代理，公网论文站点可能解析到 `198.18.0.0/15`。仅在确认该网段由
受控本机代理接管时，才额外设置：

```dotenv
DISCOVERY_ALLOW_FAKE_IP_DNS=true
```

生产环境默认保持 `false`。
