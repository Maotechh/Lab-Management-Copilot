const state = {
  preview: null,
  selectedItemIds: {},
  prepareRows: [],
  prepareOnlyMissing: false,
  prepareGroup: "",
  prepareSelectedRows: new Set(),
  locations: [],
  locationPreview: null,
  preparePreviewId: null,
  prepareActions: {},
  chatConversationId: new URLSearchParams(location.search).get('conversation'),
  chatToken: null,
  chatConversationPromise: null,
  chatTaskId: null,
  chatPhase: null,
  chatPreview: null,
  chatPlanHash: null,
  chatConfirmRequestId: null,
  chatLoadedConversationId: null,
  chatLoadedMessages: [],
};

const $ = (selector) => document.querySelector(selector);

function newRequestId(){
  if(crypto.randomUUID)return crypto.randomUUID();
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  return [...bytes].map(byte=>byte.toString(16).padStart(2,'0')).join('');
}

function shanghaiTime(value){
  const date=new Date(value+'+08:00');
  if(Number.isNaN(date.valueOf()))throw new Error('请填写有效的上海日期时间');
  return date.toISOString();
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const device = localStorage.getItem('lab-device') || newRequestId();
  localStorage.setItem('lab-device', device);
  headers.set('X-Device-ID', device);
  if (state.chatConversationId) headers.set('X-Conversation-ID', state.chatConversationId);
  const chatToken = localStorage.getItem('lab-chat-token');
  if (chatToken && path.startsWith('/api/chat/')) headers.set('X-Chat-Token', chatToken);
  const previewOnly = ['/api/transactions/preview','/api/prepare/preview','/api/prepare/export','/api/import/preview','/api/import/upload','/api/locations/preview','/api/ai/check'].includes(path);
  const writing = options.method && options.method !== 'GET' && !previewOnly;
  let requestId;
  const finishPending=()=>{
    if(JSON.parse(localStorage.getItem('lab-pending')||'null')?.id===requestId){
      localStorage.removeItem('lab-pending');
      const confirmTaskId=path.match(/^\/api\/chat\/tasks\/([^/]+)\/confirm$/)?.[1];
      if(confirmTaskId){localStorage.removeItem(`lab-chat-confirm-${confirmTaskId}`);if(state.chatTaskId===confirmTaskId)state.chatConfirmRequestId=null;}
      window.renderPending?.();
    }
  };
  if (writing) {
    let actor = $('#operatorName')?.value.trim() || localStorage.getItem('lab-actor') || '';
    if (!actor && window.ensureActorName) actor = await window.ensureActorName();
    if (!actor) throw new Error('请填写操作人姓名后再继续');
    localStorage.setItem('lab-actor', actor);
    const signature = options.body instanceof FormData
      ? JSON.stringify([...options.body.entries()].map(([key,value]) => [key, value instanceof File ? `${value.name}:${value.size}:${value.lastModified}` : value]))
      : String(options.body || '');
    const pending = JSON.parse(localStorage.getItem('lab-pending') || 'null');
    if (pending && (pending.path !== path || pending.signature !== signature)) throw new Error('有一项操作结果尚未确认，请先查询原请求结果');
    actor=pending?.actor||actor;
    headers.set('X-Actor', encodeURIComponent(actor));
    let suppliedRequestId = '';
    if (path.endsWith('/confirm') && options.body) {
      try { suppliedRequestId = JSON.parse(options.body).request_id || ''; } catch {}
    }
    requestId = pending?.id || suppliedRequestId || newRequestId();
    const formEntries=options.body instanceof FormData?[...options.body.entries()].map(([key,value])=>[key,value instanceof File?{file:true,name:value.name,size:value.size,lastModified:value.lastModified}:value]):null;
    localStorage.setItem('lab-pending',JSON.stringify({id:requestId,path,signature,body:options.body instanceof FormData ? null : options.body,formEntries,actor}));
    headers.set('X-Request-ID',requestId);
    window.renderPending?.();
  }
  let response;
  try { response = await fetch(path, {...options,headers}); }
  catch (error) { window.renderPending?.(); throw new Error('网络连接中断，输入已保留，请查询原请求结果后重试'); }
  if (!response.ok) {
    if (writing && response.status < 500) finishPending();
    let message = `${response.status} ${response.statusText}`;
    let refreshedPlan;
    try {
      const data = await response.json();
      message = data.detail || message;
      refreshedPlan = data.refreshed_plan;
    } catch {
      message = await response.text();
    }
    if (refreshedPlan) await loadChat();
    const error=new Error(message);error.status=response.status;error.refreshedPlan=refreshedPlan;throw error;
  }
  const contentType = response.headers.get("content-type") || "";
  const result=contentType.includes('application/json')?await response.json():response;
  if(writing)finishPending();
  if (result.status === 'pending_confirmation' && !path.startsWith('/api/chat/')) {
    const approved = await showPlanConfirmation(result);
    if (!approved) throw new Error('计划尚未执行，可在共享聊天中继续查看或取消');
    const key = `lab-chat-confirm-${result.task_id}`;
    const id = localStorage.getItem(key) || newRequestId();
    localStorage.setItem(key, id);
    const confirmed = await api(`/api/chat/tasks/${result.task_id}/confirm`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({request_id:id, plan_hash:result.plan_hash}),
    });
    await loadChat();
    return confirmed.result;
  }
  if (result.status === 'completed' && result.task_id && !path.startsWith('/api/chat/')) return result.result;
  return result;
}

async function chatApi(path, options = {}) {
  const headers = new Headers(options.headers || {});
  let device = localStorage.getItem('lab-device');
  if (!device) { device = newRequestId(); localStorage.setItem('lab-device', device); }
  headers.set('X-Device-ID', device);
  const token = localStorage.getItem('lab-chat-token');
  if (token && !path.endsWith('/api/chat/conversations')) headers.set('X-Chat-Token', token);
  const actor = $('#operatorName')?.value.trim() || localStorage.getItem('lab-actor') || '';
  if (actor) headers.set('X-Actor', encodeURIComponent(actor));
  let response;
  try { response = await fetch(path, {...options, headers}); }
  catch { throw new Error('网络连接中断，聊天草稿仍保存在此设备'); }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch {}
    const error = new Error(message); error.status = response.status; throw error;
  }
  const result = await response.json();
  return result;
}

async function ensureChatConversation() {
  if (state.chatConversationPromise) return state.chatConversationPromise;
  state.chatConversationPromise = ensureChatConversationOnce();
  try {
    return await state.chatConversationPromise;
  } finally {
    state.chatConversationPromise = null;
  }
}

let chatEvents;
let chatRefresh;
let chatSnapshotPromise;
let chatOlderCursor;
let chatListCursor;

async function refreshConversationList(append = false) {
  const listing = await chatApi('/api/chat/conversations' + (append && chatListCursor ? `?before=${chatListCursor}` : ''));
  const select = $('#sharedConversation');
  if (!append) { select.innerHTML = ''; state.conversations = []; }
  state.conversations ||= [];
  const seen = new Set(state.conversations.map(row => row.id));
  state.conversations.push(...(listing.conversations || []).filter(row => !seen.has(row.id)));
  for (const row of listing.conversations || []) {
    const option = document.createElement('option'); option.value = row.id;
    option.textContent = `${row.title || '实验室对话'} · ${formatDate(row.updated_at)}`;
    select.append(option);
  }
  select.value = state.chatConversationId || '';
  chatListCursor = listing.next_before;
  $('#moreConversations').hidden = !chatListCursor;
  window.renderConversationNavigation?.(state.conversations);
}

async function selectConversation(id) {
  if (localStorage.getItem('lab-chat-pending')) throw new Error('请先重试尚未确认结果的原消息，再切换对话');
  if ($('#chatFile').files.length) throw new Error('请先发送或移除附件，再切换对话');
  if (state.chatConversationId) localStorage.setItem('lab-chat-draft-' + state.chatConversationId, $('#chatText').value);
  chatEvents?.close(); chatEvents = null;
  state.chatConversationId = id;
  state.chatLoadedConversationId = null;
  state.chatLoadedMessages = [];
  state.renderedConversationId = null;
  chatOlderCursor = null;
  state.chatOlderLoadingConversationId = null;
  $('#olderMessages').disabled = false;
  $('#olderMessages').textContent = '查看更早的消息';
  localStorage.setItem('lab-chat-conversation', id);
  $('#chatText').value = localStorage.getItem('lab-chat-draft-' + id) || '';
  window.fitComposer?.();
  await updateChatSnapshot(id);
  await refreshConversationList();
}

async function createSharedConversation() {
  if (localStorage.getItem('lab-chat-pending') || $('#chatFile').files.length) throw new Error('请先发送或处理当前消息，再新建对话');
  const created = await chatApi('/api/chat/conversations', {method:'POST'});
  await selectConversation(created.conversation_id);
  window.navigateTo?.('chat');
}

