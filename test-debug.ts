import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

// Capture ALL console logs
page.on('console', msg => {
  const text = msg.text();
  if (text.includes('TOOL-DEBUG') || text.includes('IDB-PERSIST') || text.includes('tool_debug')) {
    console.log(`BROWSER [${msg.type()}]:`, text);
  }
});

// Also capture page errors
page.on('pageerror', err => console.log('PAGE ERROR:', err.message));

await page.goto('https://aikuprojgs.edgeone.dev', { waitUntil: 'networkidle', timeout: 120000 });
await page.waitForSelector('input[type="email"]', { timeout: 15000 });

const toggle = page.getByText("Don't have an account? Register");
await toggle.click();
await page.waitForTimeout(300);
await page.locator('input[type="email"]').fill('debug2@test.com');
await page.locator('input[placeholder="Your name"]').fill('Debug2');
await page.locator('input[type="password"]').fill('test123456');
await page.locator('button[type="submit"]').click();
await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 10000 });
await page.waitForTimeout(3000);

const ta = await page.waitForSelector('textarea', { timeout: 10000 });
await ta.fill('Use local_write_file to create test2.txt with content hi');
await page.keyboard.press('Enter');

await page.waitForFunction(() => {
  return document.body.innerText.includes('test2') || document.body.innerText.includes('Successfully');
}, { timeout: 120000 });
await page.waitForTimeout(5000);

console.log('--- Done ---');
await ctx.close();
await browser.close();
