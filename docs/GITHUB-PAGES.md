# GitHub Pages 静态前端

**未实际部署测试。GitHub Pages 只能放静态网页，不能运行 Python、OAuth 或 MCP 后端。** 这是 [GitHub Pages 官方定义](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)。先部署 Vercel/Render 后端，可选再加 Workers 入口，全程不需要 VPS。

1. Fork 仓库，在 Settings → Pages → Build and deployment 选择 **GitHub Actions**。
2. Actions 中手动运行 **Deploy static frontend to GitHub Pages**。工作流只上传 `web/`，不会打包 `.env` 或后端源码；发布地址通常是 `https://你的用户名.github.io/mcd-one-click/`。参见 [GitHub Pages 工作流说明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
3. 后端设置 `MCD_CORS_ORIGINS=https://你的用户名.github.io`，然后重新部署。这里只填 origin，不能带 `/mcd-one-click/`；自定义前端域名则填该域名的 HTTPS origin。多个 origin 用逗号隔开，不支持 `*`。
4. 打开 Pages 页面，填写自己后端的 HTTPS 域名与 `MCD_GATEWAY_TOKEN`，点击连接。后端若使用 Workers，填写 Worker 域名；官方 token 始终只放后端环境变量。

可以修改 `web/site-config.js` 的 `backendOrigin` 作为默认公开域名，但其中绝对不能填 token。保持空值则由访问者自己输入。页面不使用 URL 参数、localStorage 或 cookie 保存 token，切换后端会清空当前 token；只有点击连接或调用时才发送给填写的后端。

资源使用相对路径，兼容 `用户名.github.io/仓库名/` 子目录部署，也可将 `web/` 部署到其他静态托管。输入的后端必须是 HTTPS origin，不含路径。若后端域名不允许 Pages 的 origin，浏览器会因 CORS 拒绝请求，必须在后端配置，不是在网页里禁用浏览器安全检查。

**Gemini 的服务器地址仍是后端的 `/mcp-mcd/spark`，不是 github.io 页面地址。** OAuth 授权页也由后端提供。仅发布 Pages 不会获得麦当劳服务，README 的 token 与数据库配置仍然需要完成。
