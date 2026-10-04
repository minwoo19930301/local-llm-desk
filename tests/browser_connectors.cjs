// Only draft generation is mocked in the browser. Persistence and pasted OpenAPI
// parsing use the real HTTP API with http_fixture's temporary, isolated state.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.PYTHON || 'python3', ['-m', 'tests.http_fixture'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let logs = '', browser;
  server.stderr.on('data', chunk => { logs += chunk; });
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup: ' + logs)), 15000);
      createInterface({input: server.stdout}).on('line', line => {
        try { const data = JSON.parse(line); if (data.base) { clearTimeout(timer); resolve(data.base); } } catch {}
      });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${logs}`)); });
    });
    browser = await chromium.launch({headless: true, ...(process.env.CI ? {} : {channel: 'chrome'})});
    const page = await browser.newPage({viewport: {width: 1280, height: 960}});
    const errors = [], forbidden = [], draftRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    const attack = '<img src=x onerror="window.connectorXSS=true">';
    const draft = {
      kind: 'cli', name: 'Draft CLI', description: attack,
      command_template: 'echo {max-count} {enabled} {label}', readonly: true, timeout: 75,
      params: {
        'max-count': {type: 'integer', description: 'Limit ' + attack, default: 0, required: false},
        enabled: {type: 'boolean', description: 'Enabled', default: false, required: false},
        label: {type: 'string', description: 'Label', default: '', required: false},
      },
    };
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      const dangerous = /\/api\/(?:install|trial|alerts|runs)(?:\/|$)/.test(url.pathname)
        || /\/api\/(?:jobs|connectors)\/[^/]+\/(?:run|test|open)$/.test(url.pathname);
      if (url.origin !== base || dangerous) {
        forbidden.push(request.method() + ' ' + request.url());
        return route.abort();
      }
      if (url.pathname === '/api/connectors/draft') {
        draftRequests.push(request.postDataJSON());
        const result = draftRequests.length === 1
          ? {draft: null, questions: ['어떤 명령을 사용할까요? ' + attack], warnings: [], summary: attack, model: 'fixture-model:latest'}
          : {draft, questions: [], warnings: [attack], summary: attack, model: 'fixture-model:latest'};
        return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(result)});
      }
      return route.continue();
    });
    const registry = async () => (await (await page.request.get(base + '/api/connectors')).json()).connectors;
    const assertSafe = async () => {
      assert.equal(await page.evaluate(() => window.connectorXSS), undefined);
      assert.equal(await page.locator('.connector-builder img, #conList img').count(), 0);
    };
    const assertNoOverflow = async label => {
      const overflow = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
        elements: [...document.querySelectorAll('body *')].filter(el => {const r = el.getBoundingClientRect(); return r.width && r.right > innerWidth + 1;}).slice(-12).map(el => ({tag: el.tagName, id: el.id, class: el.className, right: el.getBoundingClientRect().right}))}));
      assert.ok(overflow.scrollWidth <= overflow.width, `Mobile overflow in ${label}: ${JSON.stringify(overflow)}`);
    };
    const saveConnector = async () => {
      const response = page.waitForResponse(r => r.url() === base + '/api/connectors' && r.request().method() === 'POST');
      await page.locator('#conSaveBtn').click();
      const result = await response;
      assert.equal(result.status(), 200, await result.text());
      return (await result.json()).connector;
    };
    await page.goto(base + '/connectors', {waitUntil: 'networkidle'});
    assert.equal(await page.locator('[data-con-mode="ai"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#conAIPanel').isVisible(), true);
    assert.equal(await page.locator('#conDraftBtn').isEnabled(), true);
    assert.deepEqual(await registry(), []);

    // Existing manual modes remain accessible without generating or saving anything.
    await page.locator('[data-con-mode="manual"]').click();
    for (const [kind, field] of [['CLI 명령', '#cTemplate'], ['HTTP API', '#cUrlTemplate'], ['스킬', '#cPath']]) {
      await page.locator('#conKindDrop .dd__btn').click();
      await page.locator('#conKindDrop').getByRole('option', {name: kind}).click();
      assert.equal(await page.locator(field).isVisible(), true);
    }
    assert.deepEqual(await registry(), []);
    await page.locator('[data-con-mode="ai"]').click();
    await page.locator('#conAIPrompt').fill('항목을 조회하는 CLI 연동을 만들고 싶어요.');
    await page.locator('#conDraftBtn').click();
    await page.locator('#conAIReply').waitFor({state: 'visible'});
    assert.equal(await page.locator('#conReview').isVisible(), false);
    assert.match(await page.locator('#conAIMessage').innerText(), /어떤 명령/);
    assert.deepEqual(await registry(), []);
    await assertSafe();
    await page.locator('#conAIReply').fill('echo를 쓰고 기본 개수는 0, enabled는 false로 해 주세요.');
    await page.locator('#conReplyBtn').click();
    await page.locator('#cTemplate').waitFor({state: 'visible'});
    assert.equal(draftRequests.length, 2);
    assert.match(draftRequests[1].prompt, /추가 정보.*수정 요청/);
    assert.match(draftRequests[1].prompt, /기본 개수는 0/);
    assert.equal(draftRequests[1].model, 'fixture-model:latest');
    assert.equal(await page.locator('#cReadonly').isChecked(), true);
    assert.equal(await page.locator('#cTimeout').inputValue(), '75');
    assert.equal(await page.locator('[data-p="type"]').nth(0).inputValue(), 'integer');
    assert.equal(await page.locator('[data-p="default"]').nth(0).inputValue(), '0');
    assert.equal(await page.locator('[data-p="type"]').nth(1).inputValue(), 'boolean');
    assert.equal(await page.locator('[data-p="default"]').nth(1).inputValue(), 'false');
    await page.locator('#cName').fill('브라우저 CLI 검증');
    await assertSafe();
    assert.deepEqual(await registry(), []);
    await page.setViewportSize({width: 390, height: 844});
    await assertNoOverflow('CLI review with typed parameters');
    await page.setViewportSize({width: 1280, height: 960});
    const cli = await saveConnector();
    assert.equal(cli.name, '브라우저 CLI 검증');
    assert.equal(cli.readonly, true);
    assert.equal(cli.timeout, 75);
    assert.equal(cli.params['max-count'].type, 'integer');
    assert.equal(cli.params['max-count'].default, 0);
    assert.equal(cli.params['max-count'].required, false);
    assert.equal(cli.params['max-count'].description, 'Limit ' + attack);
    assert.equal(cli.params.enabled.type, 'boolean');
    assert.equal(cli.params.enabled.default, false);
    assert.equal(cli.params.label.default, '');
    assert.equal(cli.description, attack);
    assert.equal(cli.command_template, draft.command_template);
    assert.equal((await registry()).length, 1);
    await assertSafe();

    // Actual parser, including authenticated operation and unsupported method.
    const spec = {
      openapi: '3.1.0', info: {title: 'Browser fixture', version: '1'},
      servers: [{url: 'https://api.example.invalid/v1'}],
      components: {securitySchemes: {token: {type: 'http', scheme: 'bearer'}}}, security: [{token: []}],
      paths: {'/items/{item-id}': {
        get: {operationId: 'readItem', summary: 'Read item', parameters: [{name: 'item-id', in: 'path', required: true, schema: {type: 'integer'}}], responses: {'200': {description: 'OK'}}},
        delete: {operationId: 'deleteItem', summary: 'Delete item', responses: {'200': {description: 'OK'}}},
      }},
    };
    await page.locator('[data-con-mode="openapi"]').click();
    await page.locator('#conSpecText').fill(JSON.stringify(spec));
    const parsedResponse = page.waitForResponse(r => r.url() === base + '/api/connectors/openapi');
    await page.locator('#conSpecReadBtn').click();
    const parsed = await (await parsedResponse).json();
    assert.equal(parsed.operations.length, 2);
    await page.locator('#conOperation').waitFor({state: 'visible'});
    await page.locator('#conOperation').selectOption('deleteItem');
    assert.equal(await page.locator('#conSpecDraftBtn').isDisabled(), true);
    assert.ok((await page.locator('#conSpecMessage').innerText()).length > 5);
    assert.deepEqual(parsed.operations.find(op => op.id === 'deleteItem').supported, false);
    await page.locator('#conOperation').selectOption('readItem');
    await page.locator('#conSpecDraftBtn').click();
    assert.equal(await page.locator('#cHeaders').inputValue(), 'Authorization=Bearer ${OPENAPI_TOKEN}');
    assert.match(await page.locator('#cUrlTemplate').inputValue(), /\{path_item_id\}/);
    assert.equal((await registry()).length, 1);
    await page.locator('#cName').fill('OpenAPI 브라우저 검증');
    await page.setViewportSize({width: 390, height: 844});
    await assertNoOverflow('OpenAPI review');
    await page.setViewportSize({width: 1280, height: 960});
    const http = await saveConnector();
    assert.equal(http.headers.Authorization, 'Bearer ${OPENAPI_TOKEN}');
    assert.equal(http.params.path_item_id.type, 'integer');
    assert.equal((await registry()).length, 2);

    await page.goto(base + '/jobs/new', {waitUntil: 'networkidle'});
    await page.locator('#connectorsDrop .dd__btn').click();
    const choice = page.locator('#connectorsDrop').getByRole('option', {name: cli.name});
    await choice.click();
    assert.equal(await choice.getAttribute('aria-selected'), 'true');
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
    await page.goto(base + '/connectors', {waitUntil: 'networkidle'});
    await page.setViewportSize({width: 390, height: 844});
    for (const mode of ['ai', 'openapi', 'manual']) {
      await page.locator(`[data-con-mode="${mode}"]`).click();
      await assertNoOverflow(mode);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
    console.log('Connector browser regression passed: manual access, questions/reply, explicit save, typed defaults, XSS, OpenAPI/auth, unsupported reason, mobile, job selection.');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGINT');
    await new Promise(resolve => { if (server.exitCode !== null) return resolve(); server.once('exit', resolve); setTimeout(() => { server.kill('SIGKILL'); resolve(); }, 3000).unref(); });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
