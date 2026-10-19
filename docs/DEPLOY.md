# 无 VPS 部署：Vercel / Render

**以下方案作者均未实际部署测试。只完成了本地模拟验证；平台构建、网络到麦当劳的连通性、额度和 Gemini 真实关联需要部署者自行验证。**

## Vercel + Upstash

1. Fork 本仓库到自己的 GitHub 个人账号。创建 Upstash Redis 数据库，选择 Free 方案，在控制台保存 REST URL 和拥有读写权限的 REST Token。数据库保存 OAuth 和确认状态，不能使用临时内存或无服务器文件系统替代。
2. 在 Vercel 创建项目并导入仓库，Root Directory 选仓库根目录，使用 Python/ASGI 自动识别，不要选择静态站点构建。入口由 `pyproject.toml` 的 `app:app` 指定，Python 3.13；`vercel.json` 配置 300 秒函数上限。使用支持该上限的 Fluid compute 配置，保留 Python 框架默认路由，让 `/.well-known/*` 和 `/mcp-mcd/*` 都进入同一 ASGI 应用。
3. 在项目 Environment Variables 设置下表。`PUBLIC_ORIGIN` 填项目最终的固定生产域名，例如 `https://your-project.vercel.app`，不能填每次变化的预览 URL，不含 `/mcp-mcd`。可以先建项目确认生产域名，再更新变量重新部署。
4. 部署完成后检查健康页、元数据和鉴权，再按 [客户端说明](CLIENTS.md) 关联。公网入口不能被 Vercel 登录保护、浏览器挑战或平台 SSO 拦截，否则远程 MCP 客户端无法访问。应用自己的 token/OAuth 鉴权仍必须保留。

| 变量 | 设置 |
| --- | --- |
| `PUBLIC_ORIGIN` | 固定生产 HTTPS 域名，不含路径 |
| `MCD_GATEWAY_TOKEN` | 自己生成的 32 字符以上随机口令 |
| `MCD_UPSTREAM_TOKEN` | 自己的官方 token，不带 Bearer |
| `MCD_SERVERLESS` | `1`（Vercel 环境也会自动启用） |
| `UPSTASH_REDIS_REST_URL` | Redis 控制台 REST HTTPS URL，不带命令路径 |
| `UPSTASH_REDIS_REST_TOKEN` | Redis 读写 REST Token |
| `MCD_STATE_NAMESPACE` | 例如 `mcd-one-click`，同一实例保持不变，不同用户或环境应分开 |
| `MCD_CORS_ORIGINS` | 可选，Pages 的精确 origin，见 Pages 文档 |
| `INTERNAL_ORIGIN` | 仅 Workers 反代时需要，见 Workers 文档 |

可以使用 CLI 部署：安装 Vercel CLI 后运行 `vercel` 关联项目，使用 `vercel env add 变量名 production` 交互录入每个变量，再运行 `vercel --prod`。不要把 token 拼在命令行参数或写进 `vercel.json`。

`app.py` 暴露 ASGI 应用，`/spark` 的同步 JSON MCP 和 OAuth 不依赖启动回调或常驻会话；Redis 的 `GETDEL` 原子消费授权码、刷新凭据及确认票据，支持多个函数实例。Redis I/O 在线程中执行，不阻塞事件循环；失败时不会自动重复消费或写入。

一个复杂任务包含多次上游调用，仍可能超过函数时限。服务端整体时间预算为 280 秒；超时不代表下单失败，务必先查询实际订单结果。长时间的传统 SSE 仅保留给常驻服务器；在 `MCD_SERVERLESS=1` 时 `/sse` 返回 405，推荐始终使用 `/mcp-mcd/spark`。

Vercel 支持 ASGI/Python 3.13，当前 Fluid compute 的 Hobby 函数上限是 300 秒，Hobby 面向个人非商业用途；以 [Python 文档](https://vercel.com/docs/functions/runtimes/python)、[函数限制](https://vercel.com/docs/functions/limitations) 和 [Hobby 条款](https://vercel.com/docs/plans/hobby) 为准。Upstash 当前提供免费 Redis 档位，额度与升级计费见 [价格页](https://upstash.com/pricing/redis)；本项目使用其 [REST API](https://upstash.com/docs/redis/features/restapi) 和 [GETDEL](https://upstash.com/docs/redis/sdks/ts/commands/string/getdel)，不要使用会在几天后删除的临时演示数据库保存长期 OAuth。

## Render Free + Upstash

Fork 后在 Render 创建 Blueprint，选择仓库中的 `render.yaml`；模板用 Docker 和 Free Web Service。按提示填写生产域名、官方 token 和 Upstash 凭据，网关 token 可由模板自动生成，之后在平台环境变量中查看并保管。部署前确认界面显示 Free，若自动生成的域名不同，更新 `PUBLIC_ORIGIN` 并重新部署。

模板用 `MCD_SERVERLESS=1` 强制外部持久化存储。Render Free 文件系统不持久，不能把 OAuth SQLite 放在容器内。服务空闲 15 分钟会休眠，下一次访问可能冷启动约一分钟，因此客户端连接或授权可能需要等服务唤醒后重试。免费工作区有运行小时限制，详见 [Render Free 文档](https://render.com/docs/free)。

无需 VPS，但不是无需账号、凭据或配置。平台免费政策和网络可达性可能变化，选择免费档并自行留意额度，不承诺永久免费。

## 部署后检查

```bash
curl "$PUBLIC_ORIGIN/mcp-mcd/health"
curl "$PUBLIC_ORIGIN/.well-known/oauth-protected-resource/mcp-mcd/spark"
curl -I "$PUBLIC_ORIGIN/mcp-mcd/spark"
curl "$PUBLIC_ORIGIN/mcp-mcd/api/tools" -H "Authorization: Bearer $MCD_GATEWAY_TOKEN"
```

预期依次为健康 JSON、包含同一公开域名的元数据、未授权的 401（带资源元数据提示）、只有 `mcd_one_click` 的工具列表。然后执行 README 中的只读时间查询，确认真实上游连接。这些检查不证明下单可用，不应为测试部署而盲目下单。

## 常驻环境可选项

如果已有可运行 Docker/Python 的环境，可设置 `MCD_SERVERLESS=0` 并使用持久化 `.state` 目录；SQLite 文件和父目录由服务创建并限制权限。旧部署也可通过 `MCD_GATEWAY_CONFIG` 指定私有 JSON，字段为 `gateway_token` 和 `upstream_headers`；本仓库不包含此配置文件。
