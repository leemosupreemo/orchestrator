import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: ['src/local-api.ts'], bundle: true, platform: 'node', format: 'esm', write: false });
const code = built.outputFiles[0].text;
let loads = 0;
const load = () => import(`data:text/javascript;base64,${Buffer.from(code + `\n//${loads++}`).toString('base64')}`);

function browser(href, stored = {}) {
  const calls = [];
  const state = { href, stored, calls };
  globalThis.window = { location: { href }, history: { replaceState: (_, __, next) => { state.href = next; } } };
  globalThis.sessionStorage = { getItem: key => stored[key] ?? null, setItem: (key, value) => { stored[key] = value; } };
  globalThis.fetch = async (path, init) => { calls.push({ path, auth: init.headers.get('Authorization'), type: init.headers.get('Content-Type') }); return {}; };
  return state;
}

test('token from a network link is sent, stored and removed from the address bar', async () => {
  const state = browser('http://192.168.1.5:4040/?token=abc&x=1');
  const { apiFetch } = await load();
  await apiFetch('/api/worker', { headers: { 'Content-Type': 'application/json' } });
  assert.deepEqual(state.calls[0], { path: '/api/worker', auth: 'Bearer abc', type: 'application/json' });
  assert.equal(state.stored['orchestrator-local-token'], 'abc');
  assert.equal(state.href, 'http://192.168.1.5:4040/?x=1');
});

test('stored token survives a reload without the link', async () => {
  const state = browser('http://192.168.1.5:4040/', { 'orchestrator-local-token': 'abc' });
  const { apiFetch } = await load();
  await apiFetch('/api/worker');
  assert.equal(state.calls[0].auth, 'Bearer abc');
});

test('no token means no Authorization header', async () => {
  const state = browser('http://127.0.0.1:4040/');
  const { apiFetch } = await load();
  await apiFetch('/api/worker');
  assert.equal(state.calls[0].auth, null);
});
