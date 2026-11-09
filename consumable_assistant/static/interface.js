/* Responsive laboratory workspace. All business writes remain in app.js/API. */
const routes = {
  chat:['聊天助手','chat'], search:['库存与位置','search'], tasks:['共享事务','tasks'],
  loans:['借用与归还','tasks'], lessons:['实验安排','tasks'], purchases:['采购跟踪','tasks'], alerts:['库存提醒','tasks'],
  more:['更多工具','more'], prepare:['清单核对','more'], admin:['表格与位置','more'], maintenance:['清点与维护','more'], history:['操作记录','more'],
};
const ui = {route:'chat', conversations:[], reviewOpen:false, previewKey:null, tasks:[], nextTaskCursor:null, actorWaiter:null};
const recovery=document.createElement('div');recovery.id='messageRecovery';recovery.className='notice message-recovery';recovery.hidden=true;
$('#composer').before(recovery);
window.renderMessageRecovery=function(){
  const pending=JSON.parse(localStorage.getItem('lab-chat-pending')||'null');recovery.hidden=!pending;
  if(!pending)return;
  recovery.replaceChildren();const text=document.createElement('span');text.textContent='上一条消息的结果尚未确认，请重试原消息。';
  const retry=document.createElement('button');retry.type='button';retry.className='quiet';retry.textContent='重试原消息';
  retry.addEventListener('click',()=>{if(pending.files.length&&!$('#chatFile').files.length){toast('请重新选择原消息的附件');$('#chatFile').click();return;}$('#chatText').value=pending.text;$('#chatText').dispatchEvent(new Event('input'));sendChatMessage();});recovery.append(text,retry);
};
const sidebarHome = document.createComment('desktop sidebar');
$('#sidebar').before(sidebarHome);
const planHome = document.createComment('review panel');
$('#planPanel').before(planHome);
const planDialog = document.createElement('dialog');
planDialog.id='planDialog'; planDialog.setAttribute('aria-labelledby','planTitle');
planDialog.className='plan-dialog'; document.body.append(planDialog);

