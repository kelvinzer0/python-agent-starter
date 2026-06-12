import { chromium } from 'playwright';

const BASE = 'https://aikuprojgs.edgeone.dev';
const TIMEOUT = 60_000;

let passed = 0;
let failed = 0;

async function test(name: string, fn: () => Promise<void>) {
  try {
    await fn();
    console.log(`✅ ${name}`);
    passed++;
  } catch (e: any) {
    console.log(`❌ ${name}`);
    console.log(`   ${e.message?.slice(0, 300)}`);
    failed++;
  }
}

function assert(cond: boolean, msg: string) {
  if (!cond) throw new Error(msg);
}

async function freshPage(browser: any) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  page.setDefaultTimeout(120_000);
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForSelector('input[type="email"]', { timeout: 8_000 });
  return { ctx, page };
}

async function register(page: any, email: string, username: string, password: string) {
  const toggle = page.getByText("Don't have an account? Register");
  await toggle.click();
  await page.waitForTimeout(200);
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[placeholder="Your name"]').fill(username);
  await page.locator('input[type="password"]').fill(password);
  await page.locator('button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 8_000 });
}

const browser = await chromium.launch({ headless: true });

// ═══════════════════════════════════════
// AUTH TESTS
// ═══════════════════════════════════════
await test('Auth: register', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw1@test.com', 'PW1', 'test123456');
  const token = await page.evaluate(() => localStorage.getItem('eo_auth_token'));
  assert(!!token, 'No token');
  await ctx.close();
});

await test('Auth: persists reload', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw2@test.com', 'PW2', 'test123456');
  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(500);
  assert(!(await page.$('input[type="email"]')), 'Modal shown after reload');
  await ctx.close();
});

await test('Auth: logout', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw3@test.com', 'PW3', 'test123456');
  await page.locator('button[title="Logout"]').click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });
  await ctx.close();
});

await test('Auth: wrong password', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw4@test.com', 'PW4', 'pass123456');
  await page.locator('button[title="Logout"]').click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });
  await page.locator('input[type="email"]').fill('pw4@test.com');
  await page.locator('input[type="password"]').fill('wrong');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(800);
  assert(await page.$('input[type="email"]'), 'Should show error');
  await ctx.close();
});

await test('Auth: duplicate email', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw5@test.com', 'PW5a', 'pass123456');
  await page.locator('button[title="Logout"]').click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });
  const toggle = page.getByText("Don't have an account? Register");
  await toggle.click();
  await page.waitForTimeout(200);
  await page.locator('input[type="email"]').fill('pw5@test.com');
  await page.locator('input[placeholder="Your name"]').fill('PW5b');
  await page.locator('input[type="password"]').fill('pass123456');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(1000);
  assert(await page.$('input[type="email"]'), 'Should reject duplicate');
  await ctx.close();
});

// ═══════════════════════════════════════
// WORKSPACE TESTS (IDB-first architecture)
// ═══════════════════════════════════════
await test('Workspace: templates from backend', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ws1@test.com', 'WS1', 'test123456');
  await page.waitForTimeout(2000);
  const sidebar = await page.textContent('aside');
  assert(sidebar!.includes('IDENTITY.md'), 'IDENTITY.md missing');
  await ctx.close();
});

await test('Workspace: IDB persists reload', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ws2@test.com', 'WS2', 'test123456');
  await page.waitForTimeout(1000);
  await page.evaluate(async () => {
    return new Promise<void>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) {
          db.createObjectStore('files', { keyPath: 'storageKey' });
        }
        const tx = db.transaction('files', 'readwrite');
        tx.objectStore('files').put({
          storageKey: 'idb-persist/myfile.txt', conversationId: 'idb-persist',
          filepath: 'myfile.txt', content: 'idb content', size: 11,
          hash: 'x', updatedAt: Date.now(), createdAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  });
  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(500);
  const ok = await page.evaluate(async () => {
    return new Promise<boolean>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(false); return; }
        const get = db.transaction('files', 'readonly').objectStore('files').get('idb-persist/myfile.txt');
        get.onsuccess = () => resolve(!!get.result);
        get.onerror = () => resolve(false);
      };
      req.onerror = () => resolve(false);
    });
  });
  assert(ok, 'IDB lost after reload');
  await ctx.close();
});

