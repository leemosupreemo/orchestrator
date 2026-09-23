import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({ entryPoints: ['src/tool-output.tsx'], bundle: true, platform: 'node', format: 'esm', write: false });
const { ToolOutput } = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
const render = command => renderToStaticMarkup(createElement(ToolOutput, { command }));

test('running tool presents a live log with clear status', () => {
  const html = render({ operation: 'tool.self_tests', inputs: { suite: 'console' }, status: 'running', output: 'Checking console\nStep 2', result: {} });
  assert.match(html, /Running on Mac/);
  assert.match(html, /Live log · 2 lines/);
  assert.match(html, /role="log"/);
  assert.match(html, /Checking console/);
});

test('failed tool foregrounds the error and keeps technical output available', () => {
  const html = render({ operation: 'tool.distribution', inputs: { action: 'smoke_test' }, status: 'failed', result: { exit_code: 1, output: 'Signing failed' } });
  assert.match(html, /Failed/);
  assert.match(html, /exited with code 1/);
  assert.match(html, /Technical log/);
  assert.match(html, /Signing failed/);
});

test('fleet probe shows missing tools as readable details', () => {
  const html = render({ operation: 'tool.fleet', inputs: { action: 'probe' }, status: 'succeeded', result: { probes: [{ machine: 'local', reachable: true, binaries: { codex: true, firebase: false } }] } });
  assert.match(html, /1 machine checked/);
  assert.match(html, /Tools not found: firebase/);
});

test('failed model discovery shows its reason', () => {
  const html = render({ operation: 'tool.models', inputs: { refresh: true }, status: 'failed', result: { sync: { success: false, message: 'Provider timed out' } } });
  assert.match(html, /Provider timed out/);
  assert.match(html, /Failed/);
});
