'use strict';
const $ = id => document.getElementById(id);
const feed = $('feed-scroll');
const messages = $('messages');
const searchInput = $('search-input');
const state = {rows:[], before:null, after:null, older:false, newer:false, loading:false, epoch:0, target:null, query:'', results:[], total:0, more:false};
let searchRequest = null;
let searchTimer = null;
const dateFormatter = new Intl.DateTimeFormat(undefined,{year:'numeric',month:'long',day:'numeric'});
const shortDateFormatter = new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',day:'numeric'});
const timeFormatter = new Intl.DateTimeFormat(undefined,{hour:'numeric',minute:'2-digit'});

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function get(path, options={}) {
  const response = await fetch(path, {credentials:'same-origin', ...options});
  if (response.status === 401) { location.assign('/login'); throw new Error('Please sign in again.'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The archive could not be loaded. Please try again.');
  return data;
}

function linkedText(parent, text) {
  const regex = /https?:\/\/[^\s<>]+/g;
  let start = 0;
  for (const match of text.matchAll(regex)) {
    parent.append(document.createTextNode(text.slice(start,match.index)));
    const raw = match[0];
    const url = raw.replace(/[.,;!?\])}]+$/, '');
    const link = el('a', '', url);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    parent.append(link, document.createTextNode(raw.slice(url.length)));
    start = match.index + raw.length;
  }
  parent.append(document.createTextNode(text.slice(start)));
}

function initials(name) {
  if (name.startsWith('Member ·')) return name.slice(-2);
  return name.split(/\s+/).filter(Boolean).map(p=>p[0]).slice(0,2).join('').toUpperCase();
}

function mediaElement(media) {
  if (!media.available) return el('div','attachment missing-media',`${media.kind === 'image' ? 'Photo' : media.kind.charAt(0).toUpperCase()+media.kind.slice(1)} not included in this export`);
  if (media.kind === 'image') {
    const link = el('a','attachment');
    link.href = media.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.setAttribute('aria-label','Open attached photo at full size');
    const image = el('img','attachment-image');
    image.src = media.url;
    image.alt = 'Photo shared in the VX One technical forum';
    image.loading = 'lazy';
    image.decoding = 'async';
    link.append(image);
    return link;
  }
  if (media.kind === 'video' || media.kind === 'audio') {
    const wrapper = el('div','attachment');
    const player = el(media.kind, `attachment-${media.kind}`);
    player.src = media.url;
    player.controls = true;
    player.preload = 'none';
    if (media.kind === 'video') player.playsInline = true;
    player.setAttribute('aria-label', 'Attached ' + media.kind);
    player.append(document.createTextNode('Your browser cannot play this attachment.'));
    wrapper.append(player);
    const download = el('a','text-button', 'Download ' + media.kind);
    download.href = media.url;
    download.download = media.filename;
    wrapper.append(download);
    return wrapper;
  }
  const link = el('a','attachment file-link');
  link.href = media.url;
  link.download = media.filename;
  link.append(el('span','file-badge','FILE'),el('span','',media.filename || 'Download attachment'));
  return link;
}

function messageElement(row) {
  const article = el('article','message' + (row.id === state.target ? ' selected-message' : ''));
  article.id = 'message-' + row.id;
  article.dataset.messageId = row.id;
  const avatar = el('span','avatar',initials(row.sender));
  avatar.dataset.color = row.sender_id % 5;
  avatar.setAttribute('aria-hidden','true');
  const card = el('div','message-card');
  const meta = el('div','message-meta');
  const timestamp = el('time','message-time',timeFormatter.format(new Date(row.timestamp)));
  timestamp.dateTime = row.timestamp;
  meta.append(el('span','message-sender',row.sender),timestamp);
  card.append(meta);
  if (row.body) {
    const body = el('p','message-body');
    linkedText(body,row.body);
    card.append(body);
  }
  row.media.forEach(media=>card.append(mediaElement(media)));
  article.append(avatar,card);
  return article;
}

function renderRows(rows, previousDate=null) {
  const fragment = document.createDocumentFragment();
  let day = previousDate;
  rows.forEach(row=>{
    const newDay = row.timestamp.slice(0,10);
    if (newDay !== day) {
      const divider = el('div','date-divider',dateFormatter.format(new Date(row.timestamp)));
      divider.dataset.date = newDay;
      fragment.append(divider);
      day = newDay;
    }
    fragment.append(messageElement(row));
  });
  return fragment;
}

