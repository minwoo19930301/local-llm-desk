// Real form + HTTP persistence, temporary data and stub inference only.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.http_fixture'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let logs = '';
  server.stderr.on('data', chunk => {logs += chunk;});
  let browser;
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Test server did not start: ' + logs)), 15000);
      const lines = createInterface({input: server.stdout});
      lines.on('line', line => {
        try { const d = JSON.parse(line); if (d.base) {clearTimeout(timer); resolve(d.base);} } catch {}
      });
      server.once('exit', code => {clearTimeout(timer); reject(new Error(`Test server exited ${code}: ${logs}`));});
    });
    browser = await chromium.launch({headless: true, ...(process.env.CI ? {} : {channel: 'chrome'})});
    const page = await browser.newPage({viewport: {width: 1280, height: 960}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base + '/jobs/new', {waitUntil: 'networkidle'});
    await page.getByLabel('제목', {exact: true}).fill('브라우저 회귀 테스트');
    await page.getByLabel('시킬 일', {exact: true}).fill('hello');
    await page.getByLabel('추론 수준', {exact: true}).focus();
    await page.keyboard.press('Home');
    await page.locator('.execution-options > summary').click();
    await page.getByLabel('최대 실행 시간 (분)').fill('5');
    await page.locator('#ramPolicyDrop .dd__btn').click();
    await page.locator('#ramPolicyDrop').getByRole('option', {name: '이번 실행 건너뛰기', exact: true}).click();
    assert.equal(await page.locator('#deferWrap').isVisible(), false);
    assert.equal(await page.locator('#fallbackWrap').isVisible(), false);
    const savedResponse = page.waitForResponse(r => r.url() === base + '/api/jobs' && r.request().method() === 'POST');
    await page.locator('#saveBtn').click();
    const saved = await (await savedResponse).json();
    assert.equal(saved.job.effort, 'low');
    assert.equal(saved.job.max_minutes, 5);
    assert.equal(saved.job.ram_policy, 'skip');
    await page.waitForURL(base + '/jobs');
    await page.goto(base + '/jobs/edit?id=' + saved.job.id, {waitUntil: 'networkidle'});
    assert.equal(await page.getByLabel('최대 실행 시간 (분)').inputValue(), '5');
    await page.locator('#tryBtn').click();
    await page.waitForFunction(() => document.getElementById('tryOut').textContent.includes('격리된 테스트 응답'));
    assert.equal(await page.locator('#editError').isVisible(), false);
    await page.getByLabel('시킬 일').fill('EMPTY_FIXTURE');
    await page.locator('#tryBtn').click();
    await page.waitForFunction(() => document.getElementById('tryOut').textContent.startsWith('실패'));
    await page.getByLabel('시킬 일').fill('수정된 프롬프트');
    const editedResponse = page.waitForResponse(r => r.request().method() === 'PATCH');
    await page.locator('#saveBtn').click();
    const edited = await (await editedResponse).json();
    assert.equal(edited.job.prompt, '수정된 프롬프트');
    assert.equal(edited.job.effort, 'low');
    await page.waitForURL(base + '/jobs');
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(base + '/jobs/edit?id=' + saved.job.id, {waitUntil: 'networkidle'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('#saveBtn').isVisible(), true);
    assert.deepEqual(errors, []);
    fs.mkdirSync(path.join(root, 'test-results'), {recursive: true});
    await page.screenshot({path: path.join(root, 'test-results', 'job-form-mobile.png'), fullPage: true});
    await page.goto(base + '/settings', {waitUntil: 'networkidle'});
    await page.locator('#alertMode').selectOption('window');
    const alertsSaved = page.waitForResponse(r => r.url() === base + '/api/alerts' && r.request().method() === 'POST');
    await page.locator('#alertSaveBtn').click();
    assert.equal((await (await alertsSaved).json()).alerts.macos_mode, 'window');
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(await page.locator('#alertMode').inputValue(), 'window');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('Browser regression passed: create, edit, low effort, memory controls, trial success/empty, mobile form.');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGINT');
    await new Promise(resolve => {if (server.exitCode !== null) return resolve(); server.once('exit', resolve); setTimeout(() => {server.kill('SIGKILL'); resolve();}, 3000).unref();});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