async function updateChatSnapshot(id = state.chatConversationId) {
  if (!id) return;
  const snapshot = await chatApi(`/api/chat/conversations/${id}`);
  if (state.chatConversationId !== id) return;
  const sameConversation = state.chatLoadedConversationId === id;
  const previousMessages = sameConversation ? state.chatLoadedMessages : [];
  const previousSnapshot = sameConversation ? state.chatSnapshot : null;
  const newerSnapshot = previousSnapshot && Number(previousSnapshot.last_event_id || 0) > Number(snapshot.last_event_id || 0)
    ? previousSnapshot : snapshot;
  state.chatLoadedMessages = mergeChatMessages(previousMessages, snapshot.messages || []);
  state.chatLoadedConversationId = id;
  state.chatSnapshot = {...newerSnapshot, messages:state.chatLoadedMessages};
  const newestPageFirstId = Number(snapshot.messages?.[0]?.id || 0);
  const previouslyLoadedLastId = previousMessages.reduce((maximum, message) => Math.max(maximum, Number(message.id) || 0), 0);
  if (!sameConversation || snapshot.next_before === null) {
    chatOlderCursor = snapshot.next_before;
  } else if (snapshot.next_before && (!previousMessages.length || newestPageFirstId > previouslyLoadedLastId)) {
    chatOlderCursor = snapshot.next_before;
  }
  renderChatMessages(state.chatLoadedMessages);
  restoreChatTask(state.chatSnapshot.tasks || []);
  $('#olderMessages').hidden = !chatOlderCursor;
  renderChatRuns(state.chatSnapshot.runs || []);
  if (!chatEvents) {
    chatEvents = new EventSource(`/api/chat/conversations/${id}/events?after=${state.chatSnapshot.last_event_id || 0}`);
    const refresh = () => {
      clearTimeout(chatRefresh);
      chatRefresh = setTimeout(() => updateChatSnapshot(id).catch(showError), 100);
    };
    for (const kind of ['message','run','tool','task']) chatEvents.addEventListener(kind, refresh);
    chatEvents.onopen = () => { $('#chatConnection').textContent = '共享对话已连接'; updateChatSnapshot(id).catch(showError); };
    chatEvents.onerror = () => { $('#chatConnection').textContent = '正在重新连接，显示的是此前保存的内容'; };
  }
}

function mergeChatMessages(...pages) {
  const messages = new Map();
  for (const page of pages) {
    for (const message of page || []) messages.set(String(message.id), message);
  }
  return [...messages.values()].sort((left, right) => Number(left.id) - Number(right.id));
}

async function loadOlderMessages() {
  const id = state.chatConversationId;
  const cursor = chatOlderCursor;
  const button = $('#olderMessages');
  if (!id || !cursor || state.chatOlderLoadingConversationId === id) return;
  state.chatOlderLoadingConversationId = id;
  button.disabled = true;
  button.textContent = '正在加载…';
  try {
    const snapshot = await chatApi(`/api/chat/conversations/${id}?before=${encodeURIComponent(cursor)}`);
    if (state.chatConversationId !== id) return;
    state.chatLoadedMessages = mergeChatMessages(snapshot.messages || [], state.chatLoadedMessages || []);
    state.chatSnapshot = {...state.chatSnapshot, messages:state.chatLoadedMessages};
    chatOlderCursor = snapshot.next_before === cursor ? null : snapshot.next_before;
    renderChatMessages(state.chatLoadedMessages);
  } catch (error) {
    if (state.chatConversationId === id) showError(error);
  } finally {
    if (state.chatOlderLoadingConversationId === id) state.chatOlderLoadingConversationId = null;
    if (state.chatConversationId === id) {
      button.disabled = false;
      button.textContent = '查看更早的消息';
      button.hidden = !chatOlderCursor;
    }
  }
}

async function ensureChatConversationOnce() {
  if (!state.chatConversationId) state.chatConversationId = localStorage.getItem('lab-chat-conversation');
  if (!state.chatConversationId) {
    const listing = await chatApi('/api/chat/conversations');
    state.chatConversationId = listing.conversations?.[0]?.id;
    if (!state.chatConversationId) state.chatConversationId = (await chatApi('/api/chat/conversations', {method:'POST'})).conversation_id;
    localStorage.setItem('lab-chat-conversation', state.chatConversationId);
  }
  chatSnapshotPromise ||= updateChatSnapshot().finally(() => { chatSnapshotPromise = null; });
  await chatSnapshotPromise;
  await refreshConversationList();
}

function renderChatRuns(runs) {
  const labels = {queued:'排队中',running:'正在处理',failed:'处理失败',cancelled:'已取消',completed:'已完成'};
  $('#chatRunStatus').innerHTML = runs.filter(run => ['queued','running','failed','cancelled'].includes(run.status)).slice(0,8).map(run =>
    `<div class="run-status">${escapeHtml(run.actor)}：${labels[run.status]}${run.error ? `，${escapeHtml(run.error)}` : ''}${['queued','running'].includes(run.status) ? `<button type="button" data-cancel-run="${escapeHtml(run.run_id)}">取消运行</button>` : ''}</div>`).join('');
}

async function showPlanConfirmation(result) {
  const dialog = $('#businessConfirmation');
  dialog.returnValue = 'cancel';
  $('#businessConfirmationBody').innerHTML = businessPreviewHtml(result.preview);
  $('#businessConfirmDetails').textContent = '确认将执行以上全部变更。任何老师均可确认，姓名为本人填写。计划有效期为三十分钟。';
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once:true});
    dialog.showModal();
  });
}

async function loadChat() {
  await ensureChatConversation();
  const config = await api('/api/config');
  const banner = $('#profileBanner');
  state.config = config;
  const notices = [];
  if (config.simulated) notices.push('当前为模拟数据环境。');
  if (!config.inventory_confirmed) notices.push('附件库存尚未经老师核对，请先核对实物。');
  if (!config.notifications_configured) notices.push('手机后台通知尚未配置，低库存情况请在系统页面中查看。');
  banner.hidden = notices.length === 0;
  banner.textContent = notices.join(' ');
}

function restoreChatTask(tasks) {
  const pending = tasks.filter(task => ['awaiting_details','pending_confirmation'].includes(task.status));
  const active = pending.find(task => task.id === state.preferredTaskId) || pending[0];
  if (!active) {
    state.renderedPreviewKey = null;
    state.chatTaskId = null; state.chatPhase = null; state.chatPreview = null;
    state.chatPlanHash = null;
    state.preview = null;
    $('#previewBox').innerHTML = ''; $('#commitBtn').disabled = true;
    $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>生成确认预览';
    renderChatTaskActions();
    window.updatePlanUI?.();
    return;
  }
  state.chatTaskId = active.id;
  state.chatConfirmRequestId = localStorage.getItem(`lab-chat-confirm-${active.id}`) || null;
  state.chatPhase = active.status === 'awaiting_details' ? 'details' : 'confirm';
  state.chatPreview = active.preview;
  state.chatPlanHash = active.plan_hash;
  const previewKey = active.id + ':' + active.plan_hash;
  if (state.renderedPreviewKey === previewKey && $('#previewBox').childElementCount) {
    window.updatePlanUI?.();
    return;
  }
  state.renderedPreviewKey = previewKey;
  if (state.chatPhase === 'details' && active.preview?.operations) {
    state.preview = active.preview;
    state.selectedItemIds = {};
    renderPreview(active.preview);
    $('#commitBtn').disabled = !active.preview.operations.some(op => op.candidates?.length);
    $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>生成确认预览';
  } else {
    renderBusinessPreview(active.preview);
    $('#commitBtn').disabled = false;
    $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>确认执行';
  }
  renderChatTaskActions();
  window.updatePlanUI?.();
  window.lucide?.createIcons();
}

function chatMessageContent(message) {
  const result = message.result || {};
  const detail = result.tool_results?.map(entry => renderToolResult(entry)).join('') || '';
  const completed = result.status === 'completed' ? renderCompletedResult(result.result) : '';
  const previous = result.previous_preview ? `<details class="chat-result"><summary>查看已失效的原预览，对比当前预览</summary>${businessPreviewHtml(result.previous_preview)}</details>` : '';
  const attachments = (result.attachments || []).map(file => `<span class="chat-file-chip"><i data-lucide="paperclip" class="icon-sm"></i>${escapeHtml(file.filename)}</span>`).join('');
  return `<div class="chat-message-role">${message.role === 'user' ? escapeHtml(message.actor || '老师') : '耗材助手'} · ${escapeHtml(formatDate(message.created_at))}</div><div class="chat-message-body">${escapeHtml(message.content || result.reply || '')}</div>${attachments}${detail}${completed}${previous}${result.status === 'pending_confirmation' ? '<div class="chat-result"><p>已生成变更预览，请核对计划中的数量、位置与关联记录。</p></div>' : ''}`;
}

const chatMessageRenderCache = new WeakMap();

function renderChatMessages(messages) {
  const scroll = $('#threadScroll');
  const container = $('#chatMessages');
  const sameConversation = state.renderedConversationId === state.chatConversationId;
  const nearEnd = !sameConversation || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
  const oldHeight = scroll.scrollHeight;
  const oldTop = scroll.scrollTop;
  const oldFirst = Number(container.querySelector('[data-message-id]')?.dataset.messageId || 0);
  const older = sameConversation && Number(messages[0]?.id || 0) < oldFirst;
  const existing = new Map([...container.querySelectorAll('[data-message-id]')].map(node => [node.dataset.messageId, node]));
  const wanted = new Set();
  const nodes = [];

  for (const message of messages) {
    const id = String(message.id);
    const signature = JSON.stringify(message);
    let node = existing.get(id);
    if (!node) {
      node = document.createElement('article');
      node.dataset.messageId = id;
      node.className = `chat-message ${message.role === 'user' ? 'user' : 'assistant'}`;
      node.innerHTML = chatMessageContent(message);
      chatMessageRenderCache.set(node, signature);
    } else if (chatMessageRenderCache.get(node) !== signature) {
      const expanded = [...node.querySelectorAll('details')].map(detail => detail.open);
      node.className = `chat-message ${message.role === 'user' ? 'user' : 'assistant'}`;
      node.innerHTML = chatMessageContent(message);
      [...node.querySelectorAll('details')].forEach((detail, index) => { detail.open = expanded[index] ?? false; });
      chatMessageRenderCache.set(node, signature);
    }
    wanted.add(id);
    nodes.push(node);
  }

  for (const [id, node] of existing) if (!wanted.has(id)) node.remove();
  if (!messages.length) {
    if (!container.querySelector('.chat-empty')) container.innerHTML = window.emptyChatHtml?.() || '<div class="chat-empty">请输入查询或操作内容。</div>';
  } else {
    container.querySelector('.chat-empty')?.remove();
    let next = null;
    for (let index = nodes.length - 1; index >= 0; index--) {
      const node = nodes[index];
      if (node.nextElementSibling !== next) container.insertBefore(node, next);
      next = node;
    }
  }

  state.renderedConversationId = state.chatConversationId;
  state.firstRenderedMessage = messages[0]?.id;
  requestAnimationFrame(() => {
    scroll.scrollTop = older ? oldTop + scroll.scrollHeight - oldHeight : nearEnd ? scroll.scrollHeight : oldTop;
  });
  window.lucide?.createIcons();
}

