/* Browser regression checks against isolated local fixtures, never production. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const url = process.env.TEST_URL || 'http://127.0.0.1:8039';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(url).hostname), 'Use an isolated local test server');

async function main() {
  const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1440, height:1000}});
    const errors = [];
    const writes = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      localStorage.setItem('lab-actor', '验证教师');
      window.EventSource = class { addEventListener() {} close() {} };
    });
    let latest = 200;
    let olderRequests = 0;
    const message = id => ({id, role:id % 2 ? 'user' : 'assistant', actor:'验证教师',
      content:`历史消息 ${id}`, created_at:'2026-11-09 00:00:00',
      result:id % 2 ? null : {status:'completed', result:{id}}});
    await page.route('**/api/**', async route => {
      const request = route.request();
      const endpoint = new URL(request.url());
      if (request.method() !== 'GET') {
        writes.push(endpoint.pathname);
        return route.abort();
      }
      if (endpoint.pathname === '/api/chat/conversations') {
        return route.fulfill({json:{conversations:[{id:'history-fixture', title:'历史分页验证', updated_at:'2026-11-09 00:00:00'}], next_before:null}});
      }
      if (endpoint.pathname === '/api/chat/conversations/history-fixture') {
        const before = Number(endpoint.searchParams.get('before') || latest + 1);
        const first = Math.max(1, before - 100);
        if (endpoint.searchParams.has('before')) olderRequests++;
        return route.fulfill({json:{messages:Array.from({length:before - first}, (_, index) => message(first + index)),
          tasks:[], runs:[], attachments:[], last_event_id:latest, next_before:first > 1 ? first : null}});
      }
      return route.continue();
    });
    await page.goto(`${url}/?tab=chat&conversation=history-fixture`);
    await page.waitForFunction(() => document.querySelectorAll('#chatMessages [data-message-id]').length === 100);
    await page.locator('[data-message-id="150"] summary').click();
    await page.evaluate(() => { window.retainedMessage = document.querySelector('[data-message-id="150"]'); });
    await page.locator('#olderMessages').click();
    await page.waitForFunction(() => document.querySelectorAll('#chatMessages [data-message-id]').length === 200);
    assert.equal(olderRequests, 1, 'One click must request one history page');
    assert.equal(await page.locator('#olderMessages').isVisible(), false);
    assert.equal(await page.evaluate(() => window.retainedMessage === document.querySelector('[data-message-id="150"]')), true);
    assert.equal(await page.locator('[data-message-id="150"] details').getAttribute('open'), '');
    latest = 201;
    await page.evaluate(() => updateChatSnapshot());
    assert.equal(await page.locator('#chatMessages [data-message-id]').count(), 201);
    assert.equal(await page.locator('[data-message-id="1"]').count(), 1, 'New events must preserve loaded history');
    assert.equal(await page.locator('#olderMessages').isVisible(), false);
    assert.equal(await page.evaluate(() => window.retainedMessage === document.querySelector('[data-message-id="150"]')), true);
    const tail = await page.locator('#chatMessages [data-message-id]').last().getAttribute('data-message-id');
    assert.equal(tail, '201');
    for (const size of [{width:375,height:812}, {width:844,height:390}, {width:1440,height:1000}]) {
      await page.setViewportSize(size);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, [], 'History checks must never submit business operations');
    console.log(JSON.stringify({chat_history:'passed', messages:201, older_requests:olderRequests, viewports:[375,844,1440]}));
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
