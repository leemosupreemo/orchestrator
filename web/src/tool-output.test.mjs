import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({ entryPoints: ['src/tool-output.tsx'], bundle: true, platform: 'node', format: 'esm', write: false });
const { ModelList, ServiceSummary, ToolOutput } = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text).toString('base64'));
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

test('model catalog displays enabled and disabled cards', () => {
  const models = [{ id: 'gpt-5.5', family: 'openai', tier: 'HIGH', capabilities: ['reasoning', 'coding'], required_clis: ['codex'] }, { id: 'claude-opus', family: 'claude', tier: 'HIGH', capabilities: ['coding'], required_clis: ['claude'] }];
  const html = render({ operation: 'tool.models', inputs: {}, status: 'succeeded', result: { models, selected: ['gpt-5.5'] } });
  assert.match(html, /gpt-5\.5/);
  assert.match(html, /OpenAI · High tier/);
  assert.match(html, /<h4>OpenAI<\/h4><span>1 model<\/span>/);
  assert.match(html, /<h4>Anthropic \(Claude\)<\/h4><span>1 model<\/span>/);
  assert.ok(html.indexOf('<h4>OpenAI</h4>') < html.indexOf('<h4>Anthropic (Claude)</h4>'));
  assert.match(html, /Reasoning/);
  assert.match(html, /Requires codex CLI/);
  assert.match(html, /Enabled/);
  assert.match(html, /Disabled/);
  assert.doesNotMatch(html, /Available: gpt-5\.5/);
  const selection = renderToStaticMarkup(createElement(ModelList, { models, selected: ['gpt-5.5'], onToggle: () => {} }));
  assert.match(selection, /aria-label="Enable gpt-5\.5" disabled="" checked=""/);
  assert.match(selection, /aria-label="Enable claude-opus"/);
});

test('other tool lists display one item per line', () => {
  const html = render({ operation: 'tool.docs', inputs: {}, status: 'succeeded', result: { documents: ['guide.md', 'architecture.md'] } });
  assert.match(html, /<li>guide\.md<\/li><li>architecture\.md<\/li>/);
  assert.doesNotMatch(html, /guide\.md, architecture\.md/);
});

test('provider readiness groups sign-in and fallback key by service', () => {
  const html = render({ operation: 'tool.keys', inputs: { action: 'status' }, status: 'succeeded', result: { cli_status: { codex: 'Ready', claude: 'Not installed' }, configured: { openai: false }, sign_in_commands: { claude: 'claude auth login' } } });
  assert.match(html, /CLI sign-in/);
  assert.match(html, /On the Mac: claude auth login/);
  const claudeCard = html.split('service-card').find(card => card.includes('Anthropic (Claude)'));
  assert.match(claudeCard, /Fallback API key/);
  assert.match(claudeCard, /Not configured/);
  const openaiCard = html.split('service-card').find(card => card.includes('OpenAI'));
  assert.doesNotMatch(openaiCard, /Fallback API key/);
});

test('discontinued Gemini CLI is flagged with the Antigravity install command', () => {
  const html = render({ operation: 'tool.keys', inputs: { action: 'status' }, status: 'succeeded', result: { cli_status: { gemini: 'Discontinued' }, configured: { gemini: false }, sign_in_commands: { gemini: 'brew install --cask antigravity-cli' } } });
  const card = html.split('service-card').find(part => part.includes('Google (Gemini)'));
  assert.match(card, /Not ready/);
  assert.match(card, /Antigravity CLI/);
  assert.match(card, /Discontinued · On the Mac: brew install --cask antigravity-cli/);
});

test('leftover Gemini CLI is noted without blocking Antigravity', () => {
  const html = render({ operation: 'tool.keys', inputs: { action: 'status' }, status: 'succeeded', result: { cli_status: { gemini: 'Ready' }, configured: { gemini: false }, legacy_gemini_cli: true } });
  const card = html.split('service-card').find(part => part.includes('Google (Gemini)'));
  assert.match(card, /service-status ready/);
  assert.match(card, /old Gemini CLI is also installed/);
});

test('service cards show a YOLO switch only for services that support it', () => {
  const services = [{ family: 'claude', yolo_cli: 'claude', catalog: 2 }, { family: 'ollama', catalog: 1 }];
  const html = renderToStaticMarkup(createElement(ServiceSummary, { services, models: [], selected: [], yolo: { claude: true }, onYolo: () => {} }));
  const [claudeCard, ollamaCard] = html.split(/class="service-card(?: connected)?"/).slice(1);
  assert.match(claudeCard, /YOLO mode on/);
  assert.match(claudeCard, /role="switch"/);
  assert.match(claudeCard, /checked/);
  assert.doesNotMatch(ollamaCard, /YOLO/);
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
