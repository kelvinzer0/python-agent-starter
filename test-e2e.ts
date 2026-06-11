import { chromium } from 'playwright';

const BASE = 'https://aikuprojgs.edgeone.dev';
const TIMEOUT = 120_000;

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

async function register(page: any, email: string, username: string, password: string) {
  // Switch to register mode (default is login)
  const toggle = page.getByText("Don't have an account? Register");
  await toggle.click();
  await page.waitForTimeout(300);

  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[placeholder="Your name"]').fill(username);
  await page.locator('input[type="password"]').fill(password);
  await page.locator('button[type="submit"]').click();

  // Wait for modal to disappear
  await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 10_000 });
}

async function login(page: any, email: string, password: string) {
  // Default mode is login — no need to toggle
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.locator('button[type="submit"]').click();

  await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 10_000 });
}

async function freshPage(browser: any) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForSelector('input[type="email"]', { timeout: 15_000 });
  return { ctx, page };
}

const browser = await chromium.launch({ headless: true });

// ═══════════════════════════════════════════
// AUTH TESTS
// ═══════════════════════════════════════════
await test('Auth: register new user', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'pw@test.com', 'PWUser', 'test123456');

  const token = await page.evaluate(() => localStorage.getItem('eo_auth_token'));
  const user = JSON.parse(await page.evaluate(() => localStorage.getItem('eo_auth_user') || '{}'));
  assert(!!token, 'No token in localStorage');
  assert(user.email === 'pw@test.com', `Wrong email: ${user.email}`);
  await ctx.close();
});

await test('Auth: persists across reload', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'reload@test.com', 'RelUser', 'test123456');

  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(3000);

  const modal = await page.$('input[type="email"]');
  assert(!modal, 'Auth modal shown after reload');
  await ctx.close();
});

await test('Auth: logout shows modal', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'logout@test.com', 'LogUser', 'test123456');

  // Click logout button in sidebar
  const logoutBtn = page.locator('button[title="Logout"]');
  await logoutBtn.click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });
  await ctx.close();
});

await test('Auth: wrong password rejected (same context)', async () => {
  // Register in same context, then logout, then try wrong password
  const { ctx, page } = await freshPage(browser);
  await register(page, 'wrong@test.com', 'WrongUser', 'pass123456');

  // Logout
  await page.locator('button[title="Logout"]').click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });

  // Default mode is login — try wrong password
  await page.locator('input[type="email"]').fill('wrong@test.com');
  await page.locator('input[type="password"]').fill('badpassword');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(1500);

  const stillVisible = await page.$('input[type="email"]');
  assert(!!stillVisible, 'Modal should still show after wrong password');

  // Now correct password
  await page.locator('input[type="password"]').fill('pass123456');
  await page.locator('button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 10_000 });
  await ctx.close();
});

await test('Auth: duplicate email rejected (same context)', async () => {
  const { ctx, page } = await freshPage(browser);

  // Register first
  await register(page, 'dup@test.com', 'DupUser1', 'pass123456');

  // Logout
  await page.locator('button[title="Logout"]').click();
  await page.waitForSelector('input[type="email"]', { timeout: 5_000 });

  // Try register again with same email
  const toggle = page.getByText("Don't have an account? Register");
  await toggle.click();
  await page.waitForTimeout(300);
  await page.locator('input[type="email"]').fill('dup@test.com');
  await page.locator('input[placeholder="Your name"]').fill('DupUser2');
  await page.locator('input[type="password"]').fill('pass123456');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(2000);

  const stillVisible = await page.$('input[type="email"]');
  assert(!!stillVisible, 'Should show error for duplicate email');
  await ctx.close();
});

// ═══════════════════════════════════════════
// WORKSPACE TESTS
// ═══════════════════════════════════════════
await test('Workspace: templates loaded from backend', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'ws1@test.com', 'WS1User', 'test123456');

  // Wait for sidebar to show workspace files
  await page.waitForTimeout(5000);

  const sidebar = await page.textContent('aside');
  assert(sidebar!.includes('IDENTITY.md'), `IDENTITY.md not in sidebar`);
  assert(sidebar!.includes('BOOTSTRAP.md'), `BOOTSTRAP.md not in sidebar`);
  await ctx.close();
});