function renderToolResult(entry) {
  const data = entry.result || {};
  if (data.error) return `<div class="chat-result error">${escapeHtml(data.error)}</div>`;
  const rows = data.rows || [];
  if (data.type === 'inventory_summary') {
    return `<div class="chat-result"><strong>库存统计</strong><p>匹配耗材记录 ${escapeHtml(data.item_count)} 条，实验室 ${escapeHtml(data.lab_count)} 个，位置 ${escapeHtml(data.location_count)} 个，低于预警阈值 ${escapeHtml(data.low_stock_count)} 条。</p>${tableHtml(['单位','记录数','总拥有','可用','借出','预留','待清洗','维修','待核对'],(data.units || []).map(row=>[escapeHtml(row.unit || '未填写'),escapeHtml(row.item_count),...['owned_quantity','available_quantity','borrowed_quantity','reserved_quantity','cleaning','repair','quarantine'].map(key=>escapeHtml(row[key]))]))}<p class="muted">数量按单位分别统计，借出和预留已计入可用数量计算。</p></div>`;
  }
  if (data.type === 'inventory' || data.type === 'alerts') {
    return `<div class="chat-result"><strong>${data.type === 'alerts' ? '库存预警' : '物品位置与库存'}</strong>${tableHtml(['耗材','规格','实验室','位置','可用','总拥有'], rows.slice(0,30).map(row=>[escapeHtml(row.item_name),escapeHtml(row.spec || '未填写'),escapeHtml(row.lab),escapeHtml(row.location_code),formatQuantity(row.available_quantity,row.unit),formatQuantity(row.owned_quantity,row.unit)]))}</div>`;
  }
  if (data.type === 'history') {
    return `<div class="chat-result"><strong>操作记录</strong>${tableHtml(['时间','耗材','动作','数量','操作人'],rows.slice(0,30).map(row=>[formatDate(row.created_at),escapeHtml(row.item_name),escapeHtml(row.action_label),formatDelta(row.delta_quantity,row.unit),escapeHtml(row.actor)]))}</div>`;
  }
  if (data.type === 'preparation') {
    return `<div class="chat-result"><strong>实验准备清单</strong>${tableHtml(['项目','规格','需求','可用','还需采购','位置','使用方式'],rows.slice(0,100).map(row=>[escapeHtml(row.name),escapeHtml(row.spec || '需确认'),formatQuantity(row.required_quantity,row.unit),formatQuantity(row.available_quantity,row.unit),formatQuantity(row.purchase_quantity,row.purchase_unit),escapeHtml((row.locations||[]).map(item=>`${item.lab} ${item.location_code}`).join('、') || '未匹配'),escapeHtml(row.action || '待确认')]))}</div>`;
  }
  if (data.type === 'inventory_import') {
    return `<div class="chat-result"><strong>库存导入预览</strong><p>新增或匹配 ${data.rows?.length || 0} 行，错误 ${data.errors?.length || 0} 行${data.duplicate_file ? '，检测到重复文件' : ''}。</p>${tableHtml(['行','耗材','单位','数量','重复'],(data.rows||[]).slice(0,30).map(row=>[row.row_number,escapeHtml(row.item_name),escapeHtml(row.unit),escapeHtml(row.quantity),row.duplicate_row?'是':'否']))}</div>`;
  }
  if (data.type === 'export') return `<div class="chat-result"><a class="button secondary" href="${escapeHtml(data.url)}"><i data-lucide="download" class="icon-sm"></i>${escapeHtml(data.filename || '下载结果')}</a></div>`;
  return `<div class="chat-result">${tableHtml(['记录','内容'],rows.slice(0,30).map(row=>[escapeHtml(row.id ?? row.name ?? ''),escapeHtml(row.item_name ?? row.name ?? row.status ?? '')]))}</div>`;
}

