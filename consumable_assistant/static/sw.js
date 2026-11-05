self.addEventListener('push', event => {
  event.waitUntil(self.registration.showNotification('耗材管理提醒', {
    body: '库存或实验安排需要关注，请进入校内系统查看。',
    icon: '/static/icon-192.png', tag: 'lab-attention', data: {url:'/?tab=alerts'},
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(windows => {
    const existing=windows.find(client=>new URL(client.url).origin===self.location.origin);
    if (existing) { existing.navigate('/?tab=alerts'); return existing.focus(); }
    return clients.openWindow('/?tab=alerts');
  }));
});
