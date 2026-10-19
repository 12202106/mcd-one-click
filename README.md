# 一键麦当劳 MCP

把麦当劳中国官方 MCP 接到支持远程 HTTPS MCP 的客户端，只暴露一个工具 `mcd_one_click`。查询步骤在后端串联；下单、积分兑换等账号变更先返回预览和单次确认票据，再执行确认。

**给没有 VPS 的人准备了 Vercel、Render、Cloudflare Workers 入口和 GitHub Pages 前端方案。作者未在这些云平台上实际部署测试，配置与说明供自行尝试；不保证部署成功、客户端兼容或长期免费。本地模拟测试不等于云平台实测。**

这是个人非官方适配项目，不附带账号、官方 token 或公共可用的后台。每位部署者自行提供麦当劳官方 MCP 凭据，不要连接他人的账号。

## 选一种部署方式

| 方案 | 是否需要 VPS | 实际运行内容 | 状态 |
| --- | --- | --- | --- |
| Vercel + Upstash Redis | 否 | Python MCP、OAuth、REST API 和网页 | 未实际部署测试 |
| Render Free + Upstash Redis | 否 | Docker 后端和网页，会休眠 | 未实际部署测试 |
| Cloudflare Workers + 上述后端 | 否 | Workers 提供统一反代入口，Python 仍在 Vercel/Render | 未实际部署测试 |
| GitHub Pages + 上述后端 | 否 | 只托管静态操作页，跨域调用已部署的后端 | 未实际部署测试 |
| 自己的常驻服务器 | 是或本机 | Python 后端，可用本地 SQLite 和旧版 SSE | 本仓库发布版未重新做线上部署测试 |

**本项目没有提供独立运行全部业务的 Workers 实现，也不能把 GitHub Pages 的地址直接当作 MCP 服务器。** Workers 与 Pages 均可搭配免费托管的后端，因此不需要自己购买 VPS。

直接开始：[Vercel / Render](docs/DEPLOY.md) · [Cloudflare Workers](docs/CLOUDFLARE.md) · [GitHub Pages](docs/GITHUB-PAGES.md) · [Gemini 与其他客户端](docs/CLIENTS.md)。

## 准备两个不同的 token

- `MCD_UPSTREAM_TOKEN`：你从麦当劳官方 MCP 服务获得的 token，后端用它请求 `https://mcp.mcd.cn`。仅填原文，不加 `Bearer `。
- `MCD_GATEWAY_TOKEN`：你自己生成的 32 字符以上随机口令，给可信客户端或授权页使用。可在本机运行 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。

这两个值只放部署平台的环境变量/Secrets 或本地 `.env`，不要写进仓库、网页、截图或 URL。代码不读取发布者机器的配置，示例没有真实 token。OAuth 客户端拿到独立签发的访问凭据，不会拿到麦当劳官方 token。

## 主要功能

- `prepare_order`：根据自取/外送等方式，串联地址、门店、菜单、优惠和可选算价，返回预览。
- `run`：一次执行最多 12 个只读查询，后续参数可以引用之前返回的真实字段。
- `catalog`：获取内部操作的实时参数定义，便于构建任务。
- `prepare_action`：生成下单、兑换、领券等具体变更的预览与 5 分钟确认票据。
- `confirm_action`：用户确认后，携带票据和 `confirmed: true` 执行一次；价格等关键费用发生变化会重新要求确认。

“一个工具”不代表取消客户端自身的授权提示，也不代表允许自动下单。确认票据只保证该票据不会重复发送写请求，不能替代上游订单系统的幂等保障。写操作超时后先核实实际结果，不能盲目重新生成票据再下单。

普通 HTTP API 示例（环境变量由调用者在本机设置）：

```bash
curl "$PUBLIC_ORIGIN/mcp-mcd/api/call" \
  -H "Authorization: Bearer $MCD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"mcd_one_click","arguments":{"action":"run","payload_json":"{\"steps\":[{\"id\":\"time\",\"tool\":\"now-time-info\"}]}"}}'
```

## 本地开发

Python 3.13，依赖固定在 `requirements.lock`。本地模式可用 SQLite；无服务器部署必须配置持久化 Redis REST 存储，不会偷偷降级到 `/tmp` 数据库。

```bash
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：本机开发可设置 PUBLIC_ORIGIN=http://localhost:8013，MCD_SERVERLESS=0。
# 填入自己的两个 token；仅本地 SQLite 模式下，两个 UPSTASH 变量都留空。
uvicorn app:app --host 127.0.0.1 --port 8013 --no-access-log
```

本机 HTTP 只适合本地 API 调试。远程客户端、OAuth 授权页的 Secure cookie 以及公网接入需要有效 HTTPS。

## 测试与验证范围

```bash
python -m unittest discover -v
node --test cloudflare/worker.test.mjs
```

测试覆盖 OAuth/PKCE、授权码和刷新凭据单次使用、令牌撤销、唯一工具、只读链、价格变化重新确认、写入超时不重试、模拟 Redis 跨实例原子消费、无 ASGI lifespan 的 HTTP 请求、Pages CORS 和 Workers 转发。测试使用伪造凭据和模拟上游，不会真实下单；模拟 Redis 不是 Upstash 实测，Node 中的 Worker 测试不是 Cloudflare 运行时实测。

## 数据与边界

OAuth 注册、授权状态、凭据摘要、确认票据及预览需要保存在后端数据库。确认预览可能包含账号、地址或订单信息，应限制数据库访问；不要公开数据库导出。协议日志不记录 token、查询串或业务参数，但部署平台自身的访问日志另受平台设置控制。

轮换网关 token 会使已有 OAuth 凭据与确认票据失效，客户端需要重新授权；更换公开域名、数据库或状态 namespace 后也应重新关联。官方 token 失效时在后端环境变量更新并重新部署。

公开端点没有附带完整的限流与防滥用系统，适合个人自用。上游规则、可用工具、价格及优惠以麦当劳实际返回为准。保留了几句无功能影响的注释和一份 [README.py](README.py)。

MIT License；非麦当劳或 Google 官方项目。
