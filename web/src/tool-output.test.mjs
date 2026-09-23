import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({ entryPoints: ['src/tool-output.tsx'], bundle: true, platform: 'node', format: 'esm', write: false });
const { ModelList, ToolOutput } = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
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

test('model catalog displays readable cards and selected defaults', () => {
  const models = [{ id: 'gpt-5.5', family: 'openai', tier: 'HIGH', capabilities: ['reasoning', 'coding'], required_clis: ['codex'] }];
  const html = render({ operation: 'tool.models', inputs: {}, status: 'succeeded', result: { models, selected: ['gpt-5.5'] } });
  assert.match(html, /gpt-5\.5/);
  assert.match(html, /OpenAI · High tier/);
  assert.match(html, /Reasoning/);
  assert.match(html, /Requires codex CLI/);
  assert.match(html, /Selected/);
  assert.doesNotMatch(html, /Available: gpt-5\.5/);
  const selection = renderToStaticMarkup(createElement(ModelList, { models, selected: ['gpt-5.5'], onToggle: () => {} }));
  assert.match(selection, /type="checkbox" checked=""/);
});

test('other tool lists display one item per line', () => {
  const html = render({ operation: 'tool.docs', inputs: {}, status: 'succeeded', result: { documents: ['guide.md', 'architecture.md'] } });
  assert.match(html, /<li>guide\.md<\/li><li>architecture\.md<\/li>/);
  assert.doesNotMatch(html, /guide\.md, architecture\.md/);
});
