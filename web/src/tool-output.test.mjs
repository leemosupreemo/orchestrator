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

test('provider readiness separates sign-in from fallback keys', () => {
  const html = render({ operation: 'tool.keys', inputs: { action: 'status' }, status: 'succeeded', result: { cli_status: { codex: 'Ready', claude: 'Not installed' }, configured: { openai: false }, sign_in_commands: { claude: 'claude auth login' } } });
  assert.match(html, /CLI sign-in/);
  assert.match(html, /Fallback API keys/);
  assert.match(html, /On the Mac: claude auth login/);
  assert.match(html, /Not configured \(optional if CLI sign-in works\)/);
});

test('fleet output identifies selected machines and failed probes', () => {
  const html = render({ operation: 'tool.fleet', inputs: { action: 'probe' }, status: 'succeeded', result: { machines: [{ name: 'builder', execution_mode: 'ssh', enabled: true, ssh_target: 'builder.local' }], selected: ['builder'], probes: [{ machine: 'builder', reachable: false, repo_path_ok: false, binaries: { codex: false } }] } });
  assert.match(html, /Remote · Enabled · Selected/);
  assert.match(html, /Offline · Project path missing/);
  assert.match(html, /Tools not found: codex/);
});

test('instruction and document contents have readable sections', () => {
  const instructions = render({ operation: 'tool.instructions', inputs: { action: 'list' }, status: 'succeeded', result: { files: { 'AGENTS.md': 'missing', 'CLAUDE.md': 'valid' }, role_prompts: ['reviewer.md'] } });
  assert.match(instructions, /Instruction files/);
  assert.match(instructions, /AGENTS\.md/);
  assert.match(instructions, /reviewer\.md/);
  const document = render({ operation: 'tool.docs', inputs: { action: 'read' }, status: 'succeeded', result: { name: 'guide.md', content: '# Setup\nFirst step' } });
  assert.match(document, /class="result-content"/);
  assert.match(document, /First step/);
});

test('email and distribution output show readiness checks', () => {
  const email = render({ operation: 'tool.email', inputs: { action: 'list' }, status: 'succeeded', result: { notification_provider: 'gmail', sender_ready: false, notification_emails: ['a@example.com'] } });
  assert.match(email, /Sender credentials need setup on the Mac/);
  assert.match(email, /<li>a@example\.com<\/li>/);
  const distribution = render({ operation: 'tool.distribution', inputs: { action: 'status' }, status: 'succeeded', result: { configured: true, firebase_cli: true, firebase_logged_in: false, google_service_info: true, export_options: true, headless_signing: false } });
  assert.match(distribution, /Delivery readiness/);
  assert.match(distribution, /Firebase signed in/);
  assert.match(distribution, /Headless signing/);
});

test('simple menus lead with their useful result', () => {
  const examples = [
    ['tool.base_branch', { branch: 'develop' }, /Default branch: develop/],
    ['tool.project', { current: '/project' }, /Current project: \/project/],
    ['tool.archive', { jobs: ['job-1'] }, /1 archived job found/],
    ['tool.docs', { documents: ['guide.md'] }, /1 document available/],
    ['tool.update', { version: '0.1.0' }, /Mac package version 0\.1\.0/],
  ];
  for (const [operation, result, expected] of examples) {
    assert.match(render({ operation, inputs: { action: 'status' }, status: 'succeeded', result }), expected);
  }
});
