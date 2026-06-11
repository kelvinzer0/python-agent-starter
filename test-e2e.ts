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
// WORKSPACE TESTS
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

// ═══════════════════════════════════════
// AGENTIC PERSISTENCE (deep)
// ═══════════════════════════════════════
await test('Agentic: tool-call persists file to IDB', async () => {
  const { ctx, page } = await freshPage(browser);

  // Intercept SSE to see what events arrive
  const sseEvents: any[] = [];
  page.on('response', async (resp: any) => {
    if (resp.url().includes('/chat') && resp.status() === 200) {
      try {
        const body = await resp.text();
        const events = body.split('\n\n').filter((s: string) => s.trim());
        for (const ev of events) {
          const lines = ev.split('\n');
          let eventType = '';
          let data = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) eventType = line.slice(7);
            if (line.startsWith('data: ')) data += line.slice(6);
          }
          if (eventType) sseEvents.push({ eventType, dataLen: data.length, hasSnapshot: data.includes('files_snapshot') });
        }
      } catch {}
    }
  });

  await register(page, 'ag1@test.com', 'Ag1', 'test123456');
  await page.waitForTimeout(2000);
  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('Use local_write_file to create a file called tool_persist.txt with content "tool_call_works"');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => {
    return document.body.innerText.includes('tool_persist') || document.body.innerText.includes('Successfully');
  }, { timeout: 90_000 });
  await page.waitForTimeout(2000);

  // Debug: dump SSE events received
  console.log('   [DEBUG] SSE events:', JSON.stringify(sseEvents));

  // Debug: dump ALL files in IDB for this conversation
  const allFiles = await page.evaluate(async () => {
    return new Promise<any[]>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve([]); return; }
        const cid = localStorage.getItem('eo_conversation_id');
        const index = db.transaction('files', 'readonly').objectStore('files').index('byConversation');
        const getAll = index.getAll(cid);
        getAll.onsuccess = () => resolve(getAll.result.map((r: any) => ({ filepath: r.filepath, storageKey: r.storageKey, size: r.content?.length })));
        getAll.onerror = () => resolve([]);
      };
      req.onerror = () => resolve([]);
    });
  });
  console.log('   [DEBUG] IDB files for conversation:', JSON.stringify(allFiles));

  const ok = allFiles.some((r: any) => r.filepath === 'tool_persist.txt');
  assert(ok, 'File not in IDB after tool call');
  await ctx.close();
});

await test('Agentic: file survives reload after tool call', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ag2@test.com', 'Ag2', 'test123456');
  await page.waitForTimeout(2000);
  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('Use local_write_file to create a file called survive_reload.txt with content "i_survive"');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => {
    return document.body.innerText.includes('survive_reload') || document.body.innerText.includes('Successfully');
  }, { timeout: 90_000 });
  await page.waitForTimeout(1000);

  // Reload
  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(2000);

  // Check sidebar
  const sidebar = await page.textContent('aside');
  assert(sidebar!.includes('survive_reload.txt'), 'Not in sidebar after reload');

  // Check IDB content
  const content = await page.evaluate(async () => {
    return new Promise<string | null>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(null); return; }
        const cid = localStorage.getItem('eo_conversation_id');
        const get = db.transaction('files', 'readonly').objectStore('files').get(`${cid}/survive_reload.txt`);
        get.onsuccess = () => resolve(get.result?.content || null);
        get.onerror = () => resolve(null);
      };
      req.onerror = () => resolve(null);
    });
  });
  assert(content === 'i_survive', `Content wrong: "${content}"`);
  await ctx.close();
});

await test('Agentic: multiple tool calls persist all files', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ag3@test.com', 'Ag3', 'test123456');
  await page.waitForTimeout(2000);
  const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
  await ta.fill('Use local_write_file to create these files one by one: multi_a.txt content "aaa", multi_b.txt content "bbb", multi_c.txt content "ccc"');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => {
    const t = document.body.innerText;
    return t.includes('multi_a') && t.includes('multi_b') && t.includes('multi_c');
  }, { timeout: 120_000 });
  await page.waitForTimeout(1000);

  const files = await page.evaluate(async () => {
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
  assert(files.includes('multi_a.txt'), `multi_a missing: ${files}`);
  assert(files.includes('multi_b.txt'), `multi_b missing: ${files}`);
  assert(files.includes('multi_c.txt'), `multi_c missing: ${files}`);
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
  const r = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-t-002' },
    body: JSON.stringify({ message: 'Say "pong"' }),
  });
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