function tableHtml(headers, rows) {
  return `<div class="table-wrap responsive-cards"><table><thead><tr>${headers.map(value=>`<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((cell,index)=>`<td data-label="${escapeHtml(headers[index])}">${cell}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${headers.length}">没有匹配记录</td></tr>`}</tbody></table></div>`;
}

function renderCompletedResult(result) {
  if (!result) return '';
  return `<div class="chat-result">${renderRecordChanges([{table:'operation_results',after:result}],true)}</div>`;
}

function businessPreviewHtml(preview) {
  const rows = preview?.changes || [];
  const changeTable = rows.length
    ? tableHtml(['耗材与规格','位置','当前可用','执行后可用','当前总量','执行后总量'],rows.map(row=>[escapeHtml(`${row.item_name} ${row.spec || ''}（${row.unit || ''}）`),escapeHtml(`${row.lab || ''} ${row.location_code || ''}`),escapeHtml(row.before?.available_quantity ?? '尚未建立'),escapeHtml(row.after?.available_quantity ?? '移除'),escapeHtml(row.before?.owned_quantity ?? '尚未建立'),escapeHtml(row.after?.owned_quantity ?? '移除')]))
    : '';
  const operation = preview?.operation ? renderBusinessOperation(preview.operation, rows) : '';
  const business = renderRecordChanges(preview?.record_changes || []);
  return `<section class="chat-result chat-confirm-preview"><h2>待确认操作</h2>${changeTable}${operation}${business}<p class="muted">确认时会重新检查库存版本。</p></section>`;
}

function renderRecordChanges(records, completed = false) {
  const tables = {inventory_items:'耗材资料',stock_balances:'库存与状态',loans:'借用记录',purchase_orders:'采购记录',schedules:'实验安排',reservations:'预留分配',lesson_templates:'实验模板',stock_counts:'清点记录',location_details:'位置资料',alert_states:'提醒状态',preparations:'实验准备任务',preparation_schedules:'准备与实验关联',preparation_purchases:'准备与采购关联',preparation_revisions:'准备更正记录',stock_events:'业务流水',inventory_transactions:'原始库存流水',source_rows:'附件来源',item_aliases:'耗材别名',import_jobs:'导入记录',upload_previews:'清单核对记录',notification_events:'通知事件'};
  const labels = {id:'记录编号',item_id:'耗材编号',item_name:'耗材名称',spec:'规格',brand:'品牌',category:'类别',lab:'实验室',location_code:'位置',unit:'单位',quantity:'数量',threshold:'预警数量',ready:'可用状态数量',cleaning:'待清洗数量',repair:'维修数量',quarantine:'待核对数量',active:'是否使用',usage_type:'使用方式',conversions:'单位换算',review_reason:'待核对原因',original_quantity:'原始数量',outstanding:'尚未归还数量',borrower:'借用人',purpose:'用途',destination:'存放地点',due_at:'预计归还时间',actor:'操作人',device_id:'设备编号',schedule_id:'实验安排编号',preparation_id:'准备任务编号',order_id:'采购编号',received:'已到货数量',expected_at:'预计到货时间',status:'状态',note:'说明',name:'名称',course:'课程',venue:'地点',starts_at:'开始时间',ends_at:'结束时间',people:'人数',group_size:'每组人数',lines:'耗材项目',action:'操作',description:'位置说明',photo_path:'位置照片',before_quantity:'原有数量',actual_quantity:'实际数量',reason:'原因',event_id:'业务流水编号',acknowledged:'已知悉',snooze_until:'暂缓至',is_low:'数量不足',last_reminded_at:'上次提醒时间',changes:'数量变化',related_id:'关联编号',reversed_by:'撤销记录',created_at:'记录时间',kind:'事件类型',remark:'备注',source_file:'来源文件',source_sheet:'来源工作表',source_row_number:'来源行号',alias:'别名',filename:'文件名',mode:'导入方式',committed:'已执行',rows:'清单条目',errors:'清单核对要求'};
  Object.assign(labels,{results:'各项操作结果',transactions:'库存操作记录',item:'耗材当前记录',type:'记录类型',available_quantity:'当前可用数量',owned_quantity:'当前总拥有数量',ready_quantity:'可用状态数量',borrowed_quantity:'已借出数量',reserved_quantity:'已预留数量',required_quantity:'需求数量',unmet_quantity:'尚未满足数量',purchase_draft_id:'采购草案编号',purchase_draft_ids:'采购草案编号',purchases:'关联采购记录',schedules:'关联实验安排',locations:'各位置分配',calculation_unit:'计算单位',per_person:'每人数量',per_group:'每组数量',fixed:'固定数量',spare:'备用数量',delta_quantity:'数量变化',event:'业务流水',operation:'操作内容',target:'目标位置',source:'来源位置',retired:'是否已被替代',selected_rows:'已处理清单行',skipped:'未处理条目',outcomes:'实际处理结果'});
  const hidden = new Set(['ok','item_key','normalized_name','normalized_alias','version','updated_at','snapshot','raw_json','content_hash','file_hash','row_hash','request_id']);
  const values = {draft:'采购草案，尚未订购',ordered:'已订购',partial:'部分到货',received:'已到货',cancelled:'已取消',reserved:'已预留',awaiting_procurement:'已有预留，仍需采购',completed:'已完成',ready:'可用',cleaning:'待清洗',repair:'维修',quarantine:'待核对',damaged:'破损',lost:'遗失',consume:'消耗',inbound:'入库',borrow:'借用',return:'归还',preparation:'实验准备任务',consumable:'消耗品',reusable:'可重复使用',unclassified:'尚未分类'};
  const display = value => {
    if (value == null) return '未填写';
    if (typeof value === 'string' && values[value]) return escapeHtml(values[value]);
    if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}[T ]/.test(value)) return escapeHtml(formatDate(value));
    if (typeof value === 'string' && /^[{[]/.test(value)) { try { return display(JSON.parse(value)); } catch { return escapeHtml(value); } }
    if (Array.isArray(value)) return `<ol>${value.map(item=>`<li>${display(item)}</li>`).join('')}</ol>`;
    if (typeof value === 'object') return `<dl>${Object.entries(value).filter(([key])=>!hidden.has(key)).map(([key,item])=>`<div><dt>${escapeHtml(labels[key] || key)}</dt><dd>${display(item)}</dd></div>`).join('')}</dl>`;
    return escapeHtml(value);
  };
  return records.map(record=>{
    const keys = [...new Set([...Object.keys(record.before || {}),...Object.keys(record.after || {})])].filter(key=>!hidden.has(key) && (!record.before || !record.after || JSON.stringify(record.before[key]) !== JSON.stringify(record.after[key])));
    if (completed) return `<details class="chat-operation-details"><summary>实际操作结果</summary>${tableHtml(['资料','实际结果'],keys.map(key=>[escapeHtml(labels[key] || key),display(record.after[key])]))}</details>`;
    return `<details class="chat-operation-details"><summary>${escapeHtml(tables[record.table] || record.table)}：${record.before ? record.after ? '变更' : '移除' : '新增'}</summary>${tableHtml(['资料','变更前','变更后'],keys.map(key=>[escapeHtml(labels[key] || key),record.before ? display(record.before[key]) : '尚未建立',record.after ? display(record.after[key]) : '移除']))}</details>`;
  }).join('');
}

function renderBusinessOperation(operation, changes) {
  const labels = {mixed:'组合业务计划',preparation:'完整实验准备',preparation_update:'实验准备变更',stock:'库存变更',bulk:'批量库存变更',purchase:'采购操作',schedule:'实验安排',execute_schedule:'执行实验安排',cancel_schedule:'取消实验安排',template:'实验模板',count:'库存清点',state:'物品状态',metadata:'物品资料',create_item:'新增耗材',transfer:'位置转移',archive_location:'停用位置',location_details:'位置资料',alert:'提醒设置',import:'表格导入',prepare:'准备清单执行',prepare_remap:'清单匹配',undo:'撤销库存记录'};
  const names = {item_ids:'各位置耗材编号',source:'原始清单来源',preparation_id:'准备任务编号',payload:'操作内容',kind:'操作类型',item_id:'耗材编号',item_name:'耗材名称',spec:'规格',lab:'实验室',location_code:'位置',quantity:'数量',unit:'单位',action:'操作',owner:'负责人',expected_at:'预计到货时间',order_id:'采购记录',schedule_id:'实验安排',template_id:'实验模板',name:'名称',course:'课程',venue:'地点',starts_at:'开始时间',ends_at:'结束时间',people:'人数',group_size:'每组人数',borrower:'借用人',purpose:'用途',destination:'存放地点',due_at:'预计归还时间',loan_id:'借用记录',return_state:'归还状态',from_state:'当前状态',to_state:'目标状态',note:'原因',threshold:'预警数量',target_lab:'目标实验室',target_location:'目标位置',state:'库存状态',description:'位置说明',preview_id:'表格预览编号',selected_rows:'清单行号',allow_duplicate:'允许重复导入',event_id:'库存记录',row_number:'清单行号',lines:'耗材项目',operations:'操作项目',per_person:'每人数量',per_group:'每组数量',fixed:'固定数量',spare:'备用数量'};
  const values = {draft:'采购草案，尚未订购',place:'登记订购',replenish:'补充预留',inbound:'入库',consume:'消耗',borrow:'借出',return:'归还',adjust:'修正',count:'清点',threshold:'设置预警',order:'下单',receive:'登记到货',cancel:'取消',ready:'可用',cleaning:'待清洗',repair:'维修中',quarantine:'待核对',damaged:'破损',lost:'遗失'};
  const display = value => {
    if (Array.isArray(value)) return `<ol>${value.map(item => `<li>${display(item)}</li>`).join('')}</ol>`;
    if (value && typeof value === 'object') return `<dl>${Object.entries(value).filter(([key]) => !['version','item_versions'].includes(key)).map(([key,item]) => `<div><dt>${escapeHtml(names[key] || key)}</dt><dd>${display(item)}</dd></div>`).join('')}</dl>`;
    if (typeof value === 'string' && /^(\d{4}-\d{2}-\d{2})T/.test(value)) return escapeHtml(formatDate(value));
    return escapeHtml(typeof value === 'string' && values[value] ? values[value] : value);
  };
  const fields = Object.entries(operation.details || {}).filter(([key,value]) => !['version','item_versions'].includes(key) && value != null);
  const valuesFor = (key,value) => `<div><dt>${escapeHtml(names[key] || key)}</dt><dd>${display(value)}</dd></div>`;
  const itemName = fields.find(([key])=>key==='item_id')?.[1];
  const related = changes.find(item=>String(item.item_id)===String(itemName))?.item_name;
  return `<details class="chat-operation-details" open><summary>${escapeHtml(labels[operation.kind] || '操作内容')}</summary>${related?`<p>${escapeHtml(related)}</p>`:''}<dl>${fields.filter(([key])=>key!=='item_id').map(([key,value])=>valuesFor(key,value)).join('')}</dl></details>`;
}

function renderBusinessPreview(preview) {
  $('#previewBox').innerHTML = businessPreviewHtml(preview);
}

function renderChatTaskActions() {
  const root = $('#chatTaskActions');
  const visible = Boolean(state.chatTaskId && state.chatPhase);
  root.hidden = !visible;
  root.innerHTML = visible
    ? '<button id="cancelChatTaskBtn" type="button" class="secondary"><i data-lucide="x" class="icon-sm"></i>取消当前计划</button>'
    : '';
  window.lucide?.createIcons();
}

async function cancelChatTask() {
  if (!state.chatTaskId) return;
  try {
    await chatApi(`/api/chat/tasks/${state.chatTaskId}/cancel`, {
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({plan_hash:state.chatPlanHash}),
    });
    localStorage.removeItem(`lab-chat-confirm-${state.chatTaskId}`);
    state.chatTaskId = null; state.chatPhase = null; state.chatPreview = null; state.chatPlanHash = null; state.preview = null;
    $('#previewBox').innerHTML = ''; $('#commitBtn').disabled = true;
    $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>生成确认预览';
    renderChatTaskActions();
    await loadChat();
    toast('计划已取消，没有修改库存');
  } catch (error) { showError(error); }
}

async function sendChatMessage() {
  if (state.chatSending) return;
  const text = $('#chatText').value.trim();
  const files = [...$('#chatFile').files];
  if (!text && !files.length && !localStorage.getItem('lab-chat-pending')) { toast('请输入消息或附加文件'); return; }
  if (files.length > 8 || files.some(file => file.size > 20 * 1024 * 1024)) { showError(new Error('每条消息最多八个附件，单个附件最多 20 MB')); return; }
  state.chatSending = true;
  $('#previewBtn').disabled = true;
  $('#previewBtn').textContent = files.length ? '上传中…' : '发送中…';
  try {
    await ensureChatConversation();
    let pending = JSON.parse(localStorage.getItem('lab-chat-pending') || 'null');
    const meta = files.map(file => ({name:file.name,size:file.size,lastModified:file.lastModified}));
    if (pending && (pending.text !== text || (pending.conversation_id && pending.conversation_id !== state.chatConversationId))) throw new Error('请先恢复原消息内容并重试，不能使用同一消息编号发送不同内容');
    if (pending && files.length && JSON.stringify(meta) !== JSON.stringify(pending.files)) throw new Error('请重新选择原来的附件');
    pending ||= {conversation_id:state.chatConversationId,message_id:newRequestId(),text,files:meta,attachments:[],task_id:state.chatTaskId,plan_hash:state.chatPlanHash};
    localStorage.setItem('lab-chat-pending',JSON.stringify(pending));
    for (let index=pending.attachments.length; index<pending.files.length; index++) {
      if (!files[index]) throw new Error('请重新选择尚未上传的原附件');
      const form = new FormData(); form.append('file',files[index]);
      const uploaded = await chatApi(`/api/chat/conversations/${state.chatConversationId}/attachments`,{method:'POST',body:form});
      pending.attachments.push(uploaded.id);
      localStorage.setItem('lab-chat-pending',JSON.stringify(pending));
    }
    await chatApi(`/api/chat/conversations/${state.chatConversationId}/messages`, {
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:pending.message_id,text:pending.text,attachments:pending.attachments,task_id:pending.task_id,plan_hash:pending.plan_hash}),
    });
    localStorage.removeItem('lab-chat-pending'); localStorage.removeItem('lab-chat-draft');
    localStorage.removeItem('lab-chat-draft-' + state.chatConversationId);
    if ($('#chatText').value.trim() === text) $('#chatText').value = '';
    if ($('#chatText').value) localStorage.setItem('lab-chat-draft-' + state.chatConversationId, $('#chatText').value);
    $('#chatFile').value = ''; renderSelectedAttachments();
    window.fitComposer?.();
    await updateChatSnapshot();
  } catch (error) { showError(error); }
  finally {
    state.chatSending = false;
    $('#previewBtn').disabled = Boolean(state.voicePhase && state.voicePhase !== 'idle');
    $('#previewBtn').innerHTML = '<span>发送</span><i data-lucide="arrow-up" aria-hidden="true"></i>';
    window.lucide?.createIcons();
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatQuantity(value, unit) {
  if (value === null || value === undefined || value === "") return "";
  const num = Number(value);
  const text = Number.isFinite(num) && Number.isInteger(num) ? String(num) : String(value);
  return `${text}${unit || ""}`;
}

function formatDelta(value, unit) {
  const num = Number(value || 0);
  const prefix = num > 0 ? "+" : "";
  return `${prefix}${formatQuantity(num, unit)}`;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => box.classList.remove("show"), 2600);
}

function setStatus(message) {
  $("#statusLine").textContent = message;
}

async function loadStats() {
  const data = await api("/api/stats");
  $("#metrics").innerHTML = `
    <div class="metric"><span>耗材条目</span><strong>${data.item_count}</strong></div>
    <div class="metric"><span>操作记录</span><strong>${data.transaction_count}</strong></div>
    <div class="metric"><span>库存预警</span><strong>${data.low_stock_count}</strong></div>
  `;
  setStatus(`已载入 ${data.item_count} 个库存条目`);
}

async function loadSearch() {
  const params = new URLSearchParams();
  params.set("q", $("#searchInput").value.trim());
  params.set("category", $("#categoryFilter").value);
  params.set("low_stock", $("#lowStockFilter").checked ? "true" : "false");
  const start=$('#searchStart').value,end=$('#searchEnd').value;
  if(start&&end){params.set('starts_at',shanghaiTime(start));params.set('ends_at',shanghaiTime(end));}
  const data = await api(`/api/items/search?${params.toString()}`);
  state.inventoryRows = data.rows;
  $("#searchRows").innerHTML = data.rows.map(renderInventoryRow).join("") || emptyRow(8, "没有匹配结果");
  renderSearchSummary(data);
  if (window.lucide) window.lucide.createIcons();
}

function renderSearchSummary(data) {
  const units = Object.entries(data.quantity_by_unit || {})
    .filter(([unit]) => unit)
    .map(([unit, quantity]) => `${formatQuantity(quantity, unit)}`)
    .join("、");
  const total = units || "数量单位不一致";
  $("#searchSummary").textContent = data.row_count
    ? `找到 ${data.location_count} 个位置，当前数量：${total}。结果同时显示实验室、位置、规格和预警。`
    : "没有匹配结果，请尝试名称、规格、实验室或位置编码。";
}

function renderInventoryRow(row) {
  const low = row.low_stock ? `<span class="badge danger">低于预警</span>` : `<span class="badge ok">正常</span>`;
  const spec = row.spec ? escapeHtml(row.spec) : `<span class="muted">无</span>`;
  return `
    <tr>
      <td data-label="类别">${escapeHtml(row.category)}</td>
      <td data-label="实验室">${escapeHtml(row.lab)}</td>
      <td data-label="耗材" class="name-cell"><button type="button" class="item-link" data-item-detail="${row.id}">${escapeHtml(row.item_name)}</button>${renderAliases(row.aliases)}</td>
      <td data-label="规格">${spec}</td>
      <td data-label="位置"><strong>${escapeHtml(row.location_code)}</strong></td>
      <td data-label="数量"><strong>可用 ${formatQuantity(row.available_quantity, row.unit)}</strong><div class="muted small-text">总量 ${formatQuantity(row.owned_quantity, row.unit)} · 借出 ${row.borrowed_quantity} · 预留 ${row.reserved_quantity}</div><div class="muted small-text">待清洗 ${row.cleaning} · 维修 ${row.repair} · 待核对 ${row.quarantine}</div>${row.review_reason ? `<span class="badge danger">${escapeHtml(row.review_reason)}</span>` : ''}</td>
      <td data-label="预警">
        <div class="inline-edit">
          <input class="number-input threshold-input" data-threshold-input="${row.id}" data-version="${row.version}" type="number" inputmode="decimal" aria-label="${escapeHtml(row.item_name)}的预警数量" min="0" step="any" value="${Number(row.threshold || 0)}" />
          <button class="secondary icon-only" data-threshold-id="${row.id}" title="保存预警" aria-label="保存${escapeHtml(row.item_name)}的预警数量"><i data-lucide="save" class="icon-sm"></i></button>
        </div>
      </td>
      <td data-label="最近状态">${low}${renderLatestTransaction(row.latest_transaction, row.unit)}</td>
    </tr>
  `;
}

function renderAliases(aliases) {
  if (!aliases || aliases.length === 0) return "";
  return `<div class="muted">${aliases.slice(0, 3).map(escapeHtml).join("、")}</div>`;
}

function emptyRow(colspan, text) {
  return `<tr><td colspan="${colspan}">
    <div class="empty-state">
      <i data-lucide="inbox"></i>
      <p>${escapeHtml(text)}</p>
    </div>
  </td></tr>`;
}

function renderLatestTransaction(txn, unit) {
  if (!txn) return `<div class="muted small-text">暂无变更记录</div>`;
  const delta = Number(txn.delta_quantity || 0);
  const deltaText = delta === 0 ? "数量不变" : formatDelta(delta, txn.unit || unit);
  return `<div class="muted small-text">${escapeHtml(txn.action_label)} ${escapeHtml(deltaText)}，余 ${formatQuantity(txn.quantity_after, txn.unit || unit)}</div>`;
}

async function loadTransactions() {
  const data = await api("/api/transactions?limit=60");
  $("#transactionRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td data-label="时间">${formatDate(row.created_at)}</td>
      <td data-label="动作">${escapeHtml(row.action_label)}</td>
      <td data-label="耗材">${escapeHtml(row.item_name)}<div class="muted">${escapeHtml(row.lab || "")} ${escapeHtml(row.location_code)}</div></td>
      <td data-label="数量">${formatDelta(row.delta_quantity, row.unit)}</td>
      <td data-label="结果">${formatQuantity(row.quantity_after, row.unit)}</td>
      <td data-label="操作">${renderUndoButton(row)}</td>
    </tr>
  `).join("") || emptyRow(6, "暂无操作记录");
  if (window.lucide) window.lucide.createIcons();
}

