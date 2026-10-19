# Cloudflare Workers 入口

**未在 Cloudflare 上实际部署测试。此方案是 Workers 反代 + Vercel/Render 后端，不是独立的 Workers MCP 服务；组合使用仍然不需要 VPS。**

```text
Gemini / API 客户端 → HTTPS Worker → Vercel/Render Python 后端 → 官方麦当劳 MCP
                                           ↓
                                      Upstash Redis
```

Workers 负责域名入口和 HTTP 转发，OAuth、唯一工具编排和持久化由后端处理。仓库里的 Python 依赖和 SQLite 实现没有被宣称能直接运行在 Workers 中。Workers 不需要配置官方 token 或网关 token，只在请求中转发客户端的凭据。

1. 先按照 [部署文档](DEPLOY.md) 创建可用的 Vercel/Render 后端和 Redis。
2. 安装 Node.js 22+，进入 `cloudflare/`，运行 `npm ci`、`npx wrangler login`。
3. 编辑 `wrangler.jsonc`：`BACKEND_ORIGIN` 填固定后端域名，`PUBLIC_ORIGIN` 填实际 Worker 域名或自定义域名。两个值必须是不同的 HTTPS origin，不能带路径。Workers 子域可在 Cloudflare 控制台确认；若首次部署后才取得实际地址，修改配置并重新部署。
4. 后端环境变量设置 `PUBLIC_ORIGIN=https://你的worker域名`、`INTERNAL_ORIGIN=https://你的后端域名`，保持其他 token/Redis 变量不变，重新部署后端。OAuth 元数据、授权和回调地址必须使用同一个公开 Worker 域名。
5. 运行 `npm run check` 做打包检查，再 `npm run deploy` 发布。Gemini 中填 `https://你的worker域名/mcp-mcd/spark`；有旧域名关联时先断开，重新授权。

不能只反代 `/mcp-mcd/spark`。Worker 已处理 `/mcp-mcd`、其子路径和根目录两种 `/.well-known/*` 元数据，包括授权页、动态注册、token 与撤销接口。根目录重定向到网页；其他路径不会被转发。

转发保留 Authorization、OAuth cookie、状态码和外部回调 Location，不在 Worker 内部跟随重定向，不自动重试。响应明确禁用缓存；请求体限制 256 KiB。不要再添加缓存规则、浏览器挑战或会修改授权页 CSP 的规则。直接访问后端时，元数据会仍然指向公开 Worker 域名，这是预期配置。

测试命令：

```bash
cd cloudflare
npm test
npm run check
```

`npm test` 是 Node 中的模拟 fetch 测试，`--dry-run` 只是打包检查；二者均不是 Cloudflare 线上实测。实际部署后应重复 [健康/元数据/鉴权检查](DEPLOY.md#部署后检查)，再测试一次只读工具与 OAuth 关联。

Workers 有 [免费额度与 CPU 限制](https://developers.cloudflare.com/workers/platform/pricing/)，作为轻量 HTTP 入口使用；后端和数据库仍有各自额度。转发实现参照 [Workers 修改请求示例](https://developers.cloudflare.com/workers/examples/modify-request-property/)。