function updateControls() {
  $('older-status').textContent = state.older ? 'Scroll up for older messages' : (state.rows.length ? 'Beginning of the available archive' : '');
  $('newer-status').textContent = state.newer ? 'Scroll down for newer messages' : '';
  $('jump-latest').hidden = !(state.newer || feed.scrollHeight-feed.scrollTop-feed.clientHeight > 300);
  $('context-bar').hidden = !state.target;
}

async function openFeed(target=null) {
  const epoch = ++state.epoch;
  state.loading = true;
  state.target = target;
  $('older-status').textContent = 'Loading conversation…';
  try {
    const data = await get('/api/messages' + (target ? '?around='+target : ''));
    if (epoch !== state.epoch) return;
    Object.assign(state,{rows:data.messages,before:data.before,after:data.after,older:data.has_older,newer:data.has_newer});
    messages.replaceChildren(renderRows(state.rows));
    if (!state.rows.length) {
      const empty = el('div','empty-feed');
      empty.append(el('h2','','The archive is ready for its first export.'),el('p','','The administrator can upload a WhatsApp ZIP to bring the conversation here.'));
      messages.append(empty);
    }
    if (target) {
      history.replaceState(null,'','#message-'+target);
      const selected = $('message-'+target);
      if (selected) feed.scrollTop = selected.offsetTop - Math.min(120,feed.clientHeight/3);
      $('feed-announcement').textContent = 'Opened the selected message and its surrounding conversation.';
    } else {
      history.replaceState(null,'',location.pathname);
      feed.scrollTop = feed.scrollHeight;
      $('feed-announcement').textContent = 'Showing the latest archived messages.';
    }
    updateControls();
    return true;
  } catch (error) {
    if (epoch === state.epoch) {
      $('older-status').textContent = error.message;
      const retry = el('button','button subtle','Try again');
      retry.addEventListener('click',()=>openFeed(target));
      messages.replaceChildren(retry);
    }
    return false;
  } finally { if (epoch === state.epoch) state.loading = false; }
}

async function loadDirection(direction) {
  if (state.loading || !state[direction === 'older' ? 'older' : 'newer']) return;
  const epoch = state.epoch;
  state.loading = true;
  const before = direction === 'older';
  const status = before ? $('older-status') : $('newer-status');
  status.textContent = 'Loading messages…';
  const height = feed.scrollHeight;
  const top = feed.scrollTop;
  try {
    const data = await get('/api/messages?' + (before ? 'before='+encodeURIComponent(state.before) : 'after='+encodeURIComponent(state.after)));
    if (epoch !== state.epoch) return;
    const existing = new Set(state.rows.map(row=>row.id));
    const fresh = data.messages.filter(row=>!existing.has(row.id));
    if (before) {
      if (fresh.length) {
        const oldFirstDay = state.rows[0]?.timestamp.slice(0,10);
        const incomingLastDay = fresh[fresh.length-1].timestamp.slice(0,10);
        if (oldFirstDay === incomingLastDay && messages.firstElementChild?.classList.contains('date-divider')) messages.firstElementChild.remove();
        messages.prepend(renderRows(fresh));
        state.rows = fresh.concat(state.rows);
        state.before = data.before;
      }
      state.older = data.has_older;
      feed.scrollTop = top + feed.scrollHeight - height;
    } else {
      messages.append(renderRows(fresh,state.rows[state.rows.length-1]?.timestamp.slice(0,10)));
      state.rows.push(...fresh);
      state.after = data.after || state.after;
      state.newer = data.has_newer;
    }
    updateControls();
  } catch (error) { status.textContent = error.message; }
  finally { if (epoch === state.epoch) state.loading = false; }
}

function showResults() {
  $('search-panel').hidden = false;
  $('workspace').classList.add('workspace-search-mobile');
  $('back-results').hidden = true;
}

function hideResults() {
  $('search-panel').hidden = true;
  $('workspace').classList.remove('workspace-search-mobile');
  $('back-results').hidden = !state.query;
}