function renderUndoButton(row) {
  const canUndo = row.can_undo;
  if (!canUndo) return `<span class="muted small-text">不可撤销</span>`;
  const description = `撤销${row.action_label || "库存操作"}，${row.item_name || "耗材"}，${formatQuantity(Math.abs(Number(row.delta_quantity || 0)), row.unit)}`;
  return `<button class="secondary" data-undo-id="${row.id}" data-version="${row.version}" title="${escapeHtml(description)}" aria-label="${escapeHtml(description)}"><i data-lucide="rotate-ccw" class="icon-sm"></i>撤销</button>`;
}

function formatDate(value) {
  if (!value) return '';
  const iso = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value.replace(' ','T')+'Z';
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? escapeHtml(value) : escapeHtml(date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}));
}

async function loadAlerts() {
  const data = await api("/api/alerts");
  $("#alertRows").innerHTML = data.rows.map((row) => `
    <tr>
      <td data-label="类别">${escapeHtml(row.category)}</td>
      <td data-label="实验室">${escapeHtml(row.lab)}</td>
      <td data-label="耗材" class="name-cell"><strong>${escapeHtml(row.item_name)}</strong><div class="muted">${escapeHtml(row.spec || "")}</div></td>
      <td data-label="位置">${escapeHtml(row.location_code)}</td>
      <td data-label="当前数量">${formatQuantity(row.quantity, row.unit)}</td>
      <td data-label="预警数量">${formatQuantity(row.threshold, row.unit)}</td>
      <td data-label="还差多少"><span class="badge danger">${formatQuantity(row.missing_quantity, row.unit)}</span></td>
      <td data-label="修改预警">
        <input class="number-input threshold-input" data-threshold-input="${row.id}" data-version="${row.version}" type="number" inputmode="decimal" aria-label="${escapeHtml(row.item_name)}的预警数量" min="0" step="any" value="${Number(row.threshold || 0)}" />
        <button data-threshold-id="${row.id}" class="secondary">保存</button>
      </td>
    </tr>
  `).join("") || emptyRow(8, "暂无低库存条目");
  if (window.lucide) window.lucide.createIcons();
}

