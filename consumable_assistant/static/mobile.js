/* Shared conversation controls and in-memory PCM16 voice input. */
let selectedImageUrls = [];
let attachmentSelection = [];
function setSelectedFiles(files) {
  let problem = state.chatSending ? '附件正在上传，请稍候' : '';
  if (localStorage.getItem('lab-chat-pending')) problem = '请先重试原消息，再修改附件';
  if (files.length > 8 || files.some(file => file.size > 20 * 1024 * 1024)) {
    problem = '每条消息最多八个附件，单个附件最多 20 MB';
  }
  if (problem) { files = attachmentSelection; toast(problem); }
  const transfer = new DataTransfer(); files.forEach(file => transfer.items.add(file));
  $('#chatFile').files = transfer.files; renderSelectedAttachments();
}
$('#chatFile').addEventListener('change', () => {
  const merged = [...attachmentSelection];
  for (const file of $('#chatFile').files) {
    if (!merged.some(old => old.name === file.name && old.size === file.size && old.lastModified === file.lastModified)) merged.push(file);
  }
  setSelectedFiles(merged);
}, {capture:true});
function renderSelectedAttachments() {
  selectedImageUrls.forEach(url => URL.revokeObjectURL(url)); selectedImageUrls = [];
  attachmentSelection = [...$('#chatFile').files];
  $('#chatAttachmentName').textContent = [...$('#chatFile').files].map(file => file.name).join('、');
  $('#attachmentPreviews').replaceChildren();
  for (const [index, file] of [...$('#chatFile').files].entries()) {
    const figure = document.createElement('figure');
    if (file.type.startsWith('image/')) {
      const image = document.createElement('img');
      const url = URL.createObjectURL(file); selectedImageUrls.push(url);
      image.src = url; image.alt = file.name; image.onerror = () => image.remove(); figure.append(image);
    } else { const symbol=document.createElement('i');symbol.dataset.lucide='file-spreadsheet';symbol.className='file-icon';symbol.setAttribute('aria-hidden','true');figure.append(symbol); }
    const caption = document.createElement('figcaption'); caption.textContent = `${file.name}（${Math.ceil(file.size / 1024)} KB）`;
    const remove=document.createElement('button');remove.type='button';remove.className='quiet icon-only';remove.setAttribute('aria-label',`移除附件${file.name}`);const icon=document.createElement('i');icon.dataset.lucide='x';icon.setAttribute('aria-hidden','true');remove.append(icon);remove.addEventListener('click',()=>setSelectedFiles(attachmentSelection.filter((_,i)=>i!==index)));
    figure.append(caption,remove); $('#attachmentPreviews').append(figure);
  }
  window.lucide?.createIcons();
  window.updateComposerState?.();
  window.updatePlanUI?.(false);
}
$('.attachment-button').addEventListener('keydown', event => { if(event.key==='Enter'||event.key===' '){event.preventDefault();$('#chatFile').click();} });
$('#composer').addEventListener('dragover',event=>{if([...event.dataTransfer.types].includes('Files'))event.preventDefault();});
$('#composer').addEventListener('drop',event=>{event.preventDefault();setSelectedFiles([...attachmentSelection,...event.dataTransfer.files]);});
$('#sharedConversation').addEventListener('change', event => selectConversation(event.target.value).catch(showError));
$('#newConversation').addEventListener('click', () => createSharedConversation().catch(showError));
$('#moreConversations').addEventListener('click', () => refreshConversationList(true).catch(showError));
$('#chatRunStatus').addEventListener('click', async event => {
  const button = event.target.closest('[data-cancel-run]');
  if (!button) return;
  button.disabled = true;
  try { await chatApi(`/api/chat/runs/${button.dataset.cancelRun}/cancel`, {method:'POST'}); await updateChatSnapshot(); }
  catch (error) { showError(error); button.disabled = false; }
});