await test('Workspace: IDB persists across reload', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'idb@test.com', 'IDBUser', 'test123456');
  await page.waitForTimeout(2000);

  // Manually write to IDB
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
          storageKey: 'idb-test/myfile.txt',
          conversationId: 'idb-test',
          filepath: 'myfile.txt',
          content: 'IDB test content',
          size: 17, hash: 'abc', updatedAt: Date.now(), createdAt: Date.now(),
        });
        tx.oncomplete = () => resolve();
      };
      req.onerror = () => resolve();
    });
  });

  await page.reload({ waitUntil: 'networkidle', timeout: TIMEOUT });
  await page.waitForTimeout(3000);

  const hasFile = await page.evaluate(async () => {
    return new Promise<boolean>((resolve) => {
      const req = indexedDB.open('python-starter-workspace-db', 1);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('files')) { resolve(false); return; }
        const get = db.transaction('files', 'readonly').objectStore('files').get('idb-test/myfile.txt');
        get.onsuccess = () => resolve(!!get.result);
        get.onerror = () => resolve(false);
      };
      req.onerror = () => resolve(false);
    });
  });

  assert(hasFile, 'IDB file lost after reload');
  await ctx.close();
});

await test('Workspace: sidebar shows file list', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'sidebar@test.com', 'SideUser', 'test123456');

  // Wait for files to load
  await page.waitForTimeout(5000);

  // Check sidebar has file entries
  const fileItems = await page.locator('aside').textContent();
  const hasFiles = fileItems!.includes('IDENTITY') || fileItems!.includes('SOUL') || fileItems!.includes('TOOLS');
  assert(hasFiles, `No workspace files in sidebar`);
  await ctx.close();
});

await test('Workspace: different conversations isolated', async () => {
  const { ctx, page } = await freshPage(browser);
  await register(page, 'iso@test.com', 'IsoUser', 'test123456');
  await page.waitForTimeout(2000);

  // Get current CID
  const cid1 = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));

  // Write file to IDB for this CID
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
  }, cid1);

  // Create new chat (new CID)
  await page.locator('button:has-text("New Chat")').first().click();
  await page.waitForTimeout(2000);

  const cid2 = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));
  assert(cid1 !== cid2, 'New chat should have different CID');

  // Check IDB doesn't have the old file for new CID
  const hasFile = await page.evaluate(async (cid: string) => {
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

  assert(!hasFile, 'New CID should not have old CID file');
  await ctx.close();
});

// ═══════════════════════════════════════════
// API TESTS
// ═══════════════════════════════════════════
await test('API: /workspace/files returns templates', async () => {
  const resp = await fetch(`${BASE}/workspace/files`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-test-001' },
    body: JSON.stringify({ action: 'list', conversationId: 'test' }),
  });
  const data = await resp.json();
  assert(data.files && data.files.length > 0, `Expected files, got: ${JSON.stringify(data)}`);
});

await test('API: /workspace/files read template', async () => {
  const resp = await fetch(`${BASE}/workspace/files`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-test-002' },
    body: JSON.stringify({ action: 'read', filename: 'IDENTITY.md', conversationId: 'test' }),
  });
  const data = await resp.json();
  assert(data.content && data.content.length > 0, 'IDENTITY.md content empty');
});

await test('API: /chat streams response', async () => {
  const resp = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'api-test-003' },
    body: JSON.stringify({ message: 'Say just "pong"' }),
  });
  assert(resp.ok, `Chat returned ${resp.status}`);
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let text = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
    if (text.includes('text_delta')) break;
  }
  assert(text.includes('text_delta'), `No text_delta in response`);
});

await test('API: /auth/register pass-through', async () => {
  const resp = await fetch(`${BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'auth-api-test-001' },
    body: JSON.stringify({ user_id: 'test123', email: 'api@test.com', username: 'API', token: 'tok123' }),
  });
  const data = await resp.json();
  assert(data.success === true, `Register failed: ${JSON.stringify(data)}`);
  assert(data.email === 'api@test.com', `Wrong email in response`);
});

await test('API: /auth/me validates token', async () => {
  const resp = await fetch(`${BASE}/auth/me`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'makers-conversation-id': 'auth-api-test-002',
      'Authorization': 'Bearer tok123',
    },
    body: JSON.stringify({ user_id: 'test123', email: 'api@test.com', username: 'API' }),
  });
  const data = await resp.json();
  assert(data.success === true, `Me failed: ${JSON.stringify(data)}`);
});

await test('API: /auth/me rejects no token', async () => {
  const resp = await fetch(`${BASE}/auth/me`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'makers-conversation-id': 'auth-api-test-003' },
    body: JSON.stringify({}),
  });
  const data = await resp.json();
  assert(data.error === 'Not authenticated', `Expected not authenticated, got: ${JSON.stringify(data)}`);
});

await browser.close();

console.log('\n═══════════════════════════════════════');
console.log(`  RESULTS: ${passed} passed, ${failed} failed`);
console.log('═══════════════════════════════════════');

process.exit(failed > 0 ? 1 : 0);
