import assert from 'node:assert/strict';
import {test, mock} from 'node:test';
import worker from './worker.mjs';

const env = {BACKEND_ORIGIN: 'https://backend.example', PUBLIC_ORIGIN: 'https://edge.example'};

test('proxies auth/body once, preserves cookie, rewrites backend redirect without following', async () => {
  const mocked = mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(url.href, 'https://backend.example/mcp-mcd/oauth/consent?ticket=fake');
    assert.equal(init.headers.get('Authorization'), 'Bearer dummy-owner-token');
    assert.equal(init.headers.get('host'), null);
    assert.equal(new TextDecoder().decode(init.body), 'decision=approve');
    assert.equal(init.redirect, 'manual');
    return new Response(null, {status: 303, headers: {
      Location: 'https://backend.example/mcp-mcd/spark',
      'Set-Cookie': 'mcd_oauth_csrf=fake; Path=/mcp-mcd/oauth; Secure; HttpOnly'
    }});
  });
  try {
    const response = await worker.fetch(new Request('https://edge.example/mcp-mcd/oauth/consent?ticket=fake', {
      method: 'POST', headers: {Authorization: 'Bearer dummy-owner-token', Host: 'edge.example'}, body: 'decision=approve'
    }), env);
    assert.equal(response.status, 303);
    assert.equal(response.headers.get('Location'), 'https://edge.example/mcp-mcd/spark');
    assert.match(response.headers.get('Set-Cookie'), /HttpOnly/);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(mocked.mock.callCount(), 1);
  } finally {mocked.mock.restore();}
});

test('rejects proxy loops, unknown paths, wrong host and oversized bodies before fetching', async () => {
  const mocked = mock.method(globalThis, 'fetch', async () => {throw new Error('Must not fetch');});
  try {
    assert.equal((await worker.fetch(new Request('https://edge.example/mcp-mcd/spark'), {...env, BACKEND_ORIGIN: env.PUBLIC_ORIGIN})).status, 503);
    assert.equal((await worker.fetch(new Request('https://edge.example/unrelated'), env)).status, 404);
    assert.equal((await worker.fetch(new Request('https://wrong.example/mcp-mcd/spark'), env)).status, 421);
    assert.equal((await worker.fetch(new Request('https://edge.example/mcp-mcd/spark', {method: 'POST', body: 'x'.repeat(262145)}), env)).status, 413);
    assert.equal(mocked.mock.callCount(), 0);
  } finally {mocked.mock.restore();}
});

test('passes Google redirects through; failures do not expose headers or URLs or trigger retries', async () => {
  const mocked = mock.method(globalThis, 'fetch', async () => new Response(null, {
    status: 303, headers: {Location: 'https://oauth-redirect.googleusercontent.com/r/callback?code=fake'}
  }));
  try {
    const response = await worker.fetch(new Request('https://edge.example/mcp-mcd/oauth/consent'), env);
    assert.match(response.headers.get('Location'), /^https:\/\/oauth-redirect.googleusercontent.com/);
    mocked.mock.mockImplementation(async () => {throw new Error('Bearer do-not-print');});
    const failed = await worker.fetch(new Request('https://edge.example/mcp-mcd/api/call', {method: 'POST', body: '{}'}), env);
    assert.equal(failed.status, 502);
    assert.doesNotMatch(await failed.text(), /do-not-print|backend\.example/);
    assert.equal(mocked.mock.callCount(), 2);
  } finally {mocked.mock.restore();}
});