window.navigateTo = function(tab, {replace=false, push=true} = {}) {
  if (!routes[tab]) return;
  ui.route=tab;
  document.body.dataset.page=tab;
  document.querySelectorAll('.tab-panel').forEach(panel=>panel.classList.toggle('active',panel.id==='tab-'+tab));
  document.querySelectorAll('.tab[data-tab]').forEach(button=>{
    const primary=Boolean(button.closest('.primary-nav,.bottom-nav'));
    const selected=button.dataset.tab===(primary?routes[tab][1]:tab);
    button.classList.toggle('active',selected);
    if (selected) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  $('#pageTitle').textContent=routes[tab][0];
  $('#taskNavigation').hidden=routes[tab][1]!=='tasks';
  $('#sectionBack').hidden=routes[tab][1]!=='more'||tab==='more';
  $('#metrics').hidden=tab!=='search';
  if(push){const url=new URL(location.href);url.searchParams.set('tab',tab);if(state.chatConversationId)url.searchParams.set('conversation',state.chatConversationId);history[replace?'replaceState':'pushState']({},'',url);}
  if($('#navigationDialog').open)$('#navigationDialog').close();
  if(tab!=='chat'&&ui.reviewOpen)setReviewOpen(false);
  if(tab==='tasks')loadSharedTasks().catch(showError);
  $('#mainContent').scrollTop=0;
  window.lucide?.createIcons();
};
window.addEventListener('popstate',()=>{
  navigateTo(new URLSearchParams(location.search).get('tab')||'chat',{push:false});
  const id=new URLSearchParams(location.search).get('conversation');
  if(id&&id!==state.chatConversationId)selectConversation(id).catch(showError);
});
function openNavigation(){
  if(matchMedia('(min-width:768px)').matches){setNavigationCollapsed(false);return;}
  $('#mobileSidebarSlot').append($('#sidebar'));
  if(!$('#navigationDialog').open)$('#navigationDialog').showModal();
  $('#openNavigation').setAttribute('aria-expanded','true');
  $('#closeNavigation').focus();
}
function setNavigationCollapsed(collapsed){
  $('#appFrame').dataset.collapsed=String(collapsed);
  $('#collapseNavigation').setAttribute('aria-expanded',String(!collapsed));
  $('#collapseNavigation').setAttribute('aria-label',collapsed?'展开导航与对话列表':'折叠导航');
  $('#collapseNavigation').title=collapsed?'展开导航':'折叠导航';
}
function applyNavigationPreference(){
  const preference=localStorage.getItem('lab-navigation-collapsed');
  setNavigationCollapsed(preference===null?matchMedia('(max-width:1023px)').matches:preference==='true');
}
$('#collapseNavigation').addEventListener('click',()=>{
  const collapsed=$('#appFrame').dataset.collapsed!=='true';
  localStorage.setItem('lab-navigation-collapsed',String(collapsed));setNavigationCollapsed(collapsed);
});
$('#openNavigation').addEventListener('click',openNavigation);
$('#conversationPicker').addEventListener('click',openNavigation);
$('#closeNavigation').addEventListener('click',()=>$('#navigationDialog').close());
$('#navigationDialog').addEventListener('close',()=>{sidebarHome.after($('#sidebar'));$('#openNavigation').setAttribute('aria-expanded','false');});
$('#navigationDialog').addEventListener('click',event=>{if(event.target===$('#navigationDialog'))$('#navigationDialog').close();});
matchMedia('(min-width:1024px)').addEventListener('change',applyNavigationPreference);
applyNavigationPreference();

window.renderConversationNavigation = function(rows=ui.conversations) {
  ui.conversations=rows;
  const query=$('#conversationSearch').value.trim().toLocaleLowerCase();
  const filtered=rows.filter(row=>(row.title||'新的对话').toLocaleLowerCase().includes(query));
  $('#conversationList').innerHTML=filtered.map(row=>`<button type="button" class="conversation-entry" data-conversation="${escapeHtml(row.id)}" title="${escapeHtml(row.title||'新的对话')}" ${row.id===state.chatConversationId?'aria-current="true"':''}><strong>${escapeHtml(row.title||'新的对话')}</strong><small>${escapeHtml(formatDate(row.updated_at))}</small></button>`).join('')||'<p class="muted small-text">没有匹配的对话</p>';
  const selected=rows.find(row=>row.id===state.chatConversationId);
  $('#conversationTitle').textContent=selected?.title||'新的对话';
};
$('#conversationSearch').addEventListener('input',()=>renderConversationNavigation());
$('#conversationList').addEventListener('click',async event=>{
  const button=event.target.closest('[data-conversation]');if(!button)return;
  try{await selectConversation(button.dataset.conversation);navigateTo('chat');window.updatePlanUI?.();}catch(error){showError(error);}
});

window.emptyChatHtml = ()=>'<div class="chat-empty" aria-hidden="true"></div>';
function composeMessage(text){
  navigateTo('chat');
  $('#chatText').value=text;$('#chatText').dispatchEvent(new Event('input',{bubbles:true}));$('#chatText').focus();
}
document.addEventListener('click',event=>{
  const prompt=event.target.closest('[data-compose]');if(prompt)composeMessage(prompt.dataset.compose);
  const open=event.target.closest('[data-open-review]');if(open)setReviewOpen(true);
  if(event.target.closest('[data-new-conversation]'))createSharedConversation().catch(showError);
  if(event.target.closest('[data-open-settings]')){if($('#navigationDialog').open)$('#navigationDialog').close();$('#settingsDialog').showModal();$('#deviceId').textContent=localStorage.getItem('lab-device')||'尚未生成';}
  const close=event.target.closest('[data-close-dialog]');if(close)close.closest('dialog').close();
});
$('#quickSearch').addEventListener('click',()=>{navigateTo('search');$('#searchInput').focus();});
$('#applyTimeFilter').addEventListener('click',()=>loadSearch().catch(showError));
document.addEventListener('keydown',event=>{
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')){event.preventDefault();createSharedConversation().catch(showError);}
});

function fitComposer(){
  const input=$('#chatText');input.style.height='auto';input.style.height=Math.min(180,input.scrollHeight)+'px';
}
$('#chatText').addEventListener('input',()=>{fitComposer();updatePlanUI(false);updateComposerState();});
window.fitComposer=fitComposer;
window.updateConversationPhase=function(phase){
  $('#conversationMain').dataset.phase=phase;
  $('#threadScroll').setAttribute('aria-busy',String(phase==='loading'));
  updateComposerState();fitComposer();
};
window.updateComposerState=function(){
  const busy=Boolean(state.chatSending);
  const voiceBusy=state.voicePhase&&state.voicePhase!=='idle';
  const hasContent=Boolean($('#chatText').value.trim()||$('#chatFile').files.length||localStorage.getItem('lab-chat-pending'));
  const button=$('#previewBtn');
  button.disabled=busy||voiceBusy||!hasContent;
  button.setAttribute('aria-busy',String(busy));
  const label=busy?($('#chatFile').files.length?'正在上传附件':'正在保存消息'):'发送消息';
  button.setAttribute('aria-label',label);button.title=label;
  $('#recordVoice').disabled=busy||Boolean(voiceBusy);
  $('#chatFile').disabled=busy;
  $('.attachment-button').setAttribute('aria-disabled',String(busy));
  document.querySelectorAll('#attachmentPreviews button,[data-new-conversation],#newConversation').forEach(control=>control.disabled=busy||Boolean(state.creatingConversation));
  const run=state.activeChatRun;
  $('#stopChatRun').hidden=!run;
  $('#stopChatRun').disabled=Boolean(run?.cancel_requested||state.cancellingChatRun===run?.run_id);
};
$('#stopChatRun').addEventListener('click',async()=>{
  const id=state.activeChatRun?.run_id;if(!id||state.cancellingChatRun)return;
  state.cancellingChatRun=id;updateComposerState();
  try{await chatApi(`/api/chat/runs/${id}/cancel`,{method:'POST'});await updateChatSnapshot();}
  catch(error){showError(error);}finally{state.cancellingChatRun=null;updateComposerState();}
});
window.updateTailControl=function(){
  const scroll=$('#threadScroll');
  $('#toLatestMessage').hidden=$('#conversationMain').dataset.phase!=='active'||scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<120;
};
$('#threadScroll').addEventListener('scroll',updateTailControl,{passive:true});
$('#toLatestMessage').addEventListener('click',()=>{$('#threadScroll').scrollTo({top:$('#threadScroll').scrollHeight,behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});});
new ResizeObserver(()=>{
  $('#conversationMain').style.setProperty('--composer-height',$('#composerArea').getBoundingClientRect().height+'px');
  updateTailControl();
}).observe($('#composerArea'));
$('#chatMessages').addEventListener('click',async event=>{
  const code=event.target.closest('[data-copy-code]');
  if(code){try{await navigator.clipboard.writeText(code.closest('.dsh-code-block').querySelector('code').textContent);toast('代码已复制');}catch{toast('无法访问剪贴板，请选择代码后复制');}return;}
  const button=event.target.closest('[data-copy-message]');if(!button)return;
  const message=state.chatLoadedMessages.find(entry=>String(entry.id)===button.dataset.copyMessage);
  if(!message)return;
  try{await navigator.clipboard.writeText(message.content||message.result?.reply||'');toast('回复已复制');}
  catch{toast('无法访问剪贴板，请选择回复文字后复制');}
});
$('#themePreference').value=localStorage.getItem('lab-theme')||'system';
$('#themePreference').addEventListener('change',event=>applyLabTheme(event.target.value));
function updateViewport(){
  const viewport=window.visualViewport;
  document.documentElement.style.setProperty('--app-height',(viewport?.height||window.innerHeight)+'px');
  const typing=document.activeElement?.matches('input,textarea,select');
  document.body.classList.toggle('keyboard-open',Boolean(typing&&window.innerHeight-(viewport?.height||window.innerHeight)>120));
  if(matchMedia('(min-width:768px)').matches&&$('#navigationDialog').open)$('#navigationDialog').close();
  placeReview();
}
window.visualViewport?.addEventListener('resize',updateViewport);
window.addEventListener('resize',updateViewport);
document.addEventListener('focusin',updateViewport);
document.addEventListener('focusout',()=>setTimeout(updateViewport,100));

function placeReview(){
  const panel=$('#planPanel');
  if(matchMedia('(min-width:1024px)').matches){
    if(planDialog.open){ui.ignoreNextPlanClose=true;planDialog.close();}
    if(panel.parentNode!==planHome.parentNode||panel.previousSibling!==planHome)planHome.after(panel);
    panel.hidden=!ui.reviewOpen;
  }else if(ui.reviewOpen){
    if(panel.parentNode!==planDialog)planDialog.append(panel);
    panel.hidden=false;if(!planDialog.open)planDialog.showModal();
  }else if(planDialog.open)planDialog.close();
  $('#chatLayout').classList.toggle('review-open',ui.reviewOpen);
}
function setReviewOpen(open){
  ui.reviewOpen=Boolean(open&&state.chatTaskId);placeReview();
  $('#taskBanner').setAttribute('aria-expanded',String(ui.reviewOpen));
  if(ui.reviewOpen)$('#planTitle').focus({preventScroll:true});
  else if($('#planPanel').contains(document.activeElement))$('#taskBanner').focus({preventScroll:true});
}
planDialog.addEventListener('close',()=>{if(ui.ignoreNextPlanClose){ui.ignoreNextPlanClose=false;return;}ui.reviewOpen=false;$('#planPanel').hidden=true;$('#chatLayout').classList.remove('review-open');$('#taskBanner').setAttribute('aria-expanded','false');if(!$('#taskBanner').hidden)$('#taskBanner').focus({preventScroll:true});});
planDialog.addEventListener('click',event=>{if(event.target===planDialog)setReviewOpen(false);});
$('#taskBanner').addEventListener('click',()=>setReviewOpen(true));
$('#closePlan').addEventListener('click',()=>setReviewOpen(false));
window.updatePlanUI=function(autoOpen=true){
  const disconnected=!navigator.onLine||state.chatConnected===false;
  $('#connectionNotice').hidden=!disconnected;
  $('#connectionNotice').textContent=!navigator.onLine?'设备离线，显示的是此前保存的内容。连接恢复并核对后再执行变更。':'正在重新连接，显示的是此前保存的内容。连接恢复后会重新核对计划。';
  const active=state.chatSnapshot?.tasks?.find(task=>task.id===state.chatTaskId);
  const visible=Boolean(state.chatTaskId&&state.chatPhase);
  $('#taskBanner').hidden=!visible;
  if(!visible){ui.previewKey=null;setReviewOpen(false);return;}
  const expired=active&&new Date(active.expires_at).valueOf()<=Date.now();
  const editing=Boolean($('#chatText').value.trim()||$('#chatFile').files.length);
  const offline=!navigator.onLine||state.chatConnected===false;
  const title=expired?'计划已过期，需要重新预览':state.chatPhase==='details'?'还需要核对部分信息':'有一项变更需要确认';
  $('#taskBannerTitle').textContent=title;
  $('#taskBannerDetail').textContent=editing?'有未发送的消息，发送后重新核对':state.chatPhase==='details'?'选择正确的物品和用量':'查看全部数量、位置与关联记录';
  $('#planTitle').textContent=state.chatPhase==='details'?'核对物品与用量':'核对本次变更';
  $('#planMeta').textContent=active?`${active.initiator||'教师'}发起 · 有效至 ${formatDate(active.expires_at)}`:'本次计划';
  $('#planStateNote').textContent=offline?'设备离线，重新连接后再确认。':expired?'计划已过期。请返回对话，要求按照当前库存重新预览。':editing?'您有未发送的消息，请先发送或清空，再核对新的预览。':'确认只执行本次显示的计划。';
  const hasCandidates=state.preview?.operations?.some(operation=>operation.candidates?.length);
  $('#commitBtn').disabled=Boolean(expired||editing||offline||state.chatConfirmInFlight||(state.chatPhase==='details'&&!hasCandidates));
  const key=state.chatTaskId+':'+state.chatPlanHash;
  if(autoOpen&&ui.previewKey!==key){ui.previewKey=key;if(ui.route==='chat'&&!document.querySelector('dialog[open]'))setReviewOpen(true);}
};
setInterval(()=>{if(state.chatTaskId)updatePlanUI(false);},15000);
window.addEventListener('offline',()=>updatePlanUI(false));
window.addEventListener('online',()=>updatePlanUI(false));
window.addEventListener('online',()=>{$('#statusLine').textContent='连接已恢复';});

window.ensureActorName=async function(){
  if($('#operatorName').value.trim())return $('#operatorName').value.trim();
  if(ui.actorWaiter)return ui.actorWaiter;
  ui.actorWaiter=new Promise(resolve=>{
    const dialog=$('#settingsDialog');
    const finish=()=>{resolve($('#operatorName').value.trim());ui.actorWaiter=null;};
    dialog.addEventListener('close',finish,{once:true});
    if(!dialog.open)dialog.showModal();$('#operatorName').focus();
  });
  return ui.actorWaiter;
};
function syncActor(){const actor=$('#operatorName').value.trim();$('#actorDisplay').textContent=actor||'登记姓名';$('#actorButton').setAttribute('aria-label',actor?`操作人：${actor}，查看使用设置`:'登记操作人姓名');}
$('#operatorName').addEventListener('input',syncActor);
$('#saveActor').addEventListener('click',()=>{
  if(!$('#operatorName').value.trim()){toast('请填写操作人姓名');$('#operatorName').focus();return;}
  localStorage.setItem('lab-actor',$('#operatorName').value.trim());syncActor();$('#settingsDialog').close();toast('操作人姓名已保存');
});
window.requestOperationNote=function(title){
  const dialog=$('#noteDialog');dialog.returnValue='cancel';$('#noteTitle').textContent=title;$('#operationNote').value='';
  return new Promise(resolve=>{dialog.addEventListener('close',()=>resolve(dialog.returnValue==='continue'?$('#operationNote').value.trim():null),{once:true});dialog.showModal();});
};
let installPrompt;
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;$('#installApp').hidden=false;});
$('#installApp').addEventListener('click',async()=>{await installPrompt?.prompt();installPrompt=null;$('#installApp').hidden=true;});

