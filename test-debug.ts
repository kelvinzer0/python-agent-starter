import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

page.on('console', msg => {
  if (msg.text().includes('IDB-PERSIST')) console.log('BROWSER:', msg.text());
});

await page.goto('https://aikuprojgs.edgeone.dev', { waitUntil: 'networkidle', timeout: 120000 });
await page.waitForSelector('input[type="email"]', { timeout: 15000 });

const toggle = page.getByText("Don't have an account? Register");
await toggle.click();
await page.waitForTimeout(300);
await page.locator('input[type="email"]').fill('debug@test.com');
await page.locator('input[placeholder="Your name"]').fill('Debug');
await page.locator('input[type="password"]').fill('test123456');
await page.locator('button[type="submit"]').click();
await page.waitForFunction(() => !document.querySelector('input[type="email"]'), { timeout: 10000 });
await page.waitForTimeout(3000);

const ta = await page.waitForSelector('textarea', { timeout: 10000 });
await ta.fill('Use local_write_file to create test_debug.txt with content hello_debug');
await page.keyboard.press('Enter');

await page.waitForFunction(() => {
  return document.body.innerText.includes('test_debug') || document.body.innerText.includes('Successfully');
}, { timeout: 120000 });
await page.waitForTimeout(5000);

console.log('--- Done ---');
await ctx.close();
await browser.close();