function highlightText(node,text,query) {
  const words = query.match(/\w+/g) || [];
  const escaped = words.map(w=>w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  if (!escaped.length) { node.textContent=text; return; }
  const regex = new RegExp('('+escaped.join('|')+')','gi');
  let start = 0;
  for (const match of text.matchAll(regex)) {
    node.append(document.createTextNode(text.slice(start,match.index)),el('mark','',match[0]));
    start = match.index + match[0].length;
  }
  node.append(document.createTextNode(text.slice(start)));
}

function renderResults(append=false) {
  if (!append) $('results').replaceChildren();
  const rows = append ? (state.appendCount ? state.results.slice(-state.appendCount) : []) : state.results;
  rows.forEach(row=>{
    const button = el('button','result' + (row.id===state.target ? ' active' : ''));
    button.type = 'button';
    button.dataset.messageId = row.id;
    const meta = el('div','result-meta');
    meta.append(el('span','result-sender',row.sender),el('span','',shortDateFormatter.format(new Date(row.timestamp))));
    const text = el('div','result-text');
    highlightText(text,row.excerpt || 'Attached media',state.query);
    button.append(meta,text);
    button.addEventListener('click',()=>{
      document.querySelectorAll('.result.active').forEach(node=>node.classList.remove('active'));
      button.classList.add('active');
      if (matchMedia('(max-width:760px)').matches) hideResults();
      openFeed(row.id);
    });
    $('results').append(button);
  });
  if (!state.results.length) {
    const empty = el('div','results-empty');
    empty.append(el('p','','No matching messages.'),el('p','','Try fewer words or a different boat term. Search covers all imported text, including captions.'));
    $('results').append(empty);
  }
  $('result-count').textContent = `${state.total.toLocaleString()} ${state.total===1 ? 'message' : 'messages'} matching “${state.query}”`;
  $('more-results').hidden = !state.more;
}

async function search(more=false) {
  const query = searchInput.value.trim();
  searchRequest?.abort();
  if (!query) { state.query=''; hideResults(); return false; }
  searchRequest = new AbortController();
  const signal = searchRequest.signal;
  const offset = more ? state.results.length : 0;
  state.query = query;
  showResults();
  $('result-count').textContent = 'Searching all messages…';
  $('more-results').disabled = true;
  if (!more) $('results').replaceChildren();
  try {
    const data = await get('/api/search?q='+encodeURIComponent(query)+'&offset='+offset,{signal});
    if (signal.aborted) return;
    state.results = more ? state.results.concat(data.results) : data.results;
    state.appendCount = data.results.length;
    state.total = data.total;
    state.more = data.has_more;
    renderResults(more);
    return true;
  } catch (error) {
    if (error.name !== 'AbortError') $('result-count').textContent = error.message;
    return false;
  } finally { if (!signal.aborted) $('more-results').disabled = false; }
}

feed.addEventListener('scroll',()=>{
  updateControls();
  if (feed.scrollTop < 160) loadDirection('older');
  else if (feed.scrollHeight-feed.scrollTop-feed.clientHeight < 200) loadDirection('newer');
},{passive:true});
$('search-form').addEventListener('submit',event=>{event.preventDefault();clearTimeout(searchTimer);search();});
searchInput.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>search(),350);});
$('close-search').addEventListener('click',hideResults);
$('back-results').addEventListener('click',showResults);
$('more-results').addEventListener('click',()=>search(true));
[$('jump-latest'),$('context-latest')].forEach(button=>button.addEventListener('click',()=>openFeed()));
const initialTarget = location.hash.match(/^#message-(\d+)$/);
openFeed(initialTarget ? Number(initialTarget[1]) : null);
addEventListener('hashchange',()=>{
  const target = location.hash.match(/^#message-(\d+)$/);
  openFeed(target ? Number(target[1]) : null);
});

// Progressive enhancement: the same search and navigation can be used by supported agents.
if (document.modelContext?.registerTool) {
  const lifecycle = new AbortController();
  const register = tool => Promise.resolve(document.modelContext.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{});
  register({name:'search_archive',title:'Search the fleet archive',description:'Search imported message text and show matching messages.',inputSchema:{type:'object',properties:{query:{type:'string',minLength:1,maxLength:300}},required:['query'],additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},async execute(input){if(!input||typeof input.query!=='string'||!input.query.trim()||input.query.length>300)throw new Error('Provide a search query of 1–300 characters.');searchInput.value=input.query;if(!await search())throw new Error('Search could not complete. Please try again.');return {total:state.total,results:state.results.map(r=>({id:r.id,sender:r.sender,date:r.timestamp,excerpt:r.excerpt}))};}});
  register({name:'open_archive_message',title:'Open a message in context',description:'Navigate to an archived message and its surrounding conversation.',inputSchema:{type:'object',properties:{messageId:{type:'integer',minimum:1}},required:['messageId'],additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},async execute(input){if(!Number.isInteger(input?.messageId)||input.messageId<1)throw new Error('Provide a positive message ID.');if(matchMedia('(max-width:760px)').matches)hideResults();if(!await openFeed(input.messageId))throw new Error('The message could not be opened. Check the message ID.');return {messageId:state.target,visibleMessages:state.rows.length};}});
  addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
}
