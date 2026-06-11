import { chromium } from 'playwright';

const BASE = 'https://aikuprojgs.edgeone.dev';
const TIMEOUT = 60_000;

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

// Capture ALL browser console output
const consoleLogs: string[] = [];
page.on('console', (msg) => {
  consoleLogs.push(`[${msg.type()}] ${msg.text()}`);
});

await page.goto(BASE, { waitUntil: 'networkidle', timeout: TIMEOUT });
await page.waitForSelector('input[type="email"]', { timeout: 8_000 });

// Register
const toggle = page.getByText("Don't have an account? Register");
await toggle.click();
await page.waitForTimeout(200);
await page.locator('input[type="email"]').fill('debug@test.com');
await page.locator('input[placeholder="Your name"]').fill('DBG');
await page.locator('input[type="password"]').fill('test123456');
await page.locator('button[type="submit"]').click();
await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 8_000 });
console.log('✅ Registered');

// Wait for templates to load
await page.waitForTimeout(3000);

// Store conversation ID
const cid = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));
console.log(`CID: ${cid}`);

// Check IDB BEFORE tool call
const before = await page.evaluate(async () => {
  return new Promise<string[]>((resolve) => {
    const req = indexedDB.open('python-starter-workspace-db', 1);
    req.onsuccess = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('files')) { resolve(['NO_STORE']); return; }
      const storeNames = Array.from(db.objectStoreNames);
      let indexes: string[] = [];
      try {
        const tx = db.transaction('files', 'readonly');
        const store = tx.objectStore('files');
        indexes = Array.from(store.indexNames);
      } catch {}
      const cid = localStorage.getItem('eo_conversation_id');
      try {
        const tx = db.transaction('files', 'readonly');
        const store = tx.objectStore('files');
        const index = store.index('byConversation');
        const getAll = index.getAll(cid);
        getAll.onsuccess = () => resolve(getAll.result.map((r: any) => r.filepath));
        getAll.onerror = () => resolve(['ERROR: ' + getAll.error]);
      } catch (e: any) {
        resolve(['EXCEPTION: ' + e.message, 'stores: ' + storeNames.join(','), 'indexes: ' + indexes.join(',')]);
      }
    };
    req.onerror = () => resolve(['DB_ERROR: ' + req.error]);
  });
});
console.log('IDB before:', JSON.stringify(before));

// Send message
const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
await ta.fill('Use local_write_file to create a file called tool_persist.txt with content "tool_call_works"');
await page.keyboard.press('Enter');
console.log('Message sent, waiting for response...');

// Wait for "Successfully"
await page.waitForFunction(() => {
  return document.body.innerText.includes('tool_persist') || document.body.innerText.includes('Successfully');
}, { timeout: 90_000 });
console.log('Got "Successfully" in DOM');

// Wait extra for IDB write
await page.waitForTimeout(3000);

// Check IDB AFTER tool call
const after = await page.evaluate(async () => {
  return new Promise<string[]>((resolve) => {
    const req = indexedDB.open('python-starter-workspace-db', 1);
    req.onsuccess = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('files')) { resolve(['NO_STORE']); return; }
      const cid = localStorage.getItem('eo_conversation_id');
      const tx = db.transaction('files', 'readonly');
      const store = tx.objectStore('files');
      const index = store.index('byConversation');
      const getAll = index.getAll(cid);
      getAll.onsuccess = () => resolve(getAll.result.map((r: any) => r.filepath));
      getAll.onerror = () => resolve(['ERROR: ' + getAll.error]);
    };
    req.onerror = () => resolve(['DB_ERROR: ' + req.error]);
  });
});
console.log('IDB after:', JSON.stringify(after));

// Also try to get ALL files in IDB (any conversation)
const allFiles = await page.evaluate(async () => {
  return new Promise<any[]>((resolve) => {
    const req = indexedDB.open('python-starter-workspace-db', 1);
    req.onsuccess = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('files')) { resolve([]); return; }
      const tx = db.transaction('files', 'readonly');
      const store = tx.objectStore('files');
      const getAll = store.getAll();
      getAll.onsuccess = () => resolve(getAll.result.map((r: any) => ({
        storageKey: r.storageKey,
        conversationId: r.conversationId,
        filepath: r.filepath,
      })));
      getAll.onerror = () => resolve([]);
    };
    req.onerror = () => resolve([]);
  });
});
console.log('ALL IDB files:', JSON.stringify(allFiles.filter(f => f.filepath === 'tool_persist.txt')));

// Check if tool_persist.txt exists in IDB at all (any conversation)
const foundInAny = allFiles.some(f => f.filepath === 'tool_persist.txt');
console.log('tool_persist.txt in any conversation:', foundInAny);

// Dump relevant console logs
console.log('\n=== BROWSER CONSOLE ===');
for (const log of consoleLogs) {
  if (log.includes('file_changed') || log.includes('tool_debug') || log.includes('IDB') || log.includes('tool_persist')) {
    console.log(log);
  }
}
if (consoleLogs.filter(l => l.includes('file_changed') || l.includes('tool_debug')).length === 0) {
  console.log('(no file_changed or tool_debug logs found)');
  console.log('All console logs:');
  for (const log of consoleLogs.slice(-20)) {
    console.log('  ' + log);
  }
}

await browser.close();