window.renderTaskOverview=function(){
  const data=window.opsData||{loans:[],schedules:[],purchases:[]};
  const tiles=[['loans','尚未归还',data.loans.filter(row=>Number(row.outstanding)>0).length],['lessons','已预留的实验',data.schedules.filter(row=>row.status==='reserved').length],['purchases','采购待处理',data.purchases.filter(row=>['draft','ordered','partial'].includes(row.status)).length]];
  $('#taskOverview').innerHTML=tiles.map(([tab,label,count])=>`<button class="task-stat tab" data-tab="${tab}" type="button"><span>${label}</span><strong>${count}</strong></button>`).join('');
};
async function loadSharedTasks(append=false){
  const response=await chatApi('/api/chat/tasks'+(append&&ui.nextTaskCursor?'?before='+ui.nextTaskCursor:''));
  ui.tasks=append?[...ui.tasks,...response.tasks]:response.tasks;ui.nextTaskCursor=response.next_before;
  $('#sharedTasks').innerHTML=ui.tasks.map(task=>{
    const expired=new Date(task.expires_at).valueOf()<=Date.now();
    return `<button class="shared-task" type="button" data-task-conversation="${escapeHtml(task.conversation_id)}" data-task-id="${escapeHtml(task.id)}"><span><strong>${escapeHtml(task.title||'业务变更计划')}</strong><small>${escapeHtml(task.initiator||'教师')} · ${escapeHtml(formatDate(task.created_at))}</small></span><span class="badge ${expired?'warn':''}">${expired?'需要重新预览':task.status==='awaiting_details'?'等待补充':'待确认'}</span></button>`;
  }).join('')||'<div class="empty-state"><i data-lucide="circle-check" aria-hidden="true"></i><p>目前没有待确认的聊天计划</p></div>';
  if(ui.nextTaskCursor)$('#sharedTasks').insertAdjacentHTML('beforeend','<button type="button" id="moreTasks">加载更多计划</button>');
  renderTaskOverview();window.lucide?.createIcons();
}
window.loadSharedTasks=loadSharedTasks;
$('#refreshTasks').addEventListener('click',()=>loadSharedTasks().catch(showError));
$('#sharedTasks').addEventListener('click',async event=>{
  if(event.target.closest('#moreTasks')){loadSharedTasks(true).catch(showError);return;}
  const button=event.target.closest('[data-task-conversation]');if(!button)return;
  try{state.preferredTaskId=button.dataset.taskId;await selectConversation(button.dataset.taskConversation);navigateTo('chat');setReviewOpen(true);}catch(error){showError(error);}
});

