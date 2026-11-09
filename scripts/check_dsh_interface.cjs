/* Exercise UI states with isolated HTTP fixtures; never writes production data. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const url=process.env.TEST_URL||'http://127.0.0.1:8039';
assert.ok(['127.0.0.1','localhost'].includes(new URL(url).hostname));
const output=process.env.BROWSER_CHECK_OUTPUT||'/private/tmp/lab-dsh-interface';fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']});
  await context.addInitScript(()=>{
   localStorage.setItem('lab-actor','验证教师');
   window.fixtureEvents=[];
   window.EventSource=class{constructor(){window.fixtureEvents.push(this);}addEventListener(){}close(){this.closed=true;}};
  });
  const page=await context.newPage();const errors=[];const external=[];page.on('pageerror',e=>errors.push(e.message));
  page.on('request',request=>{if(!request.url().startsWith(url))external.push(request.url());});
  let created=0,submitted=0,event=1,runs=[],tasks=[];let delayedSnapshot;
  const markdown='**中文加粗**与正文\n\n| 耗材 | 数量 |\n| --- | --- |\n| 烧杯 | 12 个 |\n\n```python\nprint("烧杯")\n```\n\n<script>window.injected=true</script>\n\n[危险链接](javascript:alert(1))\n\n![清单照片](https://invalid.example/tracker.png)';
  const message=(id,role,content)=>({id,role,content,actor:'验证教师',created_at:'2026-11-09T01:00:00Z',result:null});
  const conversations={empty:[],history:[message(1,'user','查询一百毫升烧杯'),message(2,'assistant',markdown)]};
  const snapshot=id=>({messages:conversations[id]||[],tasks:id==='history'?tasks:[],runs:id==='history'?runs:[],activities:runs.length?[{run_id:'run-1',name:'inventory_search',error:null}]:[],last_event_id:event,next_before:null});
  const chatRoute=async route=>{
   const request=route.request();const endpoint=new URL(request.url());const path=endpoint.pathname;
   if(path==='/api/chat/conversations'){
    if(request.method()==='POST'){created++;conversations['created-'+created]=[];await new Promise(resolve=>setTimeout(resolve,150));return route.fulfill({json:{conversation_id:'created-'+created}});}
    return route.fulfill({json:{conversations:Object.keys(conversations).map(id=>({id,title:id==='empty'?'新的对话':id==='history'?'耗材查询':id,updated_at:'2026-11-09T01:00:00Z'})),next_before:null}});
   }
   if(path.endsWith('/messages')){
    submitted++;const data=request.postDataJSON();assert.equal(data.text,'发送状态检查');
    conversations.history.push(message(3,'user',data.text));runs=[{run_id:'run-1',status:'queued',actor:'验证教师'}];event++;
    await new Promise(resolve=>setTimeout(resolve,200));return route.fulfill({json:{run_id:'run-1',status:'queued'}});
   }
   if(path.endsWith('/cancel')){runs[0].cancel_requested=true;event++;return route.fulfill({json:{status:'running'}});}
   const id=path.match(/^\/api\/chat\/conversations\/([^/]+)$/)?.[1];
   if(id){if(delayedSnapshot){const hold=delayedSnapshot;delayedSnapshot=null;await hold.promise;return route.fulfill({json:hold.value});}return route.fulfill({json:snapshot(id)});}
   return route.continue();
  };
  await page.route('**/api/chat/**',chatRoute);
  await page.goto(url+'/?conversation=empty');await page.waitForFunction(()=>document.querySelector('#conversationMain').dataset.phase==='hero');
  assert.equal(await page.locator('#previewBtn').isDisabled(),true);
  const dimensions=await page.locator('#newConversation').evaluate(e=>({height:e.getBoundingClientRect().height,width:document.querySelector('#sidebar').getBoundingClientRect().width}));
  assert.equal(dimensions.height,38);assert.equal(dimensions.width,280);
  await page.screenshot({path:output+'/hero-desktop.png'});
  await page.locator('#collapseNavigation').click();assert.equal(await page.locator('#sidebar').evaluate(e=>e.getBoundingClientRect().width),56);
  await page.locator('#collapseNavigation').click();
  await page.locator('[data-conversation=history]').click();await page.locator('.dsh-md-markdown table').waitFor();
  assert.equal(await page.locator('.dsh-md-markdown strong').innerText(),'中文加粗');
  await page.locator('[data-copy-code]').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'print("烧杯")\n');
  assert.equal(await page.locator('.dsh-md-markdown img, .dsh-md-markdown script').count(),0);
  assert.equal(await page.evaluate(()=>Boolean(window.injected)),false);
  assert.equal(await page.locator('.dsh-md-markdown a[href^="javascript:"]').count(),0);
  await page.locator('[data-copy-message="2"]').click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),markdown);
  await page.locator('#chatText').fill('草稿保留');await page.locator('[data-conversation=empty]').click();
  await page.locator('[data-conversation=history]').click();assert.equal(await page.locator('#chatText').inputValue(),'草稿保留');
  await page.locator('#chatText').fill('');await page.screenshot({path:output+'/messages-desktop.png'});
  tasks=[{id:'plan-1',status:'pending_confirmation',kind:'stock',initiator:'教师',plan_hash:'hash-1',expires_at:'2099-01-01T00:00:00Z',preview:{operation:{kind:'stock',details:{action:'consume',quantity:'2',unit:'个'}},changes:[]}}];event++;
  await page.evaluate(()=>updateChatSnapshot());await page.locator('#closePlan').click();
  assert.equal(await page.locator('#commitBtn').isDisabled(),false);
  await page.locator('#chatText').fill('修改用量');assert.equal(await page.locator('#commitBtn').isDisabled(),true);
  await page.locator('#chatText').fill('');await page.locator('#chatFile').setInputFiles({name:'清单.csv',mimeType:'text/csv',buffer:Buffer.from('名称,数量\n烧杯,2')});
  assert.equal(await page.locator('#commitBtn').isDisabled(),true);await page.locator('#attachmentPreviews button').click();
  assert.equal(await page.locator('#commitBtn').isDisabled(),false);
  await page.evaluate(()=>window.fixtureEvents.at(-1).onerror());assert.equal(await page.locator('#commitBtn').isDisabled(),true);
  await page.evaluate(()=>window.fixtureEvents.at(-1).onopen());await page.waitForFunction(()=>!document.querySelector('#commitBtn').disabled);
  const stale=JSON.parse(JSON.stringify(snapshot('history')));let release;
  delayedSnapshot={promise:new Promise(resolve=>{release=resolve;}),value:stale};
  const old=page.evaluate(()=>updateChatSnapshot());await page.waitForTimeout(80);
  tasks=[];event++;await page.evaluate(()=>updateChatSnapshot());release();await old;
  assert.equal(await page.locator('#taskBanner').isVisible(),false,'Late response must not restore an invalid plan');
  await page.locator('#chatText').fill('发送状态检查');await page.locator('#previewBtn').click();
  assert.equal(await page.locator('#previewBtn').getAttribute('aria-busy'),'true');
  assert.equal(await page.locator('#chatFile').isDisabled(),true);
  await page.waitForFunction(()=>document.querySelector('#previewBtn').getAttribute('aria-busy')==='false');
  assert.equal(submitted,1);assert.equal(await page.locator('#chatText').inputValue(),'');
  assert.equal(await page.locator('#stopChatRun').isVisible(),true);
  await page.locator('#stopChatRun').click();await page.waitForFunction(()=>document.querySelector('#stopChatRun').disabled);
  await page.waitForFunction(()=>document.querySelector('#chatRunStatus').textContent.includes('正在取消'));
  runs=[];event++;await page.evaluate(()=>updateChatSnapshot());assert.equal(await page.locator('#stopChatRun').isVisible(),false);
  await page.locator('#newConversation').dblclick();await page.waitForFunction(()=>document.querySelector('#conversationMain').dataset.phase==='hero');assert.equal(created,1);
  await page.locator('#actorButton').click();await page.locator('#themePreference').selectOption('dark');await page.locator('[aria-label="关闭设置"]').click();
  assert.equal(await page.locator('body').getAttribute('data-ds-dark-theme'),'');await page.screenshot({path:output+'/hero-dark.png'});
  for(const size of [{width:390,height:844},{width:844,height:390},{width:1440,height:1000}]){
   await page.setViewportSize(size);
   if(size.width<768){await page.locator('#openNavigation').click();await page.locator('#conversationSearch').fill('耗材');await page.locator('[data-conversation=history]').click();await page.locator('#navigationDialog').waitFor({state:'hidden'});await page.locator('#openNavigation').click();await page.locator('#actorButton').click();assert.equal(await page.locator('#navigationDialog').isVisible(),false);await page.locator('[aria-label="关闭设置"]').click();await page.locator('#openNavigation').click();await page.setViewportSize({width:844,height:390});await page.locator('#navigationDialog').waitFor({state:'hidden'});await page.setViewportSize(size);}
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`${size.width}px overflow`);
   await page.screenshot({path:`${output}/messages-${size.width}.png`});
  }
  await page.locator('#actorButton').click();await page.locator('#themePreference').selectOption('light');await page.locator('[aria-label="关闭设置"]').click();
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:output+'/messages-phone.png'});
  const phoneContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  await phoneContext.addInitScript(()=>localStorage.setItem('lab-actor','触摸验证教师'));
  const phone=await phoneContext.newPage();phone.on('pageerror',e=>errors.push(e.message));await phone.route('**/api/chat/**',chatRoute);
  await phone.goto(url+'/?conversation=empty');await phone.waitForFunction(()=>document.querySelector('#conversationMain').dataset.phase==='hero');
  const touch=await phone.locator('#previewBtn').evaluate(e=>({height:e.getBoundingClientRect().height,font:getComputedStyle(document.querySelector('#chatText')).fontSize}));
  assert.ok(touch.height>=40);assert.equal(touch.font,'16px');
  await phone.evaluate(()=>voiceControls('recording','录音界面检查'));
  await phone.screenshot({path:output+'/touch-recording.png'});
  assert.equal(await phone.locator('#previewBtn').isDisabled(),true);
  await phone.evaluate(()=>voiceControls('idle',''));
  await phone.locator('#openNavigation').click();await phone.locator('#closeNavigation').click();
  assert.equal(await phone.locator('#navigationDialog').isVisible(),false);
  assert.equal(await phone.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await phone.screenshot({path:output+'/touch-hero.png'});await phoneContext.close();
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);console.log(JSON.stringify({dsh_interface:'passed',checks:['hero','navigation','markdown','copy','drafts','plan_edit','attachment_edit','reconnect','stale_response','send','cancel','single_create','dark','phone_drawer','resize'],errors}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
