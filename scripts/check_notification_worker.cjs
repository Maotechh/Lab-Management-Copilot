const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  const listeners = {};
  const displayed = [];
  const navigation = [];
  let windows = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../consumable_assistant/static/sw.js'), 'utf8'), {
    URL,
    self: {
      location: {origin: 'https://10.15.21.115'},
      addEventListener: (name, callback) => { listeners[name] = callback; },
      registration: {showNotification: async (title, options) => { displayed.push({title, ...options}); }},
    },
    clients: {
      matchAll: async () => windows,
      openWindow: async url => { navigation.push(['open', url]); },
    },
  });
  const dispatch = async (name, event) => {
    let completed;
    listeners[name]({...event, waitUntil: promise => { completed = promise; }});
    await completed;
  };
  await dispatch('push', {data: {json: () => ({test: true, title: 'Private inventory', url: 'https://example.com'})}});
  assert.equal(displayed[0].title, '耗材管理测试提醒');
  assert.equal(displayed[0].data.url, '/?tab=alerts');
  assert.ok(!JSON.stringify(displayed[0]).includes('Private inventory'));
  await dispatch('push', {data: {json: () => { throw Error('Malformed payload'); }}});
  assert.equal(displayed[1].title, '耗材管理提醒');
  const notification = {close: () => navigation.push(['close'])};
  await dispatch('notificationclick', {notification});
  assert.deepEqual(navigation, [['close'], ['open', '/?tab=alerts']]);
  navigation.length = 0;
  windows = [{url: 'https://10.15.21.115/',
    navigate: async url => { await Promise.resolve(); navigation.push(['navigate', url]); },
    focus: async () => { navigation.push(['focus']); },
  }];
  await dispatch('notificationclick', {notification});
  assert.deepEqual(navigation, [['close'], ['navigate', '/?tab=alerts'], ['focus']]);
  console.log('Notification worker checks passed: generic payloads, marked tests and detail navigation');
})().catch(error => { console.error(error); process.exitCode = 1; });