let voiceCapture;
let voiceRequest;
let voiceGeneration = 0;
let voiceTimer;
const recordingTime=document.createElement('span');recordingTime.className='recording-time';recordingTime.setAttribute('aria-hidden','true');recordingTime.hidden=true;$('#stopVoice').before(recordingTime);
function voiceControls(phase, message) {
  clearInterval(voiceTimer);
  state.voicePhase = phase;
  $('#recordVoice').disabled = phase !== 'idle';
  $('#stopVoice').hidden = phase !== 'recording';
  $('#cancelVoice').hidden = phase === 'idle';
  $('#voiceStatus').textContent = message;
  $('#composer').classList.toggle('recording',phase==='recording');
  window.updateComposerState?.();
  $('#recordVoice').setAttribute('aria-pressed',String(phase==='recording'));
  recordingTime.hidden=phase!=='recording';
  if(phase==='recording'){
    const started=Date.now();recordingTime.textContent='00:00';
    voiceTimer=setInterval(()=>{const seconds=Math.floor((Date.now()-started)/1000);recordingTime.textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;},1000);
  }
}
async function releaseCapture() {
  const current = voiceCapture; voiceCapture = null;
  if (!current) return;
  clearTimeout(current.timer);
  current.processor.disconnect(); current.source.disconnect();
  current.stream.getTracks().forEach(track => track.stop());
  await current.context.close();
  return current;
}
async function pcmWave(chunks, sampleRate) {
  const samples = chunks.reduce((total, chunk) => total + chunk.length, 0);
  if (!samples) throw new Error('没有录制到声音');
  const frames = Math.ceil(samples * 16000 / sampleRate);
  const offline = new OfflineAudioContext(1, frames, 16000);
  const buffer = offline.createBuffer(1, samples, sampleRate);
  const data = buffer.getChannelData(0); let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length; }
  const source = offline.createBufferSource(); source.buffer = buffer; source.connect(offline.destination); source.start();
  const rendered = (await offline.startRendering()).getChannelData(0);
  const bytes = new ArrayBuffer(44 + rendered.length * 2); const view = new DataView(bytes);
  const ascii = (position, value) => [...value].forEach((char,index) => view.setUint8(position + index,char.charCodeAt(0)));
  ascii(0,'RIFF'); view.setUint32(4,bytes.byteLength - 8,true); ascii(8,'WAVE'); ascii(12,'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,16000,true); view.setUint32(28,32000,true); view.setUint16(32,2,true); view.setUint16(34,16,true);
  ascii(36,'data'); view.setUint32(40,rendered.length * 2,true);
  for (let index=0;index<rendered.length;index++) { const sample = Math.max(-1,Math.min(1,rendered[index])); view.setInt16(44 + index*2,Math.round(sample*(sample<0?32768:32767)),true); }
  return bytes;
}
$('#recordVoice').addEventListener('click', async () => {
  const generation = ++voiceGeneration;
  let stream;
  let context;
  try {
    if (!isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('录音需要可信 HTTPS 及麦克风权限，请先按照证书说明完成设置');
    voiceControls('requesting','正在请求麦克风权限');
    stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true},video:false});
    if (generation !== voiceGeneration) { stream.getTracks().forEach(track => track.stop()); return; }
    context = new AudioContext(); await context.resume();
    if (generation !== voiceGeneration) { stream.getTracks().forEach(track => track.stop()); await context.close(); return; }
    const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(4096,1,1);
    const chunks = []; processor.onaudioprocess = event => { chunks.push(new Float32Array(event.inputBuffer.getChannelData(0))); };
    source.connect(processor); processor.connect(context.destination);
    voiceCapture = {stream,context,sampleRate:context.sampleRate,source,processor,chunks,timer:setTimeout(() => $('#stopVoice').click(),119000)};
    voiceControls('recording','正在录音，最长两分钟。停止后会先显示可编辑文字。');
  } catch (error) { stream?.getTracks().forEach(track => track.stop()); await context?.close(); voiceControls('idle',error.message); }
});
$('#stopVoice').addEventListener('click', async () => {
  const generation = voiceGeneration;
  const current = await releaseCapture(); if (!current) return;
  voiceRequest = new AbortController();
  voiceControls('transcribing','正在识别，请稍候。识别文字不会自动发送。');
  try {
    const bytes = await pcmWave(current.chunks,current.sampleRate); current.chunks.length = 0;
    if (generation !== voiceGeneration) return;
    const response = await fetch('/api/voice/transcribe',{method:'POST',headers:{'Content-Type':'audio/wav'},body:bytes,signal:voiceRequest.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '语音识别失败');
    if (generation !== voiceGeneration) return;
    $('#chatText').value += ($('#chatText').value ? '\n' : '') + (data.text || '');
    $('#chatText').dispatchEvent(new Event('input')); $('#chatText').focus();
    voiceControls('idle','识别完成，请核对名称、规格、数量和单位后发送。');
  } catch (error) { if (generation === voiceGeneration) voiceControls('idle',error.name === 'AbortError' ? '已取消识别' : error.message); }
  finally { current.chunks.length = 0; if (generation === voiceGeneration) voiceRequest = null; }
});
$('#cancelVoice').addEventListener('click', async () => {
  ++voiceGeneration; voiceRequest?.abort(); voiceRequest = null;
  const current = await releaseCapture(); if (current) current.chunks.length = 0;
  voiceControls('idle','已取消，录音未保存。');
});
window.addEventListener('pagehide', () => { clearInterval(voiceTimer); ++voiceGeneration; voiceRequest?.abort(); void releaseCapture(); });
function markConnection() {
  if (!navigator.onLine) {
    $('#chatConnection').textContent = '设备离线，页面内容可能已变化';
    setStatus('设备离线，库存为此前查询结果');
    $('#commitBtn').disabled = true;
  }
}
window.addEventListener('offline',markConnection);
window.addEventListener('online',() => refreshAll());
markConnection();