await test('Workspace: conversations isolated', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ws3@test.com', 'WS3', 'test123456');
  await page.waitForTimeout(1000);
  const cid1 = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));
  await page.evaluate(async (cid: string) => {
    return new Promise<void>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) {
          db.createObjectStore('files', { keyPath: 'storageKey' });
        }
        const tx = db.transaction('files', 'readwrite');
        tx.objectStore('files').put({
          storageKey: `${cid}/secret.txt`, conversationId: cid, filepath: 'secret.txt',
          content: 'private', size: 7, hash: 'x', updatedAt: Date.now(), createdAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  }, cid1!);
  await page.locator('button:has-text("New Chat")').first().click();
  await page.waitForTimeout(1000);
  const cid2 = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));
  assert(cid1 !== cid2, 'Same CID');
  const has = await page.evaluate(async (cid: string) => {
    return new Promise<boolean>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(false); return; }
        const get = db.transaction('files', 'readonly').objectStore('files').get(`${cid}/secret.txt`);
        get.onsuccess = () => resolve(!!get.result);
        get.onerror = () => resolve(false);
      };
      req.onerror = () => resolve(false);
    });
  }, cid2);
  assert(!has, 'New CID has old file');
  await ctx.close();
});

await test('Workspace: IDB files embedded in chat request', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ws4@test.com', 'WS4', 'test123456');
  await page.waitForTimeout(1000);

  // Manually put a file in IDB
  await page.evaluate(async () => {
    return new Promise<void>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) {
          db.createObjectStore('files', { keyPath: 'storageKey' });
        }
        const cid = localStorage.getItem('eo_conversation_id');
        const tx = db.transaction('files', 'readwrite');
        tx.objectStore('files').put({
          storageKey: `${cid}/embedded.txt`, conversationId: cid!, filepath: 'embedded.txt',
          content: 'from_idb', size: 8, hash: 'x', updatedAt: Date.now(), createdAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  });

  // Intercept the chat request to verify workspace_files is included
  let capturedBody: any = null;
  page.on('request', (req: any) => {
    if (req.url().includes('/chat') && req.method() === 'POST') {
      try { capturedBody = JSON.parse(req.postData()); } catch {}
    }
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('Say OK');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('OK'), { timeout: 90_000 });
  await page.waitForTimeout(1000);

  assert(capturedBody !== null, 'No chat request captured');
  assert(capturedBody.workspace_files !== undefined, 'workspace_files not in request body');
  assert(capturedBody.workspace_files['embedded.txt'] === 'from_idb', 'IDB file not embedded');
  await ctx.close();
});

// ═══════════════════════════════════════
// IDB PERSISTENCE (deterministic — mock SSE)
// ═══════════════════════════════════════

function fakeSse(events: Array<{event: string; data: any}>): string {
  return events.map(e => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}`).join('\n\n') + '\n\n';
}

async function getIdbCid(page: any): Promise<string> {
  return page.evaluate(() => localStorage.getItem('eo_conversation_id') || '');
}

async function getIdbFiles(page: any): Promise<string[]> {
  return page.evaluate(async () => {
    return new Promise<string[]>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve([]); return; }
        const cid = localStorage.getItem('eo_conversation_id');
        const index = db.transaction('files', 'readonly').objectStore('files').index('byConversation');
        const getAll = index.getAll(cid);
        getAll.onsuccess = () => resolve(getAll.result.map((r: any) => r.filepath));
        getAll.onerror = () => resolve([]);
      };
      req.onerror = () => resolve([]);
    });
  });
}

async function getIdbFileContent(page: any, filepath: string): Promise<string | null> {
  return page.evaluate(async (fp: string) => {
    return new Promise<string | null>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(null); return; }
        const cid = localStorage.getItem('eo_conversation_id');
        const get = db.transaction('files', 'readonly').objectStore('files').get(`${cid}/${fp}`);
        get.onsuccess = () => resolve(get.result?.content || null);
        get.onerror = () => resolve(null);
      };
      req.onerror = () => resolve(null);
    });
  }, filepath);
}

await test('IDB: file_changed with snapshot saves file to IDB', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'db1@test.com', 'DB1', 'test123456');
  await page.waitForTimeout(1000);

  // Mock /chat to return file_changed with files_snapshot
  await page.route('**/chat', async (route: any) => {
    const sse = fakeSse([
      { event: 'file_changed', data: { version: 1, files_snapshot: { 'new_file.txt': 'hello world' } } },
      { event: 'text_delta', data: { delta: 'OK' } },
      { event: 'done', data: { stopped: false } },
    ]);
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('trigger file_changed');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('[done'), { timeout: 30_000 });
  await page.waitForTimeout(1000);

  const cid = await getIdbCid(page);
  const files = await getIdbFiles(page);
  assert(files.includes('new_file.txt'), `File not in IDB: ${files}`);

  const content = await getIdbFileContent(page, 'new_file.txt');
  assert(content === 'hello world', `Content wrong: "${content}"`);
  await ctx.close();
});

await test('IDB: file_changed removes deleted files from IDB', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'db2@test.com', 'DB2', 'test123456');
  await page.waitForTimeout(1000);

  // Pre-populate IDB with 3 files
  const cid = await getIdbCid(page);
  await page.evaluate(async (cid: string) => {
    return new Promise<void>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction('files', 'readwrite');
        const store = tx.objectStore('files');
        for (const [name, content] of [['keep.txt', 'stay'], ['delete_me.txt', 'gone'], ['also_keep.txt', 'also']]) {
          store.put({
            storageKey: `${cid}/${name}`, conversationId: cid, filepath: name,
            content, size: content.length, hash: 'x', updatedAt: Date.now(), createdAt: Date.now(),
          });
        }
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  }, cid);

  // Mock /chat — snapshot only has keep.txt and also_keep.txt (delete_me.txt removed)
  await page.route('**/chat', async (route: any) => {
    const sse = fakeSse([
      { event: 'file_changed', data: { version: 2, files_snapshot: { 'keep.txt': 'stay', 'also_keep.txt': 'also' } } },
      { event: 'text_delta', data: { delta: 'OK' } },
      { event: 'done', data: { stopped: false } },
    ]);
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('trigger delete');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('[done'), { timeout: 30_000 });
  await page.waitForTimeout(1000);

  const files = await getIdbFiles(page);
  assert(files.includes('keep.txt'), `keep.txt missing: ${files}`);
  assert(files.includes('also_keep.txt'), `also_keep.txt missing: ${files}`);
  assert(!files.includes('delete_me.txt'), `delete_me.txt still in IDB: ${files}`);
  await ctx.close();
});

await test('IDB: file_changed updates existing file content', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'db3@test.com', 'DB3', 'test123456');
  await page.waitForTimeout(1000);

  // Pre-populate IDB with a file
  const cid = await getIdbCid(page);
  await page.evaluate(async (cid: string) => {
    return new Promise<void>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        const tx = db.transaction('files', 'readwrite');
        tx.objectStore('files').put({
          storageKey: `${cid}/update_me.txt`, conversationId: cid, filepath: 'update_me.txt',
          content: 'old content', size: 11, hash: 'x', updatedAt: Date.now(), createdAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  }, cid);

  // Mock /chat — snapshot has updated content
  await page.route('**/chat', async (route: any) => {
    const sse = fakeSse([
      { event: 'file_changed', data: { version: 3, files_snapshot: { 'update_me.txt': 'new content' } } },
      { event: 'text_delta', data: { delta: 'OK' } },
      { event: 'done', data: { stopped: false } },
    ]);
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('trigger update');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('[done'), { timeout: 30_000 });
  await page.waitForTimeout(1000);

  const content = await getIdbFileContent(page, 'update_me.txt');
  assert(content === 'new content', `Content not updated: "${content}"`);
  await ctx.close();
});

await test('IDB: multiple files in snapshot all saved to IDB', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'db4@test.com', 'DB4', 'test123456');
  await page.waitForTimeout(1000);

  const cid = await getIdbCid(page);

  await page.route('**/chat', async (route: any) => {
    const sse = fakeSse([
      { event: 'file_changed', data: { version: 1, files_snapshot: { 'a.txt': 'aaa', 'b.txt': 'bbb', 'c.txt': 'ccc' } } },
      { event: 'text_delta', data: { delta: 'OK' } },
      { event: 'done', data: { stopped: false } },
    ]);
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('trigger multi');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('[done'), { timeout: 30_000 });
  await page.waitForTimeout(1000);

  const files = await getIdbFiles(page);
  assert(files.includes('a.txt'), `a.txt missing: ${files}`);
  assert(files.includes('b.txt'), `b.txt missing: ${files}`);
  assert(files.includes('c.txt'), `c.txt missing: ${files}`);

  assert(await getIdbFileContent(page, 'a.txt') === 'aaa');
  assert(await getIdbFileContent(page, 'b.txt') === 'bbb');
  assert(await getIdbFileContent(page, 'c.txt') === 'ccc');
  await ctx.close();
});

await test('IDB: file persists reload after snapshot save', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'db5@test.com', 'DB5', 'test123456');
  await page.waitForTimeout(1000);

  const cid = await getIdbCid(page);

  await page.route('**/chat', async (route: any) => {
    const sse = fakeSse([
      { event: 'file_changed', data: { version: 1, files_snapshot: { 'persist.txt': 'survives' } } },
      { event: 'text_delta', data: { delta: 'OK' } },
      { event: 'done', data: { stopped: false } },
    ]);
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
  });

  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('trigger persist');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.body.innerText.includes('[done'), { timeout: 30_000 });
  await page.waitForTimeout(1000);

  // Reload page
  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(1000);

  const content = await page.evaluate(async () => {
    return new Promise<string | null>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(null); return; }
        const cid = localStorage.getItem('eo_conversation_id');
        const get = db.transaction('files', 'readonly').objectStore('files').get(`${cid}/persist.txt`);
        get.onsuccess = () => resolve(get.result?.content || null);
        get.onerror = () => resolve(null);
      };
      req.onerror = () => resolve(null);
    });
  });
  assert(content === 'survives', `File lost after reload: "${content}"`);
  await ctx.close();
});

// ═══════════════════════════════════════
// API TESTS
// ═══════════════════════════════════════
await test('API: workspace/templates', async () => {
  const r = await fetch(`${BASE}/workspace/files`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-001' },
    body: JSON.stringify({ action: 'list', conversationId: 'test' }),
  });
  const d = await r.json();
  assert(d.files?.length > 0, 'No files');
});

await test('API: chat streams', async () => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60_000);
  const r = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-002' },
    body: JSON.stringify({ message: 'Say "pong"' }),
    signal: ctrl.signal,
  });
  clearTimeout(timer);
  assert(r.ok, `Status ${r.status}`);
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let text = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    text += dec.decode(value, { stream: true });
    if (text.includes('text_delta')) break;
  }
  assert(text.includes('text_delta'), 'No text_delta');
});

await test('API: auth endpoints', async () => {
  const r1 = await fetch(`${BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-003' },
    body: JSON.stringify({ user_id: 'x', email: 'x@x.com', username: 'X', token: 't' }),
  });
  assert((await r1.json()).success, 'register failed');

  const r2 = await fetch(`${BASE}/auth/me`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-004', 'Authorization': 'Bearer t' },
    body: JSON.stringify({ user_id: 'x', email: 'x@x.com', username: 'X' }),
  });
  assert((await r2.json()).success, 'me failed');

  const r3 = await fetch(`${BASE}/auth/me`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-005' },
    body: JSON.stringify({}),
  });
  assert((await r3.json()).error === 'Not authenticated', 'should reject');
});

await browser.close();

console.log(`\n═══════════════════════════════════════`);
console.log(`  RESULTS: ${passed} passed, ${failed} failed`);
console.log(`═══════════════════════════════════════`);
process.exit(failed > 0 ? 1 : 0);
