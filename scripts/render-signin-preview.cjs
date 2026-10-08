// Usage: NODE_PATH=/path/to/node_modules node scripts/render-signin-preview.cjs
// Requires Playwright with Chromium and Python with Pillow. CHROME_PATH is optional.
// Captures the production screen renderers and CSS with fictional API fixtures.
// All requests are intercepted: no server, account, AI run or live user data is used.
const {chromium} = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'orchestrator/web/static');
const origin = 'http://localhost:4179';
const pitch = 'A shared recipe book for my family.';
const tasks = ['Save a favourite recipe', 'Browse the family collection', 'Share a recipe with family'];
const questions = spawnSync('python3', ['-c',
  'import json; from orchestrator.new_project import QUESTIONS; print(json.dumps(QUESTIONS))'],
  {cwd: root, encoding: 'utf8'});
if (questions.status !== 0) throw Error(questions.stderr || 'Could not load project questions');
const fixtures = {
  'new-project': {
    questions: JSON.parse(questions.stdout), github: {user: 'demo'}, default_parent: '/Projects',
    draft: {step: 'describe', answers: {name: 'Family Recipes', pitch, audience: 'My family',
      problem: 'Our favourite recipes are scattered across messages.', features: tasks.join('\n'), platform: 'Web app'}},
  },
  product: {path: 'docs/product.md', history: [], sections: [
    {id: 'pitch', title: 'Pitch', hint: 'What are you building?', filled: true, body: pitch},
    {id: 'features', title: 'Core features', hint: 'What it must do on day one.', filled: true,
      body: tasks.map(t => '- ' + t).join('\n')},
  ]},
  integrations: {integrations: []}, 'visual-checks': {checks: []},
};
function jobFixture(stage, count = 0) {
  const planned = stage === 'plan', complete = stage === 'done';
  const status = planned ? 'planned' : complete ? 'completed' : 'executing';
  return {
    summary: {id: 'family-recipes', title: 'Build Family Recipes', type: 'feature', kind: 'Feature', status,
      state: {label: planned ? 'Ready for your approval' : complete ? 'Completed' : 'Building your app',
        tone: complete ? 'done' : 'working', group: planned ? 'needs_you' : complete ? 'done' : 'working',
        reason: complete ? 'Built, tested and reviewed.' : 'Working through your approved plan.',
        next: planned ? {action: 'approve', label: 'Approve plan'} : null}},
    job: {status, tasks, completed_tasks: tasks.slice(0, count).map((_, i) => String(i)),
      plan: {summary: pitch, tasks}},
    outputs: [], runs: [], docs: [], links: [],
    test_summary: {status: complete ? 'passed' : 'pending', passed_count: complete ? 12 : 0},
  };
}

(async () => {
  const frames = fs.mkdtempSync(path.join(os.tmpdir(), 'orchestrator-signin-'));
  let browser;
  try {
    browser = await chromium.launch({headless: true,
      ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
    const page = await browser.newPage({viewport: {width: 600, height: 1200}, deviceScaleFactor: 1,
      colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block'});
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      if (url.pathname.startsWith('/api/')) {
        const key = url.pathname.slice(5);
        if (key === 'client-log') return route.fulfill({json: {}});
        if (!(key in fixtures)) throw Error(`Missing preview fixture: ${key}`);
        return route.fulfill({json: fixtures[key]});
      }
      const file = path.join(output, url.pathname === '/' ? 'index.html' : url.pathname);
      if (!file.startsWith(output + path.sep) || !fs.existsSync(file)) return route.abort();
      return route.fulfill({path: file});
    });
    await page.addInitScript(() => {
      // Keep background routing/polling idle while explicitly rendering sample screens.
      sessionStorage.setItem('firebase:pendingRedirect:preview', 'true');
      localStorage.setItem('orchestrator_token', 'offline-preview');
    });
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.goto(origin, {waitUntil: 'load'});
    await page.evaluate(() => {
      state.project = {name: 'Family Recipes', mobile_app: false};
      document.body.classList.remove('signin-gate');
      document.querySelector('.app').classList.remove('session-locked');
    });
    const render = async (name) => page.evaluate(async name => {
      const result = await pages[name](name === 'job' ? ['family-recipes'] : [], new URLSearchParams());
      setHeader(result);
      view.innerHTML = result.html;
      window.scrollTo(0, 0);
    }, name);
    const capture = async (selector, height = 310) => {
      const box = await page.locator(selector).boundingBox();
      if (!box) throw Error(`Missing capture target: ${selector}`);
      return 'data:image/png;base64,' + (await page.screenshot({clip: {
        x: box.x, y: box.y, width: box.width, height,
      }, animations: 'disabled'})).toString('base64');
    };
    const shots = {idea: [], product: [], plan: [], build: [], done: []};
    await render('new-project');
    for (let i = 0; i <= 18; i++) {
      await page.locator('[name=pitch]').fill(pitch.slice(0, Math.ceil(pitch.length * i / 18)));
      shots.idea.push(await capture('fieldset.np-section-card'));
    }
    await render('product');
    shots.product.push(await capture('#sec-pitch'));
    fixtures['jobs/family-recipes'] = jobFixture('plan');
    await render('job');
    shots.plan.push(await capture('#plan-section'));
    for (let i = 0; i <= 2; i++) {
      fixtures['jobs/family-recipes'] = jobFixture('build', i);
      await render('job');
      shots.build.push(await capture('.job-top', 390));
    }
    fixtures['jobs/family-recipes'] = jobFixture('done', 3);
    await render('job');
    shots.done.push(await capture('.job-top', 390));
    if (errors.length) throw Error(errors.join('\n'));
    await page.close();

    const art = await browser.newPage({viewport: {width: 720, height: 540}, deviceScaleFactor: 1});
    await art.goto('file://' + path.join(__dirname, 'signin-preview.html'));
    await art.evaluate(shots => loadScreens(shots), shots);
    for (let i = 0; i < 180; i++) {
      await art.evaluate(t => drawFrame(t), i / 10);
      const data = await art.evaluate(() => document.querySelector('canvas').toDataURL().split(',')[1]);
      fs.writeFileSync(path.join(frames, `${String(i).padStart(3, '0')}.png`), Buffer.from(data, 'base64'));
    }
    // The static poster explains the starting point, including the finished idea.
    await art.evaluate(() => drawFrame(3));
    const poster = await art.evaluate(() => document.querySelector('canvas').toDataURL().split(',')[1]);
    fs.writeFileSync(path.join(output, 'signin-preview.png'), Buffer.from(poster, 'base64'));
    const result = spawnSync('python3', ['-c', `
from PIL import Image
from pathlib import Path
import sys
frames = [Image.open(p).convert('RGB') for p in sorted(Path(sys.argv[1]).glob('*.png'))]
# A shared palette sampled across every scene keeps text crisp and avoids colour flicker.
samples = [frames[i].resize((180, 135)) for i in (30, 50, 90, 130, 170)]
atlas = Image.new('RGB', (180 * len(samples), 135))
for i, sample in enumerate(samples): atlas.paste(sample, (180 * i, 0))
palette = atlas.quantize(colors=128)
frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
frames[0].save(sys.argv[2], save_all=True, append_images=frames[1:], duration=100,
               loop=0, optimize=True, disposal=1)
`, frames, path.join(output, 'signin-preview.gif')], {stdio: 'inherit'});
    if (result.status !== 0) throw Error('GIF encoding failed');
    console.log('Rendered signin-preview.gif and signin-preview.png from production screens');
  } finally {
    await browser?.close();
    fs.rmSync(frames, {recursive: true, force: true});
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
