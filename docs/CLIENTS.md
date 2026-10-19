# 客户端接入

云平台方案未经过实际部署测试；能否完成关联还取决于客户端版本、账号资格、网络及官方 MCP 凭据。

## Gemini 自定义 MCP

在可添加自定义 MCP 的 Gemini 页面中填写：

```text
https://你的公开后端域名/mcp-mcd/spark
```

使用 Workers 入口时填 Worker 域名。Client ID 和 Client Secret 先留空，由动态客户端注册处理。下一步会打开你后端的授权页，核对回调地址，输入 `MCD_GATEWAY_TOKEN`，授权后返回客户端。不要输入官方 token。

刷新后只应出现 `mcd_one_click`。可以先请求“用一键麦当劳查询当前时间”。改变服务器域名、网关 token 或状态数据库后，需要重新关联；普通的源码更新保留域名、网关 token 和数据库时不应要求重新 OAuth。

此入口接收 MCP JSON-RPC：`initialize`、`tools/list`、`tools/call`，每次使用完整 `application/json` 响应，没有 SSE 长连接或持久会话。这里的 HTTP 仍然是 MCP 协议，不能把任意 REST API、OpenAPI 文件或网页当作 MCP URL。客户端行为与功能资格以其界面为准。

## 通用客户端与 REST

| 路径 | 用途 |
| --- | --- |
| `/mcp-mcd/spark` | 推荐的同步 JSON MCP + OAuth 资源，或网关 Bearer token |
| `/mcp-mcd/api/tools` | GET 读取唯一工具定义，需网关 token |
| `/mcp-mcd/api/call` | POST `{name, arguments}`，需网关 token |
| `/mcp-mcd/openapi.json` | 普通 HTTP agent 使用的 OpenAPI 描述 |
| `/mcp-mcd/` | 网页调试入口 |
| `/mcp-mcd/health` | 健康检查，不检查上游连通性 |
| `/mcp-mcd/mcp` | 常驻模式为 SDK Streamable HTTP；serverless 模式转为同步 JSON，仅接受网关 token |
| `/mcp-mcd/sse` | 仅常驻模式保留的旧版 SSE |

Bearer 请求头为 `Authorization: Bearer <MCD_GATEWAY_TOKEN>`。OAuth 访问凭据绑定 `/spark`，不能拿去调用其他 REST/旧版入口。REST 返回 HTTP 200 时仍应检查 MCP 的 `isError`，不要将业务失败当作成功。

`run` 的参数例子：

```json
{
  "name": "mcd_one_click",
  "arguments": {
    "action": "run",
    "payload_json": "{\"steps\":[{\"id\":\"time\",\"tool\":\"now-time-info\"}]}"
  }
}
```

复杂参数先用 `catalog` 查询真实的内部 schema。`run` 只接受白名单查询；写操作必须先 `prepare_action`，展示预览并取得明确确认，再传单次票据给 `confirm_action`。不要伪造 ID、价格或把用户的一次授权理解成无限自动购买。

## 排错

- 401：检查网关 token，或重新完成 OAuth；网页页面能打开不代表 API 已授权。
- 403：核对浏览器实际 Origin 和 `MCD_CORS_ORIGINS`，OAuth 表单应同源提交并保留 cookie。
- 421：检查 `PUBLIC_ORIGIN` / `INTERNAL_ORIGIN` 是否与实际请求域名一致，不要使用临时预览域名关联。
- 502：上游不可达或官方凭据失效，更新后端环境变量并重新部署。
- 503：存储故障或整体超时；检查 Redis 与平台函数日志。写操作先核实结果，不要自动重试。
- 授权后仍无法连接：检查所有 `/.well-known` 和 OAuth 路径均指向同一公开域名；Render 先等待服务唤醒，再尝试。不要在日志或 issue 里粘贴 token、授权码、完整回调 URL 或账号信息。