window.openItemDetails=function(id){
  const item=state.inventoryRows?.find(row=>row.id===Number(id));if(!item)return;
  $('#itemTitle').textContent=item.item_name;
  $('#itemDetails').innerHTML=`<div class="item-location"><span class="eyebrow">${escapeHtml(item.lab||'未填写实验室')}</span><strong>${escapeHtml(item.location_code||'未填写位置')}</strong><span class="muted small-text">${escapeHtml(item.spec||'规格未填写')} · ${escapeHtml(item.category||'未分类')}</span></div><div class="item-quantities">${[['available_quantity','当前可用'],['borrowed_quantity','已借出'],['reserved_quantity','已预留']].map(([key,label])=>`<div><span>${label}</span><strong>${formatQuantity(item[key],item.unit)}</strong></div>`).join('')}</div><p class="muted small-text">总拥有 ${formatQuantity(item.owned_quantity,item.unit)} · 待清洗 ${escapeHtml(item.cleaning)} · 维修 ${escapeHtml(item.repair)} · 待核对 ${escapeHtml(item.quarantine)}</p><label class="field-label" for="itemQuantity">本次操作数量（${escapeHtml(item.unit||'基础单位')}）</label><input id="itemQuantity" type="number" min="0" step="any" inputmode="decimal" placeholder="填写数量"><div class="toolbar">${[['inbound','登记入库'],['consume','登记消耗'],['borrow','登记借用']].map(([action,label])=>`<button type="button" data-item-action="${action}" data-item="${item.id}">${label}</button>`).join('')}</div><p class="muted small-text">信息将进入聊天输入框，核对后发送。业务变更仍需查看预览并确认。</p>`;
  $('#itemDialog').showModal();
};
$('#searchRows').addEventListener('click',event=>{const button=event.target.closest('[data-item-detail]');if(button)openItemDetails(button.dataset.itemDetail);});
$('#itemDetails').addEventListener('click',event=>{
  const button=event.target.closest('[data-item-action]');if(!button)return;
  const quantity=$('#itemQuantity').value;
  if(!quantity||!Number.isFinite(Number(quantity))||Number(quantity)<=0){toast('请填写大于零的有效数量');$('#itemQuantity').focus();return;}
  const item=state.inventoryRows.find(row=>row.id===Number(button.dataset.item));
  const action={inbound:'入库',consume:'实际消耗',borrow:'借用后归还'}[button.dataset.itemAction];
  $('#itemDialog').close();composeMessage(`为位于${item.lab}、${item.location_code}的${item.spec||''}${item.item_name}登记${action}，数量为 ${quantity} ${item.unit}。请生成变更预览。`);
});

const bootTab=new URLSearchParams(location.search).get('tab')||'chat';
navigateTo(bootTab,{replace:true});syncActor();updateViewport();fitComposer();updateComposerState();
window.initialRefresh.then(()=>{renderConversationNavigation(state.conversations||[]);renderTaskOverview();updatePlanUI();fitComposer();renderMessageRecovery();updateConversationPhase(state.chatLoadedMessages.length?'active':'hero');}).catch(showError);
