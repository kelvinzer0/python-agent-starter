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
await page.locator('input[type="email"]').fill('debug2@test.com');
await page.locator('input[placeholder="Your name"]').fill('DBG2');
await page.locator('input[type="password"]').fill('test123456');
await page.locator('button[type="submit"]').click();
await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 8_000 });
console.log('✅ Registered');

await page.waitForTimeout(3000);
const cid = await page.evaluate(() => localStorage.getItem('eo_conversation_id'));
console.log(`CID: ${cid}`);

// Send message
const ta = await page.waitForSelector('textarea', { timeout: 8_000 });
await ta.fill('Use local_write_file to create a file called tool_persist.txt with content "tool_call_works"');
await page.keyboard.press('Enter');
console.log('Message sent...');

// Wait for response
await page.waitForFunction(() => {
  return document.body.innerText.includes('tool_persist') || document.body.innerText.includes('Successfully');
}, { timeout: 90_000 });
console.log('Got response');

await page.waitForTimeout(3000);

// Get raw SSE events captured by frontend
const rawEvents = await page.evaluate(() => (window as any).__raw_sse_events || []);
console.log('\n=== RAW SSE EVENTS ===');
for (const ev of rawEvents) {
  console.log(`  ${ev.eventType} (data: ${ev.dataLen} bytes, ts: ${ev.ts})`);
}

// Check IDB
const idbFiles = await page.evaluate(async () => {
  return new Promise<any[]>((resolve) => {
    const req = indexedDB.open('python-starter-workspace-db', 1);
    req.onsuccess = () => {
      const db = req.result;
      const cid = localStorage.getItem('eo_conversation_id');
      try {
        const tx = db.transaction('files', 'readonly');
        const store = tx.objectStore('files');
        const index = store.index('byConversation');
        const getAll = index.getAll(cid);
        getAll.onsuccess = () => resolve(getAll.result.map((r: any) => r.filepath));
        getAll.onerror = () => resolve(['ERROR']);
      } catch { resolve(['EXCEPTION']); }
    };
    req.onerror = () => resolve(['DB_ERROR']);
  });
});
console.log('\n=== IDB FILES ===');
console.log(idbFiles);

// Check relevant console logs
console.log('\n=== BROWSER CONSOLE (relevant) ===');
for (const log of consoleLogs) {
  if (log.includes('file_changed') || log.includes('tool_debug') || log.includes('IDB') || 
      log.includes('file-bridge') || log.includes('WebSocket') || log.includes('raw_sse')) {
    console.log(log);
  }
}

const hasToolPersist = idbFiles.includes('tool_persist.txt');
console.log(`\ntool_persist.txt in IDB: ${hasToolPersist}`);

await browser.close();
process.exit(hasToolPersist ? 0 : 1);