async function previewTransaction() {
  const text = $("#chatText").value.trim();
  if (!text) {
    toast("请输入库存变更内容");
    return;
  }
  $("#previewBtn").disabled = true;
  try {
    const data = await api("/api/transactions/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if ($('#chatText').value.trim() !== text) return;
    state.preview = data;
    state.previewInputs={};
    state.selectedItemIds = {};
    for (const op of data.operations) {
      if (op.candidates && op.candidates.length === 1) {
        state.selectedItemIds[op.id] = op.candidates[0].id;
      }
    }
    renderPreview(data);
    persistPreviews();
    if (window.lucide) window.lucide.createIcons();
  } catch (err) {
    toast(err.message || "解析失败");
    console.error(err);
  } finally {
    $("#previewBtn").disabled = false;
  }
}

function renderPreview(data) {
  const engineLabel = data.engine && data.engine !== 'heuristic'
    ? `${data.engine}${data.model_name ? ` · ${data.model_name}` : ''}${data.local_split_used ? ' + 规则校验' : ''}`
    : (data.local_split_used ? "本地多条规则" : "本地规则");
  const modelWarning = data.model_enabled && data.engine !== "deepseek-pro" && !data.local_split_used
    ? `<span class="badge warn">模型未返回可用结果，已使用本地规则</span>`
    : "";
  let html = `<div class="toolbar compact"><span class="badge">${escapeHtml(engineLabel)}</span>${modelWarning}</div>`;
  
  let totalCandidates = 0;

  for (const op of data.operations) {
    totalCandidates += op.candidates.length;
    const isInbound = op.action === "inbound";
    const candidatesHtml = op.candidates.map((item, index) => `
      <label class="candidate">
        <input class="candidate-select" type="${isInbound ? "checkbox" : "radio"}" name="candidate-${op.id}" value="${item.id}" ${state.selectedItemIds[op.id]===item.id ? "checked" : ""} />
        <span>
          <strong>${escapeHtml(item.item_name)}</strong>
          <span class="muted">${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${escapeHtml(item.spec || "")}</span>
          <span class="muted small-text">匹配 ${Math.round(Number(item.score || 0))} 分${item.low_stock ? "，低于预警" : ""}</span>
        </span>
        ${isInbound
          ? `<input class="candidate-quantity number-input" data-candidate-quantity="${op.id}-${item.id}" type="number" min="0" step="0.01" value="${index === 0 ? Number(op.quantity || 0) : 0}" aria-label="${escapeHtml(item.item_name)}入库数量" />`
          : `<span>${formatQuantity(item.quantity, item.unit)}</span>`}
      </label>
    `).join("");

    const confidenceLabel = op.confidence === null || op.confidence === undefined
      ? ""
      : `<span class="badge">${Math.round(Number(op.confidence) * 100)}%</span>`;

    html += `
      <div class="surface" style="margin-top: 16px; padding: 16px;">
        <div class="toolbar compact" style="margin-top: 0;">
          <span class="badge ${op.needs_review ? "warn" : "ok"}">${escapeHtml(op.action_label)}</span>
          ${confidenceLabel}
        </div>
        <p style="margin-bottom: 12px; margin-top: 8px;">${escapeHtml(op.message)}<br><small class="muted">${escapeHtml(op.raw_text)}</small></p>
        <div class="toolbar compact">
          <label class="field-label" style="margin-bottom: 0;">动作
            <select id="actionSelect-${op.id}" style="margin-left: 8px;">
              ${op.needs_review ? '<option value="">请选择动作</option>' : ""}
              ${actionOption("inbound", "入库", op.action)}
              ${actionOption("consume", "消耗", op.action)}
              ${actionOption("borrow", "借出", op.action)}
              ${actionOption("return", "归还", op.action)}
              ${actionOption("adjust", "修正", op.action)}
              ${actionOption("threshold", "预警设置", op.action)}
            </select>
          </label>
          <label class="field-label ${isInbound ? "inbound-total-label" : ""}" style="margin-bottom: 0;">${isInbound ? "各位置分别填写数量" : "数量"}
            <input id="quantityInput-${op.id}" class="number-input" type="number" min="0" step="0.01" value="${op.quantity ?? 0}" style="margin-left: 8px;" />
          </label>
        </div>
        <div class="candidate-list">${candidatesHtml || '<div class="muted" style="padding: 12px 0;">没有候选耗材</div>'}</div>
        ${op.action === 'borrow' ? `<div class="form-grid"><label>借用人<input id="borrower-${op.id}" required></label><label>用途<input id="purpose-${op.id}" required></label><label>目标位置<input id="destination-${op.id}" required></label><label>预计归还时间（上海时间）<input id="due-${op.id}" type="datetime-local" required></label></div>` : ''}
        ${op.action === 'return' ? `<div class="form-grid"><label>对应借用<select id="loan-${op.id}"><option value="">选择借用记录</option>${(window.opsData?.loans || []).filter(loan=>Number(loan.outstanding)>0).map(loan=>`<option value="${loan.id}">${escapeHtml(loan.borrower)} · ${escapeHtml(loan.item?.item_name)} · 未归还 ${loan.outstanding}</option>`).join('')}</select></label><label>归还状态<select id="return-state-${op.id}"><option value="">选择状态</option><option value="ready">可用</option><option value="cleaning">待清洗</option><option value="repair">维修</option><option value="quarantine">待核对</option><option value="damaged">破损</option><option value="lost">遗失</option></select></label></div>` : ''}
      </div>
    `;
  }

  $("#previewBox").innerHTML = html;
  for(const [id,saved] of Object.entries(state.previewInputs||{})){
    const input=document.getElementById(id);if(input){if(input.type==='checkbox'||input.type==='radio')input.checked=saved.checked;else input.value=saved.value;}
  }
  $("#commitBtn").disabled = totalCandidates === 0;

  for (const op of data.operations) {
    document.querySelectorAll(`input[name='candidate-${op.id}']`).forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) state.selectedItemIds[op.id] = Number(input.value);
        if (op.quantity_mode === "all_available" && input.checked) {
          const selected = op.candidates.find((item) => Number(item.id) === Number(input.value));
          const quantityInput = $(`#quantityInput-${op.id}`);
          if (selected && quantityInput) quantityInput.value = Number(selected.quantity || 0);
        }
      });
    });
    const actionSelect = $(`#actionSelect-${op.id}`);
    if (actionSelect) {
      actionSelect.addEventListener("change", () => {
        state.previewInputs={};
        $('#previewBox').querySelectorAll('input[id],select[id]').forEach(input=>state.previewInputs[input.id]={value:input.value,checked:input.checked});
        for (const entry of data.operations) {
          const input = $(`#quantityInput-${entry.id}`);
          if (input) entry.quantity = input.value;
        }
        op.action = actionSelect.value;
        op.action_label = { inbound: "入库", consume: "消耗", borrow: "借出", return: "归还", adjust: "修正", threshold: "预警设置" }[op.action] || "需要确认";
        op.needs_review = false;
        renderPreview(data);
        if (window.lucide) window.lucide.createIcons();
      });
    }
  }
}

function actionOption(value, label, active) {
  return `<option value="${value}" ${value === active ? "selected" : ""}>${label}</option>`;
}

async function commitTransaction() {
  if (state.chatConfirmInFlight) return;
  if (state.chatTaskId && state.chatPhase === 'confirm') {
    const confirmationKey = `lab-chat-confirm-${state.chatTaskId}`;
    const requestId = state.chatConfirmRequestId || localStorage.getItem(confirmationKey) || newRequestId();
    state.chatConfirmRequestId = requestId;
    localStorage.setItem(confirmationKey, requestId);
    try {
      state.chatConfirmInFlight = true;
      $('#commitBtn').disabled = true;
      await api(`/api/chat/tasks/${state.chatTaskId}/confirm`, {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({request_id:requestId,plan_hash:state.chatPlanHash}),
      });
      state.chatTaskId = null; state.chatPhase = null; state.chatPreview = null; state.chatPlanHash = null;
      state.chatConfirmRequestId = null; state.preview = null;
      localStorage.removeItem('lab-chat-draft'); localStorage.removeItem('lab-chat-draft-' + state.chatConversationId); $('#chatText').value = ''; window.fitComposer?.();
      $('#previewBox').innerHTML = ''; $('#commitBtn').disabled = true;
      $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>生成确认预览';
      renderChatTaskActions();
      await Promise.all([loadChat(),refreshAll()]);
      toast('已完成确认的操作');
      return;
    } catch (error) {
      if (error.status && error.status < 500) {
        localStorage.removeItem(confirmationKey);
        state.chatConfirmRequestId = null;
      }
      showError(error);
      $('#commitBtn').disabled = error.refreshedPlan ? state.chatPhase !== 'confirm' : false;
      return;
    } finally {
      state.chatConfirmInFlight = false;
      window.updatePlanUI?.(false);
    }
  }
  if (!state.preview || !state.preview.operations) return;
  
  const payloadOperations = [];
  for (const op of state.preview.operations) {
    const action = $(`#actionSelect-${op.id}`).value;
    if (!action) {
      toast("请先选择库存动作");
      return;
    }
    if (action === "inbound") {
      const selectedInputs = [...document.querySelectorAll(`input[name='candidate-${op.id}']:checked`)];
      if (selectedInputs.length === 0) {
        toast("入库至少选择一个位置");
        return;
      }
      for (const input of selectedInputs) {
        const item = op.candidates.find((candidate) => Number(candidate.id) === Number(input.value));
        const quantityInput = document.querySelector(`[data-candidate-quantity='${op.id}-${input.value}']`);
        const quantity = Number(quantityInput?.value || 0);
        if (quantity <= 0) {
          toast("已选择的入库位置需要填写大于 0 的数量");
          return;
        }
        payloadOperations.push({
          item_id: Number(input.value),
          version: item?.version,
          action,
          quantity: quantityInput.value,
          unit: op.unit || item?.unit,
          note: op.raw_text,
        });
      }
      continue;
    }

    const selectedInput = document.querySelector(`input[name='candidate-${op.id}']:checked`);
    const selectedId = selectedInput ? Number(selectedInput.value) : state.selectedItemIds[op.id];
    if (!selectedId) continue;
    const quantity = Number($(`#quantityInput-${op.id}`).value || 0);
    if (["consume", "borrow", "return"].includes(action) && quantity <= 0) {
      toast("消耗、借出和归还的数量需要大于 0");
      return;
    }
    if (["adjust", "threshold"].includes(action) && quantity < 0) {
      toast("数量不能小于 0");
      return;
    }
    const item = op.candidates.find(candidate=>Number(candidate.id)===selectedId);
    const extra = {};
    if (action === 'borrow') {
      extra.borrower=$(`#borrower-${op.id}`)?.value; extra.purpose=$(`#purpose-${op.id}`)?.value; extra.destination=$(`#destination-${op.id}`)?.value;
      const due=$(`#due-${op.id}`)?.value;
      if (!extra.borrower || !extra.purpose || !extra.destination || !due) { toast('请完整填写借用信息'); return; }
      extra.due_at=shanghaiTime(due);
    }
    if (action === 'return') { extra.loan_id=Number($(`#loan-${op.id}`)?.value); extra.return_state=$(`#return-state-${op.id}`)?.value; }
    payloadOperations.push({ item_id: selectedId, version:item?.version, action, quantity:$(`#quantityInput-${op.id}`).value, unit: op.unit, note: op.raw_text, ...extra });
  }

  if (payloadOperations.length === 0) {
    toast("没有可提交的耗材操作");
    return;
  }

  if (state.chatTaskId && state.chatPhase === 'details') {
    try {
      $('#commitBtn').disabled = true;
      const result = await chatApi(`/api/chat/tasks/${state.chatTaskId}/prepare`, {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({operations:payloadOperations,plan_hash:state.chatPlanHash}),
      });
      state.chatPhase = 'confirm'; state.chatPreview = result.preview; state.chatPlanHash = result.plan_hash;
      renderBusinessPreview(result.preview);
      $('#commitBtn').disabled = false;
      $('#commitBtn').innerHTML = '<i data-lucide="check" class="icon-sm"></i>确认执行';
      renderChatTaskActions();
    window.lucide?.createIcons();
    window.renderMessageRecovery?.();
      await loadChat();
      return;
    } catch (error) {
      showError(error); $('#commitBtn').disabled = false; return;
    }
  }


  try {
    $("#commitBtn").disabled = true;
    await api("/api/transactions/commit_bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operations: payloadOperations }),
    });
    toast(`成功提交了 ${payloadOperations.length} 个耗材变更`);
    $("#chatText").value = "";
    localStorage.removeItem('lab-chat-draft');
    localStorage.removeItem('lab-chat-preview');
    $("#previewBox").innerHTML = "";
    state.preview = null;
    await refreshAll();
  } catch (err) {
    toast(err.message || "提交失败");
    $("#commitBtn").disabled = false;
  }
}

