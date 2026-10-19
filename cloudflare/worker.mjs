// A Workers edge entry for the Python backend. It never stores either token.
const BASE = '/mcp-mcd';
const META = new Set([
  '/.well-known/oauth-protected-resource',
  '/.well-known/oauth-protected-resource/mcp-mcd/spark',
  '/.well-known/oauth-authorization-server',
  '/.well-known/oauth-authorization-server/mcp-mcd/oauth'
]);

function httpsOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Configure an HTTPS origin without a path');
  }
  return url.origin;
}

function reply(message, status) {
  return new Response(JSON.stringify({error: message}), {
    status, headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}
  });
}

async function boundedBody(request) {
  if (!request.body) return undefined;
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 262144) {
      await reader.cancel();
      throw new RangeError('Request too large');
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {body.set(chunk, offset); offset += chunk.byteLength;}
  return body;
}

export default {
  async fetch(request, env) {
    let backend, publicOrigin;
    try {
      backend = httpsOrigin(env.BACKEND_ORIGIN);
      publicOrigin = httpsOrigin(env.PUBLIC_ORIGIN);
      if (backend === publicOrigin) throw new Error('Proxy loop');
    } catch (_) {return reply('Configure different BACKEND_ORIGIN and PUBLIC_ORIGIN HTTPS origins', 503);}

    const incoming = new URL(request.url);
    if (incoming.origin !== publicOrigin) return reply('Invalid public host', 421);
    if (incoming.pathname === '/') return Response.redirect(publicOrigin + BASE + '/', 307);
    if (!(incoming.pathname === BASE || incoming.pathname.startsWith(BASE + '/') || META.has(incoming.pathname))) {
      return reply('Not found', 404);
    }
    if (!['GET', 'HEAD', 'POST', 'DELETE', 'OPTIONS'].includes(request.method)) return reply('Method not allowed', 405);
    if (Number(request.headers.get('Content-Length') || 0) > 262144) return reply('Request too large', 413);
    let body;
    try {body = await boundedBody(request);} catch (_) {return reply('Request too large', 413);}

    const target = new URL(incoming.pathname + incoming.search, backend);
    // No arbitrary user-supplied upstream URL; Host is derived from the configured target.
    if (target.origin !== backend) return reply('Invalid path', 400);
    const headers = new Headers(request.headers);
    for (const name of ['host', 'content-length', 'forwarded', 'x-forwarded-host', 'x-forwarded-proto']) headers.delete(name);
    try {
      const upstream = await fetch(target, {
        method: request.method, headers, body,
        redirect: 'manual', signal: AbortSignal.timeout(290000)
      });
      const response = new Response(upstream.body, upstream);
      response.headers.set('Cache-Control', 'no-store');
      // Never follow an OAuth redirect inside the Worker with the owner's headers.
      const location = response.headers.get('Location');
      if (location) {
        const url = new URL(location, backend);
        if (url.origin === backend) response.headers.set('Location', publicOrigin + url.pathname + url.search + url.hash);
      }
      return response;
    } catch (_) {return reply('Backend unavailable. Do not automatically retry account changes.', 502);}
  }
};
