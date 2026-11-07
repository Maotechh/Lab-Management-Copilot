self.addEventListener('push', event => {
  let test = false;
  try { test = event.data?.json()?.test === true; } catch { /* Keep a generic reminder. */ }
  event.waitUntil(self.registration.showNotification(test ? '耗材管理测试提醒' : '耗材管理提醒', {
    body: test ? '这是设备通知测试，请进入校内提醒页面查看。' : '库存或实验安排需要关注，请进入校内系统查看。',
    icon: '/static/icon-192.png', tag: test ? 'lab-delivery-test' : 'lab-attention', data: {url:'/?tab=alerts'},
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(async windows => {
    const existing=windows.find(client=>new URL(client.url).origin===self.location.origin);
    if (existing) { await existing.navigate('/?tab=alerts'); return existing.focus(); }
    return clients.openWindow('/?tab=alerts');
  }));
});