async function preparePreview() {
  const file = $("#prepareFile").files[0];
  if (!file) {
    toast("请选择 Excel 文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("course", $("#courseName").value.trim());
  $("#prepareBtn").disabled = true;
  try {
    const data = await api("/api/prepare/preview", { method: "POST", body: form });
    state.prepareRows = data.rows;
    state.preparePreviewId = data.preview_id;
    state.prepareActions = {};
    state.prepareSelectedRows = new Set();
    persistPreviews();
    $("#prepareSelectEnough").checked = false;
    const groups = data.groups || [];
    $("#prepareGroupFilter").innerHTML = `<option value="">全部课程实验</option>${groups.map((group) => {
      const value = `${group.course}||${group.experiment}`;
      return `<option value="${escapeHtml(value)}">${escapeHtml(group.course)} · ${escapeHtml(group.experiment)}</option>`;
    }).join("")}`;
    $("#exportPrepareBtn").disabled = data.rows.length === 0;
    renderPrepareRows(data.rows);
    if (window.lucide) window.lucide.createIcons();
  } catch (err) {
    toast(err.message || "清单解析失败");
  } finally {
    $("#prepareBtn").disabled = false;
  }
}

function renderPrepareRows(rows) {
  const groupRows = state.prepareGroup
    ? rows.filter((row) => `${row.course}||${row.experiment}` === state.prepareGroup)
    : rows;
  const sourceRows = state.prepareOnlyMissing
    ? groupRows.filter((row) => Number(row.missing_quantity || 0) > 0)
    : groupRows;
  const sortedRows = [...sourceRows].sort((a, b) => Number(b.missing_quantity || 0) - Number(a.missing_quantity || 0));
  renderPrepareSummary(rows);
  $("#prepareRows").innerHTML = sortedRows.map((row) => {
    const visibleLocations = (row.locations || []).slice(0, 4);
    const locations = visibleLocations.map((item) =>
      `${escapeHtml(item.lab || "")} ${escapeHtml(item.location_code || "")} ${formatQuantity(item.quantity, item.unit)}`
    ).join("<br>") + ((row.locations || []).length > 4 ? `<br><span class="muted">还有 ${(row.locations || []).length - 4} 个位置</span>` : "");
    const statusClass = row.status === "足够" ? "ok" : "danger";
    const enough = Number(row.missing_quantity || 0) <= 0;
    const selected = state.prepareSelectedRows.has(String(row.row_number));
    return `
      <tr>
        <td data-label="选择"><input class="prepare-select" type="checkbox" data-prepare-row="${escapeHtml(row.row_number)}" ${selected ? "checked" : ""} ${enough ? "" : "disabled"} /></td>
        <td data-label="课程/实验"><strong>${escapeHtml(row.course || "")}</strong><div class="muted">${escapeHtml(row.experiment || "")}</div></td>
        <td data-label="耗材" class="name-cell"><strong>${escapeHtml(row.name)}</strong></td>
        <td data-label="规格">${escapeHtml(row.spec || "")}</td>
        <td data-label="需求">${formatQuantity(row.required_quantity, row.unit)}</td>
        <td data-label="使用方式"><select data-prepare-action="${row.row_number}" aria-label="${escapeHtml(row.name)}使用方式"><option value="">请选择</option><option value="consume" ${(state.prepareActions[row.row_number]||row.action)==='consume'?'selected':''}>消耗</option><option value="borrow" ${(state.prepareActions[row.row_number]||row.action)==='borrow'?'selected':''}>借用</option></select></td>
        <td data-label="可用">${formatQuantity(row.available_quantity, row.unit)}</td>
        <td data-label="还需采购">${formatQuantity(row.missing_quantity, row.unit)}</td>
        <td data-label="建议采购">${formatQuantity(row.purchase_quantity, row.purchase_unit)}</td>
        <td data-label="位置">${locations || '<span class="muted">无匹配位置</span>'}${row.status==='需要确认匹配'||row.selected_item_id?`<select data-prepare-match="${row.row_number}" aria-label="${escapeHtml(row.name)}匹配物品"><option value="">选择具体物品</option>${(row.candidates||[]).map(item=>`<option value="${item.id}" ${item.id===row.selected_item_id?'selected':''}>${escapeHtml(item.item_name)} ${escapeHtml(item.spec)} · ${escapeHtml(item.lab)} ${escapeHtml(item.location_code)} · ${escapeHtml(item.unit)}</option>`).join('')}</select>`:''}</td>
        <td data-label="状态"><span class="badge ${statusClass}">${escapeHtml(row.status)}</span></td>
      </tr>
    `;
  }).join("") || emptyRow(11, state.prepareOnlyMissing ? "当前清单没有需要采购的耗材" : "暂无清单结果");
  updatePrepareCommitButton();
}

function renderPrepareSummary(rows) {
  const needRows = rows.filter((row) => Number(row.missing_quantity || 0) > 0);
  const locationCount = rows.reduce((sum, row) => sum + Number(row.match_count || 0), 0);
  $("#prepareSummary").innerHTML = rows.length
    ? `共 ${rows.length} 个条目，${new Set(rows.map((row) => `${row.course}||${row.experiment}`)).size} 个课程实验，${needRows.length} 个需要采购，匹配到 ${locationCount} 个库存位置。已选 ${state.prepareSelectedRows.size} 个库存足够的条目，可按课程实验批量扣减。`
    : "上传课程实验清单后显示各实验的库存数量和需要采购的条目。";
}

function updatePrepareCommitButton() {
  $("#consumePrepareBtn").disabled = state.prepareSelectedRows.size === 0;
}

async function consumeSelectedPrepareRows() {
  const selected = state.prepareRows.filter((row) => state.prepareSelectedRows.has(String(row.row_number)));
  if (!selected.length) {
    toast("请先选择库存足够的清单条目");
    return;
  }
  $("#consumePrepareBtn").disabled = true;
  try {
    const result = await api("/api/prepare/consume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_id:state.preparePreviewId,selected_rows:selected.map(row=>row.row_number),actions:state.prepareActions }),
    });
    toast(`已扣减 ${result.transaction_count} 条库存记录`);
    state.prepareSelectedRows = new Set();
    state.prepareRows=[];state.preparePreviewId=null;localStorage.removeItem('lab-prepare-preview');renderPrepareRows([]);
    await refreshAll();
  } catch (err) {
    toast(err.message || "库存扣减失败");
    updatePrepareCommitButton();
  }
}

async function exportPrepareRows() {
  const response = await api("/api/prepare/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows: state.prepareRows }),
  });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "实验准备核对.xlsx";
  link.click();
  URL.revokeObjectURL(url);
}

async function loadLocations() {
  const data = await api("/api/locations");
  state.locations = data.rows || [];
  const selected = $("#sourceLocationSelect").value;
  $("#sourceLocationSelect").innerHTML = `<option value="">选择原位置</option>${state.locations.map((location) => `
    <option value="${escapeHtml(JSON.stringify({ lab: location.lab, location_code: location.location_code }))}">
      ${escapeHtml(location.label)} · ${location.item_count} 项 · ${location.zero_count} 个为 0
    </option>
  `).join("")}`;
  if ([...$("#sourceLocationSelect").options].some((option) => option.value === selected)) {
    $("#sourceLocationSelect").value = selected;
  }
}

function getSelectedLocation() {
  const value = $("#sourceLocationSelect").value;
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

async function previewSelectedLocation() {
  const location = getSelectedLocation();
  if (!location) {
    toast("请先选择原位置");
    return;
  }
  try {
    const data = await api("/api/locations/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(location),
    });
    state.locationPreview = data;
    renderLocationPreview(data);
  } catch (err) {
    toast(err.message || "位置读取失败");
  }
}

function renderLocationPreview(data) {
  $("#locationSummary").textContent = data.row_count
    ? `${data.lab || "未填写实验室"} · ${data.location_code || "未填写位置"}：${data.row_count} 项，${data.zero_count} 个数量为 0。位置变化保留历史记录。`
    : "原位置没有耗材。";
  $("#locationRows").innerHTML = (data.rows || []).map((row) => `
    <tr>
      <td data-label="耗材"><strong>${escapeHtml(row.item_name)}</strong><div class="muted">${escapeHtml(row.category || "")}</div></td>
      <td data-label="规格">${escapeHtml(row.spec || "")}</td>
      <td data-label="数量">${formatQuantity(row.quantity, row.unit)}</td>
      <td data-label="预警">${formatQuantity(row.threshold, row.unit)}</td>
    </tr>
  `).join("") || emptyRow(4, "原位置没有耗材");
  const hasRows = Number(data.row_count || 0) > 0;
  $("#moveLocationBtn").disabled = !hasRows;
  $("#deleteLocationBtn").disabled = !hasRows;
}

