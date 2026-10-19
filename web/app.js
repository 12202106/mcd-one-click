'use strict';
const $ = id => document.getElementById(id);
let tools = [];
$('backend').value = window.MCD_SITE_CONFIG?.backendOrigin ||
  (location.hostname.endsWith('github.io') ? '' : location.origin);
function backend() {
  const url = new URL($('backend').value.trim());
  if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)
      || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname)))) {
    throw new Error('后端必须是 HTTPS 域名，不要带路径或 token');
  }
  return url.origin;
}
function updateEndpoint() {
  try { $('mcp-url').textContent = backend() + '/mcp-mcd/spark'; }
  catch (_) { $('mcp-url').textContent = '请先填写部署好的后端域名'; }
}
updateEndpoint();
$('backend').addEventListener('input', () => {
  tools = []; $('tools').disabled = true; $('call').disabled = true;
  $('token').value = ''; $('status').textContent = '后端已更改，请重新连接'; updateEndpoint();
});
async function api(path, body) {
  const response = await fetch(backend() + '/mcp-mcd/api/' + path, {
    method: body ? 'POST' : 'GET', cache: 'no-store', credentials: 'omit',
    headers: {Authorization: 'Bearer ' + $('token').value.trim(), ...(body ? {'Content-Type': 'application/json'} : {})},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(110000)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败：' + response.status);
  return result;
}
function selection() {
  const tool = tools.find(t => t.name === $('tools').value);
  $('description').textContent = tool?.description || '';
  $('schema').textContent = JSON.stringify(tool?.inputSchema || {}, null, 2);
  $('arguments').value = JSON.stringify({action: 'run', payload_json: JSON.stringify({steps: [{id: 'time', tool: 'now-time-info'}]})}, null, 2);
}
$('tools').addEventListener('change', selection);
$('token').addEventListener('input', () => {
  tools = []; $('tools').disabled = true; $('call').disabled = true;
  $('status').textContent = 'token 已更改，请重新连接';
});
$('connect').addEventListener('click', async () => {
  $('connect').disabled = true; $('call').disabled = true; $('tools').disabled = true;
  $('status').textContent = '正在连接…';
  try {
    tools = (await api('tools')).tools;
    $('tools').replaceChildren(...tools.map(t => new Option(t.annotations?.title || t.name, t.name)));
    $('tools').disabled = !tools.length; $('call').disabled = !tools.length;
    $('status').textContent = '连接成功 · ' + tools.length + ' 个工具'; selection();
  } catch (error) { $('status').textContent = error.message; }
  finally { $('connect').disabled = false; }
});
$('call').addEventListener('click', async () => {
  let args;
  try { args = JSON.parse($('arguments').value); if (!args || Array.isArray(args) || typeof args !== 'object') throw new Error(); }
  catch (_) { $('result').textContent = '参数必须是 JSON 对象。'; return; }
  if (!confirm('确认调用 ' + $('tools').value + '？\n若涉及账号变更，请确认你已授权；下单或兑换前请确认商品和费用。')) return;
  $('call').disabled = true; $('result').textContent = '正在调用…';
  try { $('result').textContent = JSON.stringify(await api('call', {name: $('tools').value, arguments: args}), null, 2); }
  catch (error) { $('result').textContent = error.message + '\n账号变更请先核实结果，再决定是否重试。'; }
  finally { $('call').disabled = false; }
});