async function moveSelectedLocation() {
  const source = getSelectedLocation();
  const targetLab = $("#targetLabInput").value.trim();
  const targetLocation = $("#targetLocationInput").value.trim();
  if (!source || !targetLab || !targetLocation) {
    toast("请填写原位置和目标实验室、目标位置");
    return;
  }
  try {
    const result = await api("/api/locations/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lab: source.lab,
        source_location: source.location_code,
        target_lab: targetLab,
        target_location: targetLocation,
        versions: state.locationPreview?.versions,
        confirmed: true,
      }),
    });
    toast(`已转移 ${result.moved_count} 项，合并 ${result.merged_count} 项，清理 ${result.deleted_zero_count} 个零数量条目`);
    state.locationPreview = null;
    const targetValue = JSON.stringify({ lab: targetLab, location_code: targetLocation });
    await loadLocations();
    if ([...$("#sourceLocationSelect").options].some((option) => option.value === targetValue)) {
      $("#sourceLocationSelect").value = targetValue;
      await previewSelectedLocation();
    } else {
      renderLocationPreview({ rows: [], row_count: 0, zero_count: 0 });
    }
    await refreshAll();
  } catch (err) {
    toast(err.message || "位置转移失败");
  }
}

async function deleteSelectedLocation() {
  const source = getSelectedLocation();
  if (!source || !state.locationPreview?.row_count) {
    toast("请先查看有耗材的原位置");
    return;
  }
  try {
    const result = await api("/api/locations/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lab: source.lab,
        source_location: source.location_code,
        confirmed: true,
        note: $('#locationNote')?.value || '位置停用',
      }),
    });
    toast(`已停用 ${result.archived_count} 项耗材，历史记录已保留`);
    state.locationPreview = null;
    await loadLocations();
    renderLocationPreview({ rows: [], row_count: 0, zero_count: 0 });
    await refreshAll();
  } catch (err) {
    toast(err.message || "位置删除失败");
  }
}

async function batchUploadInventory() {
  const file = $("#batchUploadFile").files[0];
  if (!file) {
    toast("请选择库存表格");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("mode", $("#batchUploadMode").value);
  form.append("category", $("#batchUploadCategory").value);
  $("#batchUploadBtn").disabled = true;
  try {
    const result = await api("/api/import/upload", { method: "POST", body: form });
    window.importPreview = result;
    $('#batchUploadLog').textContent = `${result.rows.length} 行，${result.errors.length} 行错误${result.duplicate_file ? '，这份表格已经导入过' : ''}\n` + result.rows.map(row=>`第 ${row.row_number} 行：${row.item_name}，原数量 ${row.before}，表格数量 ${row.quantity}${row.unit}，${row.item_id?'已有物品':'新增物品'}${row.duplicate_row?'，表内重复行将忽略':''}`).join('\n') + '\n' + result.errors.map(row=>`第 ${row.row_number} 行：${row.error}`).join('\n');
    $('#confirmImportBtn').disabled = !result.rows.length || !!result.errors.length;
    toast('库存表预览已生成，请核对后确认');
  } catch (err) {
    toast(err.message || "批量上传失败");
  } finally {
    $("#batchUploadBtn").disabled = false;
  }
}

async function seedData() {
  const data = await api("/api/import/seed", { method: "POST" });
  $("#adminLog").textContent = JSON.stringify(data, null, 2);
  toast("附件数据导入完成");
  await refreshAll();
}

async function saveThreshold(itemId, button) {
  const input = button?.closest("tr")?.querySelector(`[data-threshold-input="${itemId}"]`)
    || document.querySelector(`[data-threshold-input="${itemId}"]`);
  const value = Number(input?.value || 0);
  await api(`/api/items/${itemId}/threshold`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ threshold: value,version:Number(input?.dataset.version) }),
  });
  toast("预警数量已保存");
  await refreshAll();
}

async function undoTransaction(txnId) {
  const button = document.querySelector(`[data-undo-id="${txnId}"]`);
  await api(`/api/transactions/${txnId}/undo`, { method: "POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(button?.dataset.version)}) });
  toast("库存操作已撤销");
  await refreshAll();
}

function requestNotification() {
  return window.subscribeNotifications?.();
}

function bindEvents() {
  document.addEventListener('click', event => {
    const button = event.target.closest('.tab[data-tab]');
    if (button) window.navigateTo?.(button.dataset.tab);
  });
  $("#refreshAllBtn").addEventListener("click", refreshAll);
  $("#searchBtn").addEventListener("click", loadSearch);
  $("#searchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadSearch();
  });
  $("#categoryFilter").addEventListener("change", loadSearch);
  $("#lowStockFilter").addEventListener("change", loadSearch);
  $("#previewBtn").addEventListener("click", sendChatMessage);
  $("#olderMessages").addEventListener("click", loadOlderMessages);
  $("#commitBtn").addEventListener("click", commitTransaction);
  $("#prepareBtn").addEventListener("click", preparePreview);
  $("#exportPrepareBtn").addEventListener("click", exportPrepareRows);
  $("#prepareMissingOnly").addEventListener("change", (event) => {
    state.prepareOnlyMissing = event.target.checked;
    renderPrepareRows(state.prepareRows);
  });
  $("#prepareGroupFilter").addEventListener("change", (event) => {
    state.prepareGroup = event.target.value;
    renderPrepareRows(state.prepareRows);
  });
  $("#prepareSelectEnough").addEventListener("change", (event) => {
    if (event.target.checked) {
      state.prepareSelectedRows = new Set(
        state.prepareRows
          .filter((row) => Number(row.missing_quantity || 0) <= 0)
          .map((row) => String(row.row_number))
      );
    } else {
      state.prepareSelectedRows = new Set();
    }
    renderPrepareRows(state.prepareRows);
  });
  $("#consumePrepareBtn").addEventListener("click", consumeSelectedPrepareRows);
  $("#prepareRows").addEventListener("change", async (event) => {
    const match=event.target.closest('select[data-prepare-match]');
    if(match && match.value){
      try{
        const data=await api('/api/actions/prepare_remap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_id:state.preparePreviewId,row_number:Number(match.dataset.prepareMatch),item_id:Number(match.value)})});
        state.preparePreviewId=data.preview_id;state.prepareRows=data.rows;state.prepareSelectedRows.clear();renderPrepareRows(data.rows);persistPreviews();
      }catch(error){toast(error.message);}
      return;
    }
    const action = event.target.closest('select[data-prepare-action]');
    if (action) { state.prepareActions[action.dataset.prepareAction] = action.value; return; }
    const checkbox = event.target.closest("input[data-prepare-row]");
    if (!checkbox) return;
    const rowNumber = String(checkbox.dataset.prepareRow);
    if (checkbox.checked) state.prepareSelectedRows.add(rowNumber);
    else state.prepareSelectedRows.delete(rowNumber);
    updatePrepareCommitButton();
    renderPrepareSummary(state.prepareRows);
  });
  $('#chatText').addEventListener('input',()=>{
    const draftKey = state.chatConversationId ? 'lab-chat-draft-' + state.chatConversationId : 'lab-chat-draft';
    localStorage.setItem(draftKey, $('#chatText').value);
    if (!state.chatTaskId) { state.preview=null; state.previewInputs={}; $('#commitBtn').disabled=true; }
    window.updatePlanUI?.(false);
    localStorage.removeItem('lab-chat-preview');
  });
  $('#chatText').addEventListener('keydown',event=>{
    if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229&&matchMedia('(min-width:1024px) and (pointer:fine)').matches){event.preventDefault();sendChatMessage();}
  });
  $('#chatFile').addEventListener('change',()=>{
    renderSelectedAttachments();
    $('#chatAttachmentName').textContent=[...$('#chatFile').files].map(file=>file.name).join('、');
  });
  $('#chatTaskActions').addEventListener('click',event=>{
    if(event.target.closest('#cancelChatTaskBtn'))cancelChatTask();
  });
  $("#seedBtn").addEventListener("click", seedData);
  $("#batchUploadBtn").addEventListener("click", batchUploadInventory);
  $("#previewLocationBtn").addEventListener("click", previewSelectedLocation);
  $("#moveLocationBtn").addEventListener("click", moveSelectedLocation);
  $("#deleteLocationBtn").addEventListener("click", deleteSelectedLocation);
  $("#requestNotifyBtn").addEventListener("click", requestNotification);
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      $("#chatText").value = button.dataset.prompt;
      $('#chatText').dispatchEvent(new Event('input',{bubbles:true}));
      $("#chatText").focus();
    });
  });
  $("#searchRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-threshold-id]");
    if (target) saveThreshold(target.dataset.thresholdId, target);
  });
  $("#alertRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-threshold-id]");
    if (target) saveThreshold(target.dataset.thresholdId, target);
  });
  $("#transactionRows").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-undo-id]");
    if (target) undoTransaction(target.dataset.undoId);
  });
}

function persistPreviews(){
  if(state.preview)localStorage.setItem('lab-chat-preview',JSON.stringify({text:$('#chatText').value,data:state.preview}));
  if(state.preparePreviewId)localStorage.setItem('lab-prepare-preview',JSON.stringify({id:state.preparePreviewId,rows:state.prepareRows,actions:state.prepareActions}));
}
document.addEventListener('change',persistPreviews);
$('#prepareFile').addEventListener('change',()=>{state.prepareRows=[];state.preparePreviewId=null;state.prepareSelectedRows.clear();localStorage.removeItem('lab-prepare-preview');renderPrepareRows([]);});

async function refreshAll() {
  try {
    await Promise.all([loadStats(), loadSearch(), loadAlerts(), loadTransactions(), loadLocations(), loadChat()]);
    await window.refreshWorkflows?.();
    window.renderTaskOverview?.();
    if (document.body.dataset.page === 'tasks') await window.loadSharedTasks?.();
  } catch (error) {
    console.error(error);
    toast(error.message || "操作失败");
    setStatus("读取失败");
  }
}

bindEvents();
window.initialRefresh=refreshAll().then(() => {
  if (window.lucide) window.lucide.createIcons();
});
