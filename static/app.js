/* ClipAgent — front end.
   One page, five sections in the sidebar (Make clips, My videos, Campaigns,
   Money, Settings) plus a page per video and per campaign, chosen by the URL
   hash (#/video/<id>). The editor and the campaign post kit open on top. */

const $ = (id) => document.getElementById(id);
const api = async (url, opts = {}) => {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
};
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

let CONFIG = null, currentJob = null, currentClip = null;
let poll = null, listPoll = null, statusPoll = null;
let pickedFile = null, LOOK = 'auto';
const PLAT_NAMES = { tiktok: 'TikTok', youtube: 'YouTube Shorts', instagram: 'Instagram Reels', x: 'X', facebook: 'Facebook', snapchat: 'Snapchat' };

/* ---------- small helpers ---------- */
function fill(select, pairs) { select.innerHTML = pairs.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join(''); }
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : '';
const fmt = (s) => { s = Math.max(0, s || 0); const m = Math.floor(s / 60), r = Math.floor(s % 60); return `${m}:${String(r).padStart(2, '0')}`; };
const mins = (s) => s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : s >= 60 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`;
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const short = (n) => n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K' : String(n || 0);
const money$ = (n) => '$' + (n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function ago(t) {
  if (!t) return '';
  const s = Date.now() / 1000 - t;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 7 * 86400) return new Date(t * 1000).toLocaleDateString([], { weekday: 'long' });
  return new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function toast(msg, isErr = false) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.setAttribute('role', isErr ? 'alert' : 'status');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6500 : 3200);
}
async function copyText(text, done = 'Copied') {
  try { await navigator.clipboard.writeText(text); toast(done); }
  catch { toast('Couldn’t reach the clipboard — select the text and press Ctrl+C', true); }
}
function busy(btn, label) {
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(label)}`;
  return () => { btn.disabled = false; btn.innerHTML = old; };
}

/* ---------- theme ---------- */
function themeChoice() { try { return localStorage.getItem('ca-theme') || 'system'; } catch { return 'system'; } }
function applyTheme(choice) {
  try { localStorage.setItem('ca-theme', choice); } catch { /* private window: it just won't stick */ }
  const dark = choice === 'dark' || (choice === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  $('theme-label').textContent = dark ? 'Light mode' : 'Dark mode';
  document.querySelectorAll('[data-theme-choice]').forEach(b => {
    b.classList.toggle('on', b.dataset.themeChoice === choice);
    b.setAttribute('aria-checked', b.dataset.themeChoice === choice ? 'true' : 'false');
  });
  if (!$('drawer').classList.contains('hidden')) drawTimeline();
}
$('theme-toggle').addEventListener('click', () =>
  applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'));
document.querySelectorAll('[data-theme-choice]').forEach(b => b.addEventListener('click', () => applyTheme(b.dataset.themeChoice)));
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { if (themeChoice() === 'system') applyTheme('system'); });

/* ---------- routing ---------- */
const PAGES = ['make', 'videos', 'video', 'edits', 'edit', 'campaigns', 'camp-new', 'rules', 'campaign', 'money', 'settings'];
const NAV_OF = { make: 'make', videos: 'videos', video: 'videos', edits: 'edits', edit: 'edits', campaigns: 'campaigns', 'camp-new': 'campaigns', rules: 'campaigns', campaign: 'campaigns', money: 'money', settings: 'settings' };

function showPage(name) {
  PAGES.forEach(p => $(`page-${p}`).classList.toggle('hidden', p !== name));
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('on', a.dataset.route === NAV_OF[name]));
  if (name !== 'video') { clearInterval(poll); poll = null; }
  if (name !== 'videos' && name !== 'make') { clearInterval(listPoll); listPoll = null; }
  if (name !== 'edit') { clearInterval(EE.poll); EE.poll = null; }
  if (name !== 'edits') { clearInterval(EM.listPoll); EM.listPoll = null; stopSong(); }
  window.scrollTo(0, 0);
}

function route() {
  if (!CONFIG) return;
  const parts = (location.hash.replace(/^#\/?/, '') || 'make').split('/');
  const [page, id, sub] = parts;
  OVERLAY_HASH = null; closeEditor(true); closeKit(true);
  if (page === 'video' && id) { showPage('video'); openVideo(id); }
  else if (page === 'videos') { showPage('videos'); loadVideos(); }
  else if (page === 'edits') { showPage('edits'); editsHome(id === 'from' ? sub : ''); }
  else if (page === 'edit' && id) { showPage('edit'); openEdit(id); }
  else if (page === 'campaigns' && id === 'new') { showPage('camp-new'); $('camp-brief').focus(); }
  else if (page === 'campaigns' && id === 'review') { if (CAMP.draft) { showPage('rules'); renderRulebook(); } else location.hash = '#/campaigns'; }
  else if (page === 'campaigns') { showPage('campaigns'); campHome(); }
  else if (page === 'campaign' && id && sub === 'rules') { showPage('rules'); campOpen(id, 'rules'); }
  else if (page === 'campaign' && id) { showPage('campaign'); campOpen(id, 'use'); }
  else if (page === 'money') { showPage('money'); moneyLoad(); }
  else if (page === 'settings') { showPage('settings'); renderSettings(); }
  else { showPage('make'); placeShared('make'); loadRecent(); }
}
window.addEventListener('hashchange', route);

/* ---------- boot ---------- */
(async function init() {
  applyTheme(themeChoice());
  try { CONFIG = await api('/api/config'); }
  catch (err) { toast('ClipAgent isn’t answering — is its window still open?', true); return; }

  fill($('layout'), CONFIG.layouts.map(l => [l.id, l.label]));
  const POS = { bottom: 'Bottom', middle: 'Middle', top: 'Top', pop: 'Just below the middle' };
  fill($('cappos'), CONFIG.caption_positions.map(p => [p, POS[p] || cap(p)]));
  fill($('ed-pos'), CONFIG.caption_positions.map(p => [p, POS[p] || cap(p)]));
  fill($('logocorner'), CONFIG.logo_corners.map(c => [c, cap(c.replace('-', ' '))]));
  $('maxclips').value = Math.min(8, CONFIG.max_clips);
  $('cappos').value = 'bottom';
  if (matchMedia('(max-width: 820px)').matches) $('url').placeholder = 'Paste a video link';

  buildLooks();
  buildStyleChips();
  buildLayoutChips();
  renderPresets(CONFIG.presets);
  setLogo(CONFIG.has_logo);
  loadBrand();
  $('go-hint').innerHTML = (CONFIG.telegram || {}).connected
    ? 'Takes a few minutes. You’ll get the clips on Telegram too.'
    : 'Takes a few minutes. Want them on your phone? <a href="#/settings">Set up Telegram</a>.';
  fillHookSelects();
  renderStatus();
  statusPoll = setInterval(refreshWorking, 8000);
  refreshWorking();

  // Old links (?job=…) still open the right video.
  const legacy = new URLSearchParams(location.search).get('job');
  if (legacy) { history.replaceState({}, '', location.pathname + `#/video/${legacy}`); }
  route();
})();

/* Anything marked as a button but not a <button> still answers Enter and Space. */
document.addEventListener('keydown', ev => {
  if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches && ev.target.matches('[role="button"]:not(button)')) {
    ev.preventDefault(); ev.target.click();
  }
});

/* The sidebar: what's connected, and how many videos are working. */
function renderStatus() {
  const k = CONFIG.keys || {}, tg = CONFIG.telegram || {};
  const rows = [];
  rows.push(k.claude && k.whisper
    ? '<div class="status-row"><span class="dot ok"></span>Claude and transcription ready</div>'
    : `<div class="status-row"><span class="dot bad"></span><a href="#/settings">${!k.claude ? 'Claude key missing' : 'Transcription key missing'}</a></div>`);
  rows.push(tg.connected
    ? '<div class="status-row"><span class="dot ok"></span>Telegram connected</div>'
    : `<div class="status-row"><span class="dot warn"></span><a href="#/settings">${tg.on ? 'Press Start in your bot' : 'Telegram not set up'}</a></div>`);
  $('side-status').innerHTML = rows.join('');
}
async function refreshWorking() {
  try {
    const { videos } = await api('/api/videos?limit=30');
    const n = videos.filter(v => v.status === 'running' || v.status === 'queued').length;
    $('nav-working').textContent = n;
    $('nav-working').classList.toggle('hidden', !n);
    $('nav-working').title = `${n} working`;
    const { edits } = await api('/api/edits?limit=20');
    const e = edits.filter(x => x.status === 'running' || x.status === 'queued').length;
    $('nav-editing').textContent = e;
    $('nav-editing').classList.toggle('hidden', !e);
    $('nav-editing').title = `${e} edit${e === 1 ? '' : 's'} being made`;
  } catch { /* a missed tick is fine */ }
}

/* =====================================================================
   Make clips
   ===================================================================== */
const urlBox = $('url');
const LINK_RE = /https?:\/\/\S+/g;
function autoGrow(el) { el.style.height = 'auto'; el.style.height = Math.min(220, el.scrollHeight + 2) + 'px'; }
function linksIn(text) { return [...new Set((text || '').match(LINK_RE) || [])]; }
urlBox.addEventListener('input', () => {
  autoGrow(urlBox);
  const n = linksIn(urlBox.value).length;
  if (n && pickedFile) clearFile();
  $('go').textContent = n > 1 ? `Make clips for ${n} videos` : 'Make clips';
  $('url-hint').textContent = n > 1 ? `${n} videos — they run one after another.` : 'Got several videos? Paste one link per line and they run one after another.';
});
urlBox.addEventListener('keydown', ev => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) $('go').click(); });

const drop = $('drop');
$('pickfile').addEventListener('click', () => $('fileinput').click());
['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('hot'); }));
['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('hot'); }));
drop.addEventListener('drop', ev => setFile(ev.dataTransfer.files[0]));
$('fileinput').addEventListener('change', ev => setFile(ev.target.files[0]));
function setFile(file) {
  if (!file) return;
  pickedFile = file;
  drop.classList.add('has-file');
  $('dropname').textContent = `${file.name} — ${(file.size / 1e6).toFixed(0)} MB, ready`;
  $('pickfile').textContent = 'Choose another';
  urlBox.value = ''; autoGrow(urlBox);
  $('go').textContent = 'Make clips';
}
function clearFile() {
  pickedFile = null; $('fileinput').value = '';
  drop.classList.remove('has-file');
  $('dropname').textContent = 'or drop a video file here — MP4, MOV, MKV, WEBM, up to about 4 hours';
  $('pickfile').textContent = 'Choose a file';
}

$('platforms').querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
  ch.classList.toggle('on');
  if (!$('platforms').querySelector('.chip.on')) ch.classList.add('on');   // at least one
  platHint();
}));
function chosenPlatforms(el = $('platforms')) { return [...el.querySelectorAll('.chip.on')].map(c => c.dataset.p); }
function platHint() {
  const p = chosenPlatforms();
  const lens = { youtube: '16–35 s', tiktok: '25–60 s', instagram: '15–45 s' };
  $('plat-hint').textContent = p.length === 1
    ? `Clips aim for ${lens[p[0]]}, where winning ${PLAT_NAMES[p[0]]} clips sit.`
    : 'Clips aim for a length that works on all of them.';
}

$('mc-minus').addEventListener('click', () => { $('maxclips').value = Math.max(1, (+$('maxclips').value || 8) - 1); });
$('mc-plus').addEventListener('click', () => { $('maxclips').value = Math.min(24, (+$('maxclips').value || 8) + 1); });

/* ---------- the look gallery ---------- */
const LOOK_SAMPLES = {
  auto: '<div class="s-auto"><i></i><i></i><i></i><i></i></div>',
  wordpop: '<div class="s-word" style="bottom:30%;font-size:22px">He made <span class="g">$20M</span></div>',
  label: '<div class="s-label">Tyler was SHOCKED after the final shot 😳</div>',
  titlebar: '<div class="s-bar">He made <span class="y">$1.3M</span> in a year</div><div class="s-word" style="bottom:28%;font-size:17px">no way</div>',
  bubble: '<div class="s-bubble">no way he actually said that 😭</div><div class="s-word" style="bottom:28%;font-size:17px">I quit</div>',
  stack: '<div class="s-stack"><i></i><i></i></div><div class="s-word" style="top:44%;font-size:18px">No <span class="y">way</span></div>',
};
const LOOK_NAMES = {
  auto: ['Let ClipAgent choose', 'The best look for each clip, from what’s winning right now'],
  wordpop: ['Word-pop captions', 'Big words, 1–3 at a time, money in green'],
  label: ['Headline label', 'White box with the story in one line'],
  titlebar: ['Title bar', 'One headline on top for the whole clip'],
  bubble: ['Comment bubble', 'A viewer’s reaction on top'],
  stack: ['Stacked split', 'Two people, one panel each'],
};
function classicSample(s) {
  const font = s.font === 'Anton' ? '"Anton", Impact, sans-serif' : '"Poppins", sans-serif';
  const words = s.uppercase ? ['THIS', 'IS', 'CRAZY'] : ['this', 'is', 'crazy'];
  const style = `bottom:30%;font-family:${font};font-size:${s.font === 'Anton' ? 21 : 16}px;` +
    (s.box ? 'background:rgba(0,0,0,.65);-webkit-text-stroke:0;padding:4px 2px;border-radius:4px;' : '');
  return `<div class="s-word" style="${style}">${words[0]} ${words[1]} <span style="color:${s.active}">${words[2]}</span></div>`;
}
function buildLooks() {
  const recipes = Object.fromEntries((CONFIG.recipes || []).map(r => [r.id, r]));
  const card = (id, name, sub, sample, extra = '') => `
    <button type="button" class="look ${extra}" data-look="${id}" aria-pressed="false">
      ${id === 'auto' ? '<span class="badge">Best</span>' : ''}<span class="tick" aria-hidden="true"></span>
      <div class="frame">${sample}</div>
      <div class="name">${esc(name)}</div><div class="sub">${esc(sub)}</div>
    </button>`;
  let html = '<div class="look-row">' + card('auto', ...LOOK_NAMES.auto, LOOK_SAMPLES.auto);
  ['wordpop', 'label', 'titlebar', 'bubble', 'stack'].forEach(id => {
    if (!recipes[id]) return;
    html += card(id, LOOK_NAMES[id][0], LOOK_NAMES[id][1], LOOK_SAMPLES[id], recipes[id].available ? '' : 'unavailable');
  });
  html += '</div><div class="look-group">Classic captions — the same look on every clip</div><div class="look-row">';
  CONFIG.caption_styles.forEach(s => { html += card(`classic:${s.id}`, s.label, 'Captions only', classicSample(s)); });
  $('looks').innerHTML = html + '</div>';
  $('looks').querySelectorAll('.look').forEach(el => el.addEventListener('click', () => setLook(el.dataset.look)));
  setLook('auto');
}
function setLook(id) {
  LOOK = id;
  $('looks').querySelectorAll('.look').forEach(el => {
    el.classList.toggle('on', el.dataset.look === id);
    el.setAttribute('aria-pressed', el.dataset.look === id ? 'true' : 'false');
  });
  const hints = {
    auto: 'Each clip gets the look that suits it — a reaction gets a headline label, a story gets word-pop captions.',
    stack: 'Only used on clips where two people sit side by side. Others get word-pop captions.',
  };
  $('look-hint').textContent = id.startsWith('classic:') ? 'Captions in this style on every clip, with a hook at the start.' : (hints[id] || 'Every clip gets this look. Claude still writes each one’s text.');
}

/* ---------- settings sent with a run ---------- */
function settings() {
  const classic = LOOK.startsWith('classic:');
  return {
    max_clips: Math.max(1, Math.min(24, +$('maxclips').value || 8)),
    layout: $('layout').value,
    caption_style: classic ? LOOK.split(':')[1] : 'impact',
    caption_position: $('cappos').value,
    auto_style: !classic,
    style_recipe: classic ? 'auto' : LOOK,
    look: LOOK,
    platforms: chosenPlatforms().join(','),
    tighten: $('opt-tighten').checked,
    drop_fillers: $('opt-fillers').checked,
    auto_frame: $('opt-frame').checked,
    motion: $('opt-motion').checked,
    structure: $('opt-structure').checked,
    alternates: $('opt-structure').checked,
    headline: $('opt-headline').checked,
    doctor: $('opt-doctor').checked,
    logo: $('opt-logo').checked,
    logo_corner: $('logocorner').value,
    accent: $('accent').dataset.on === '1' ? $('accent').value : '',
  };
}
function applySettings(s) {
  if (!s) return;
  if (s.max_clips) $('maxclips').value = s.max_clips;
  if (s.layout) $('layout').value = s.layout;
  if (s.caption_position) $('cappos').value = s.caption_position;
  const look = s.look || (s.auto_style === false ? `classic:${s.caption_style || 'impact'}` : (s.style_recipe || 'auto'));
  setLook($('looks').querySelector(`[data-look="${look}"]`) ? look : 'auto');
  if (s.platforms) {
    const want = Array.isArray(s.platforms) ? s.platforms : String(s.platforms).split(',');
    $('platforms').querySelectorAll('.chip').forEach(c => c.classList.toggle('on', want.includes(c.dataset.p)));
    if (!$('platforms').querySelector('.chip.on')) $('platforms').querySelectorAll('.chip').forEach(c => c.classList.add('on'));
    platHint();
  }
  $('opt-tighten').checked = s.tighten !== false;
  $('opt-fillers').checked = s.drop_fillers !== false;
  $('opt-frame').checked = s.auto_frame !== false;
  $('opt-motion').checked = s.motion !== false;
  $('opt-structure').checked = s.structure !== false;
  $('opt-headline').checked = s.headline !== false;
  $('opt-doctor').checked = s.doctor !== false;
  $('opt-logo').checked = !!s.logo;
  if (s.logo_corner) $('logocorner').value = s.logo_corner;
  if (s.accent) { $('accent').value = s.accent; $('accent').dataset.on = '1'; showAccent(); }
}

/* The look, count and options sit on the Make clips page, and move to a
   campaign's page when you make clips for one. */
const SHARED_HOME = { parent: $('shared').parentElement, next: $('shared').nextElementSibling };
function placeShared(where) {
  const el = $('shared');
  if (where === 'campaign') $('cu-shared-slot').appendChild(el);
  else if (el.parentElement !== SHARED_HOME.parent) SHARED_HOME.parent.insertBefore(el, SHARED_HOME.next);
  if (where !== 'campaign') unlockShared();
}

/* ---------- presets ---------- */
function renderPresets(list) {
  CONFIG.presets = list || [];
  $('presetlist').innerHTML = CONFIG.presets.length
    ? CONFIG.presets.map(p => `<span class="chip preset" data-name="${esc(p.name)}" role="button" tabindex="0">${esc(p.name)}
        <b class="x" title="Delete" aria-label="Delete ${esc(p.name)}">×</b></span>`).join('')
    : '<span class="hint">None saved yet.</span>';
  $('presetlist').querySelectorAll('.preset').forEach(chip => {
    chip.addEventListener('click', async (ev) => {
      const name = chip.dataset.name;
      if (ev.target.classList.contains('x')) {
        const r = await api(`/api/presets/${encodeURIComponent(name)}`, { method: 'DELETE' });
        renderPresets(r.presets); toast(`Deleted “${name}”`);
        return;
      }
      const found = CONFIG.presets.find(p => p.name === name);
      applySettings(found && found.settings);
      toast(`Using “${name}”`);
    });
  });
}
$('savepreset').addEventListener('click', async () => {
  const name = $('presetname').value.trim();
  if (!name) return toast('Give these settings a name first', true);
  try {
    const r = await post('/api/presets', { name, settings: settings() });
    renderPresets(r.presets); $('presetname').value = '';
    toast(`Saved “${name}”`);
  } catch (err) { toast(err.message, true); }
});

/* ---------- start a run ---------- */
$('go').addEventListener('click', async () => {
  const links = linksIn(urlBox.value);
  const typed = urlBox.value.trim();
  if (!links.length && !pickedFile) {
    toast(typed ? 'That doesn’t look like a link — it should start with https://' : 'Paste a video link or drop a file first', true);
    urlBox.focus(); return;
  }
  const done = busy($('go'), 'Starting…');
  try {
    if (links.length > 1) {
      if (links.length > 25) throw new Error('25 links at a time is the limit');
      const s = settings();
      await post('/api/batch', { urls: links, settings: { ...s, platforms: chosenPlatforms() } });
      urlBox.value = ''; autoGrow(urlBox);
      toast(`${links.length} videos queued — they run one after another`);
      location.hash = '#/videos';
    } else {
      const form = new FormData();
      if (pickedFile) form.append('file', pickedFile); else form.append('url', links[0]);
      Object.entries(settings()).forEach(([k, v]) => form.append(k, v));
      const { job_id } = await api('/api/jobs', { method: 'POST', body: form });
      urlBox.value = ''; autoGrow(urlBox); clearFile();
      location.hash = `#/video/${job_id}`;
    }
    refreshWorking();
  } catch (err) { toast(err.message, true); }
  finally { done(); $('go').textContent = 'Make clips'; }
});

/* =====================================================================
   Video lists: recent (Make clips page), all (My videos), per campaign
   ===================================================================== */
const STATUS_TAG = (v) => {
  if (v.status === 'running' || v.status === 'queued')
    return `<span class="tag accent">${v.status === 'queued' || /waiting|queued/i.test(v.stage || '') ? 'Waiting' : `Working ${v.progress || 0}%`}</span>`;
  if (v.status === 'failed') return '<span class="tag bad">Didn’t work</span>';
  if (!v.clips) return '<span class="tag warn">No clips</span>';
  return `<span class="tag good">${v.clips} clip${v.clips === 1 ? '' : 's'}</span>`;
};
const PH_ICON = '<span class="ph"><svg viewBox="0 0 24 24"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 10l3 2-3 2z"/></svg></span>';
function sourceName(src) {
  if (!src || src === 'upload' || src === 'campaign') return '';
  try { return new URL(src).hostname.replace(/^www\./, ''); } catch (e) { return ''; }
}
/* A run's name for people: never a bare link, never a file extension. */
function niceTitle(v) {
  const t = (v.title || '').trim();
  if (t && !/^https?:\/\//.test(t)) return t.replace(/\.(mp4|mov|mkv|webm|m4v|avi)$/i, '');
  if (v.status === 'running' || v.status === 'queued') return 'Getting the video…';
  const host = sourceName(v.source || t);
  return host ? `Video from ${host}` : 'Untitled video';
}
function vcard(v, opts = {}) {
  const working = v.status === 'running' || v.status === 'queued';
  const title = niceTitle(v);
  const camp = !opts.noCampaign && v.campaign && v.campaign.name ? `<span class="tag" title="${esc(v.campaign.name)}">${esc(v.campaign.name)}</span>` : '';
  return `<a class="vcard" href="#/video/${v.id}">
    <div class="poster">${v.poster ? `<img src="${v.poster}" alt="" loading="lazy" onerror="this.replaceWith(document.createRange().createContextualFragment(PH_ICON))">` : working ? '<span class="spin"></span>' : PH_ICON}</div>
    <div>
      <div class="t" title="${esc(title)}">${esc(title)}</div>
      <div class="facts">${STATUS_TAG(v)}${camp}</div>
      ${working ? `<div class="mini-bar"><i style="width:${v.progress || 0}%"></i></div>` : ''}
      ${v.status === 'failed' && v.error ? `<div class="why">${esc(v.error)}</div>` : `<div class="when">${ago(v.created_at)}${v.duration ? ` — from ${mins(v.duration)} of video` : ''}</div>`}
    </div>
  </a>`;
}
let VIDEOS = [], VFILTER = 'all';
async function fetchVideos() { const { videos } = await api('/api/videos?limit=120'); VIDEOS = videos; return videos; }
function keepPolling(render) {
  clearInterval(listPoll);
  if (VIDEOS.some(v => v.status === 'running' || v.status === 'queued'))
    listPoll = setInterval(async () => { await fetchVideos().catch(() => {}); render(); }, 5000);
}
async function loadRecent() {
  try { await fetchVideos(); } catch (err) { return; }
  const render = () => {
    const recent = VIDEOS.slice(0, 6);
    $('recent-wrap').classList.toggle('hidden', !recent.length);
    $('recent').innerHTML = recent.map(v => vcard(v)).join('');
  };
  render(); keepPolling(render);
}
async function loadVideos() {
  try { await fetchVideos(); } catch (err) { toast(err.message, true); return; }
  renderVideos(); keepPolling(renderVideos);
}
function renderVideos() {
  const pick = { all: () => true, working: v => v.status === 'running' || v.status === 'queued', done: v => v.status === 'done', failed: v => v.status === 'failed' }[VFILTER];
  const list = VIDEOS.filter(pick);
  $('v-filters').querySelectorAll('.chip').forEach(c => {
    const n = VIDEOS.filter({ all: () => true, working: v => v.status === 'running' || v.status === 'queued', done: v => v.status === 'done', failed: v => v.status === 'failed' }[c.dataset.f]).length;
    c.classList.toggle('on', c.dataset.f === VFILTER);
    c.innerHTML = `${{ all: 'All', working: 'Working', done: 'Done', failed: 'Didn’t work' }[c.dataset.f]} <span class="n">${n}</span>`;
  });
  $('videos').innerHTML = list.length ? list.map(v => vcard(v)).join('')
    : (VIDEOS.length ? '<div class="empty">Nothing here.</div>'
      : '<div class="empty"><b>No videos yet</b>Paste a link on Make clips to get your first batch of clips.<br><a class="btn" href="#/make">Make clips</a></div>');
}
$('v-filters').querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => { VFILTER = c.dataset.f; renderVideos(); }));

/* =====================================================================
   One video: progress, where the clips come from, the clips
   ===================================================================== */
let CFILTER = 'all', HL = null;
function openVideo(id) {
  currentJob = null; CFILTER = 'all'; HL = null;
  $('job-title').textContent = 'Loading…'; $('job-sub').innerHTML = '';
  $('grid').innerHTML = ''; $('c-filters').innerHTML = ''; $('strip-wrap').classList.add('hidden');
  $('progress').classList.add('hidden');
  $('ask').classList.add('hidden'); $('ask-log').innerHTML = ''; ASK_SCOPE.clear();
  watchJob(id);
  loadAsks(id);
}
function watchJob(jobId) {
  clearInterval(poll);
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (location.hash !== `#/video/${jobId}`) { clearInterval(poll); return; }
      currentJob = job;
      renderVideoHead(job);
      renderProgress(job);
      renderStrip(job);
      renderResults(job);
      renderAskBox(job);
      const busyClip = job.clips.some(c => ['rendering', 'checking', 'pending'].includes(c.status));
      if ((job.status === 'done' || job.status === 'failed') && !busyClip) { clearInterval(poll); poll = null; refreshWorking(); }
    } catch (err) {
      clearInterval(poll); poll = null;
      $('job-title').textContent = 'Video not found';
      $('job-sub').innerHTML = `<span class="hint">${esc(err.message)}</span>`;
    }
  };
  tick();
  poll = setInterval(tick, 2500);
}

function renderVideoHead(job) {
  const title = niceTitle(job);
  $('job-title').textContent = title;
  document.title = `${title} — ClipAgent`;
  const main = job.clips.filter(c => !c.alt_of);
  const ready = main.filter(c => c.status === 'ready').length;
  const tags = [];
  if (job.campaign) tags.push(`<a class="tag accent" href="#/campaign/${job.campaign.id}">${esc(job.campaign.name)}</a>`);
  if (job.duration) tags.push(`<span class="tag">${mins(job.duration)} video</span>`);
  if (main.length) {
    const groups = { ready: 0, look: 0, working: 0, failed: 0 };
    main.forEach(c => groups[clipGroup(c)]++);
    tags.push(`<span class="tag">${main.length} clip${main.length > 1 ? 's' : ''}</span>`);
    if (groups.ready) tags.push(`<span class="tag good">${groups.ready} ready to post</span>`);
    if (groups.look) tags.push(`<span class="tag warn">${groups.look} to check first</span>`);
    if (groups.working) tags.push(`<span class="tag accent">${groups.working} still making</span>`);
    if (groups.failed) tags.push(`<span class="tag bad">${groups.failed} didn’t render</span>`);
  }
  if ((job.platforms || []).length) tags.push(`<span class="tag">For ${job.platforms.map(p => esc(PLAT_NAMES[p] || p)).join(', ')}</span>`);
  if (job.created_at) tags.push(`<span class="tag">${ago(job.created_at)}</span>`);
  $('job-sub').innerHTML = tags.join('');
  $('dlzip').href = `/api/jobs/${job.id}/download.zip`;
  $('dlcsv').href = `/api/jobs/${job.id}/export.csv`;
  const hasClips = ready > 0;
  ['dlzip', 'dlcsv', 'planposts'].forEach(id => $(id).classList.toggle('hidden', !hasClips));
  $('makeedit').href = `#/edits/from/${job.id}`;
  $('makeedit').classList.toggle('hidden', !(job.status === 'done' && hasClips && !(job.campaign && job.campaign.mode === 'overlay')));
  $('rerun').classList.toggle('hidden', !(job.status === 'done' && job.can_retry));
}

const STEPS = [
  ['Download', /download|reading your file|waiting|queued/i],
  ['Listen', /extract|transcrib|reusing the transcript/i],
  ['Find moments', /finding|ranking|judging|framed|building each clip/i],
  ['Style', /choosing a style|against the brief/i],
  ['Make and check', /render|framing, rendering|checked/i],
];
const OVERLAY_STEPS = [
  ['Get the clips', /download|reading/i], ['Look at them', /looking|watching|reading/i],
  ['Write hooks', /writing|hooks/i], ['Make and check', /render|check/i],
];
function renderProgress(job) {
  const box = $('progress');
  const running = job.status === 'running' || job.status === 'queued';
  const failed = job.status === 'failed';
  box.classList.toggle('hidden', !running && !failed);
  box.classList.toggle('failed', failed);
  if (!running && !failed) return;
  const steps = job.campaign && job.campaign.mode === 'overlay' ? OVERLAY_STEPS : STEPS;
  let at = steps.findIndex(([, re]) => re.test(job.stage || ''));
  if (at < 0 && failed) {        // older runs only say "Failed": read the step off the error
    const e = job.error_detail || job.error || '';
    at = /download|youtube|unsupported url|not a bot|http error|upload|file/i.test(e) ? 0
      : /transcri|audio|413|whisper/i.test(e) ? 1
      : /closed or restarted/i.test(e) ? -1 : 2;
    if (at < 0) at = 0;
  }
  if (at < 0) at = job.progress > 75 ? steps.length - 1 : 0;
  $('steps').innerHTML = steps.map(([label], i) => {
    const cls = failed && i === at ? 'fail' : i < at ? 'done' : i === at ? (failed ? 'fail' : 'on') : '';
    return `<span class="s ${cls}"><i></i>${label}</span>`;
  }).join('');
  $('barfill').style.width = `${failed ? 100 : job.progress || 0}%`;
  $('stagetext').textContent = failed ? (job.error || 'It didn’t work.') : (job.stage || 'Working…');
  $('pct').textContent = failed ? '' : `${job.progress || 0}%`;
  $('retry').classList.toggle('hidden', !(failed && (job.can_retry || job.can_refetch)));
  $('retry').textContent = job.can_retry ? 'Try again — no new download' : 'Try again';
  const raw = failed && job.error_detail && job.error_detail !== job.error ? job.error_detail : '';
  $('err-detail').classList.toggle('hidden', !raw);
  $('err-raw').textContent = raw;
}
$('retry').addEventListener('click', () => rerun('Trying again from the copy already downloaded'));
$('rerun').addEventListener('click', () => rerun('Running it again — same video, fresh picks'));
async function rerun(msg) {
  if (!currentJob) return;
  const done = busy($('rerun'), 'Starting…');
  try {
    const { job_id } = await post(`/api/jobs/${currentJob.id}/rerun`, {});
    toast(msg);
    location.hash = `#/video/${job_id}`;
  } catch (err) { toast(err.message, true); }
  finally { done(); }
}
$('planposts').addEventListener('click', async () => {
  if (!currentJob) return;
  const done = busy($('planposts'), 'Planning…');
  try {
    const { planned } = await post(`/api/jobs/${currentJob.id}/plan`, {});
    toast(planned.length ? `Planned ${planned.length} posts — see them in Money. Reminders come on Telegram.` : 'Nothing new to plan — every clip is already scheduled');
  } catch (err) {
    toast(/accounts/i.test(err.message) ? 'Add the accounts you post to first — Money page, “Your accounts”' : err.message, true);
  } finally { done(); }
});

/* The long video as a strip, with each clip's piece of it marked. */
function renderStrip(job) {
  const main = job.clips.filter(c => !c.alt_of);
  const wrap = $('strip-wrap');
  if (!job.duration || !main.length || (job.campaign && job.campaign.mode === 'overlay')) { wrap.classList.add('hidden'); return; }
  wrap.classList.remove('hidden');
  $('strip-len').textContent = fmt(job.duration);
  const D = job.duration;
  let html = '';
  for (let m = 10 * 60; m < D; m += 10 * 60) html += `<span class="tick" style="left:${(m / D) * 100}%"></span>`;
  main.forEach(c => {
    const spans = c.parts && c.parts.length > 1 ? c.parts : [{ start: c.start, end: c.end }];
    spans.forEach((p, i) => {
      const w = Math.max(0.4, ((p.end - p.start) / D) * 100);
      html += `<span class="m ${HL === c.id ? 'hl' : ''}" data-id="${c.id}" style="left:${(p.start / D) * 100}%;width:${w}%"
        title="Clip ${c.rank}: ${fmt(p.start)}–${fmt(p.end)}">${i === 0 ? c.rank : ''}</span>`;
    });
  });
  $('strip').innerHTML = html;
  $('strip').querySelectorAll('.m').forEach(el => el.addEventListener('click', () => {
    HL = el.dataset.id; renderStrip(currentJob); renderResults(currentJob);
    document.querySelector(`.clip[data-id="${HL}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }));
}

/* ---------- the clips ---------- */
const GATE_LABEL = { ready: 'Ready to post', check: 'Check first', blocked: 'Blocked' };
const STYLE_NAME = { wordpop: 'Word-pop', label: 'Headline label', titlebar: 'Title bar', bubble: 'Comment bubble', stack: 'Stacked split' };
function clipLength(c) { return Math.max(0, (c.duration || 0) - (c.saved || 0)); }
function clipGroup(c) {
  if (c.status === 'failed') return 'failed';
  if (c.status !== 'ready') return 'working';
  const gate = c.compliance && c.compliance.status;
  if (gate === 'blocked' || gate === 'check' || (c.doctor && c.doctor.status === 'check')) return 'look';
  return 'ready';
}
function renderResults(job) {
  const showAlts = $('show-alts').checked;
  const alts = job.clips.filter(c => c.alt_of).length;
  $('alts-wrap').classList.toggle('hidden', !alts);
  const main = job.clips.filter(c => !c.alt_of);
  const counts = { all: main.length, ready: 0, look: 0, working: 0, failed: 0 };
  main.forEach(c => counts[clipGroup(c)]++);
  const names = { all: 'All', ready: 'Ready to post', look: 'Check first', working: 'Still making', failed: 'Didn’t render' };
  $('c-filters').innerHTML = main.length > 1 ? Object.keys(names).filter(k => k === 'all' || counts[k])
    .map(k => `<button type="button" class="chip ${CFILTER === k ? 'on' : ''}" data-f="${k}">${names[k]} <span class="n">${counts[k]}</span></button>`).join('') : '';
  $('c-filters').querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => { CFILTER = ch.dataset.f; renderResults(currentJob); }));

  const order = [];
  main.forEach(c => {
    if (CFILTER === 'all' || clipGroup(c) === CFILTER) {
      order.push(c);
      if (showAlts) job.clips.filter(a => a.alt_of === c.id).forEach(a => order.push(a));
    }
  });
  if (!order.length) {
    const running = job.status === 'running' || job.status === 'queued';
    $('grid').innerHTML = running ? '' : job.status === 'failed' ? ''
      : `<div class="empty"><b>${esc(job.stage && /no clip-worthy/i.test(job.stage) ? 'No moments good enough to post' : 'No clips here')}</b>${
        /no clip-worthy/i.test(job.stage || '') ? 'ClipAgent didn’t find a moment that would stand on its own. Try a different video.' : ''}</div>`;
    return;
  }
  // Keep a playing preview playing through the 2.5-second refresh.
  const playing = document.querySelector('.clip .poster.playing');
  const playingId = playing && playing.closest('.clip').dataset.id;
  if (playingId && order.some(c => c.id === playingId) && job.clips.find(c => c.id === playingId)?.status === 'ready'
      && $('grid').dataset.sig === sig(order)) return;
  $('grid').dataset.sig = sig(order);
  $('grid').innerHTML = order.map(c => clipCard(c, job)).join('');
  wireCards(job);
}
const sig = (order) => order.map(c => `${c.id}:${c.status}:${c.thumb_url}:${(c.compliance || {}).status}:${(c.doctor || {}).status}:${c.hook}`).join('|');

function doctorHTML(c) {
  const d = c.doctor;
  if (!d) return '';
  const score = d.postable ? ` · ${d.postable}/10` : '';
  const head = { good: 'Checked — looks good', fixed: 'Checked — fixed', check: 'Check first' }[d.status] || 'Checked';
  const first = (d.fixed && d.fixed[0]) || ((d.summary || '').replace(/^(Fixed|Look at):\s*/, '').split(/;|\. /)[0]);
  const rest = d.status !== 'good' && d.summary && d.summary.length > (first || '').length + 4;
  return `<div class="doc ${d.status}"><b>${head}${score}</b>${d.status !== 'good' && first ? ` — ${esc(first)}` : ''}${
    rest ? ` <button type="button" class="more-link" data-act="doc">More</button><div class="full hidden">${esc(d.summary)}</div>` : ''}</div>`;
}
function clipCard(c, job) {
  const gate = c.compliance && c.compliance.status;
  const isCamp = !!(c.overlay || c.compliance || c.clip_type === 'campaign');
  const ready = c.status === 'ready';
  const style = c.edits && c.edits.style;
  const facts = [];
  if (style) facts.push(`<span class="tag accent">${esc(STYLE_NAME[style] || style)}</span>`);
  if (c.alt_of) facts.push(`<span class="tag">Second version</span>`);
  else if (c.parts && c.parts.length > 1) facts.push(`<span class="tag">Stitched</span>`);
  if (c.post && c.post.platform) facts.push(`<span class="tag" title="The platform this clip's length and caption suit best">For ${esc(PLAT_NAMES[c.post.platform] || c.post.platform)}</span>`);
  let overlayBadge = '';
  if (c.status === 'failed') overlayBadge = '<div class="gate failed">Didn’t render</div>';
  else if (isCamp && gate) overlayBadge = `<div class="gate ${gate}">${GATE_LABEL[gate]}</div>`;
  else if (isCamp && ready) overlayBadge = '<div class="gate checking">Checking…</div>';
  const poster = c.thumb_url && c.status !== 'failed'
    ? `<img src="${c.thumb_url}?v=${encodeURIComponent(c.status)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'"><span class="play"></span>`
    : `<div class="rendering">${c.status === 'failed' ? '' : '<span><span class="spin"></span> Making it…</span>'}</div>`;
  const score = c.score ? `<span class="score ${c.score >= 80 ? 'hi' : ''}" title="How likely it is to do well, out of 100">Score ${c.score}</span>` : '';
  const len = ready || c.duration ? `<span class="len">${fmt(clipLength(c))}</span>` : '';
  let actions;
  if (!ready) actions = c.status === 'failed' ? `<div class="hint wide">${esc(c.reason || 'This clip failed to render.')}</div>` : '';
  else if (isCamp) actions = `<button class="btn small wide" data-act="kit">Post kit</button>
      ${c.overlay ? '' : '<button class="btn ghost small" data-act="edit">Edit</button>'}
      <button class="btn ghost small ${c.overlay ? 'wide' : ''}" data-act="dl" ${gate === 'blocked' ? 'disabled title="Blocked by the campaign check — open the post kit to see why"' : ''}>Download</button>`;
  else actions = `<button class="btn small" data-act="dl">Download</button>
      <button class="btn ghost small" data-act="edit">Edit</button>
      <button class="btn ghost small wide" data-act="copy">Copy caption</button>`;
  return `<article class="clip ${c.alt_of ? 'alt' : ''} ${c.status === 'failed' ? 'failed' : ''} ${HL === c.id ? 'hl' : ''}" data-id="${c.id}">
    <div class="poster" data-act="play" title="${ready ? 'Play' : ''}">${poster}${overlayBadge}</div>
    <div class="body">
      <div class="card-top"><span class="num">${c.alt_of ? `#${c.rank} B` : `#${c.rank}`}</span>${score}${len}${
        c.can_undo && ready ? '<button type="button" class="undo-link" data-act="undo" title="Put back the version from before the last change">Undo</button>' : ''}</div>
      ${facts.length ? `<div class="facts">${facts.join('')}</div>` : ''}
      ${c.hook ? `<div class="hook">${esc(c.hook)}</div>` : `<div class="hook">${esc(c.title || '')}</div>`}
      ${doctorHTML(c)}
      ${isCamp && gate && gate !== 'ready' ? `<div class="doc check"><b>${GATE_LABEL[gate]}</b> — ${esc((c.compliance.summary || '').replace(/^(Blocked|Check before posting):\s*/, ''))}</div>` : ''}
      <div class="actions">${actions}</div>
    </div>
  </article>`;
}
function wireCards(job) {
  $('grid').querySelectorAll('.clip').forEach(card => {
    const clip = () => currentJob.clips.find(c => c.id === card.dataset.id);
    card.querySelectorAll('[data-act]').forEach(el => el.addEventListener('click', (ev) => {
      const c = clip(); if (!c) return;
      const act = el.dataset.act;
      if (act === 'play') {
        if (c.status !== 'ready' || !c.video_url || el.classList.contains('playing')) return;
        el.classList.add('playing');
        el.querySelector('img')?.remove();
        el.insertAdjacentHTML('afterbegin', `<video src="${c.video_url}?v=${Date.now()}" controls autoplay playsinline></video>`);
      } else if (act === 'edit') openEditor(c);
      else if (act === 'undo') undoClip(c.id);
      else if (act === 'kit') openKit(c);
      else if (act === 'dl') location.href = `/api/clips/${c.id}/download`;
      else if (act === 'copy') {
        const tags = (c.hashtags || []).map(t => '#' + String(t).replace(/^#/, '')).join(' ');
        copyText([c.caption || '', tags].filter(Boolean).join('\n\n') || c.hook || '', 'Caption and hashtags copied');
      } else if (act === 'doc') {
        ev.stopPropagation();
        const full = el.parentElement.querySelector('.full');
        full.classList.toggle('hidden'); el.textContent = full.classList.contains('hidden') ? 'More' : 'Less';
      }
    }));
  });
}
$('show-alts').addEventListener('change', () => currentJob && renderResults(currentJob));

/* =====================================================================
   Editor
   ===================================================================== */
const CRITERIA_LABELS = { clarity: 'Clear in 3 s', hook: 'Hook', context: 'Context', payoff: 'Payoff', flow: 'Flow', pace: 'Pace' };
function judgeTable(c) {
  const j = c.judge || {};
  if (!j.continuous) return '';
  const cols = ['continuous', 'stitched'].filter(v => j[v]);
  const name = v => v === 'stitched' ? 'With setup' : 'Straight cut';
  const rows = Object.keys(CRITERIA_LABELS).map(k => `<tr><td>${CRITERIA_LABELS[k]}</td>${cols.map(v => `<td>${(+j[v][k]).toFixed(1)}</td>`).join('')}</tr>`).join('');
  const why = (j.why || []).slice(0, 2).map(w => `<div class="why">“${esc(w)}”</div>`).join('');
  return `<h4>How a first-time viewer scored it (1–10)</h4><table><tr><th></th>${cols.map(v =>
    `<th class="${cols.length > 1 && j.winner === v ? 'win' : ''}">${name(v)}</th>`).join('')}</tr>${rows}
    <tr><td><b>Overall</b></td>${cols.map(v => `<td class="${cols.length > 1 && j.winner === v ? 'win' : ''}">${j[v].overall.toFixed(1)}</td>`).join('')}</tr></table>${why}`;
}
/* The campaign check (the brief's rules), for the editor's Check tab. */
function campaignCheckHTML(c) {
  const g = c.compliance;
  if (!g) return '';
  const items = (g.checks || []).filter(ch => ch.status !== 'pass')
    .map(ch => `<li class="${ch.status === 'fail' ? 'fail' : 'warn'}">${esc(ch.label)}${ch.detail ? ` — ${esc(ch.detail)}` : ''}</li>`);
  if (!items.length) items.push('<li>Follows every rule in the brief.</li>');
  return `<div class="doc-report"><div class="head"><span>Campaign check: ${GATE_LABEL[g.status] || g.status}</span></div><ul>${items.join('')}</ul></div>`;
}
function doctorReport(c) {
  const d = c.doctor;
  if (!d) return '<div class="doc-report"><div class="head"><span>Clip doctor</span></div><div class="hint">This clip wasn’t checked.</div></div>';
  const head = { good: 'Looks good', fixed: 'Fixed what it found', check: 'Check first before posting' }[d.status] || 'Checked';
  const items = [];
  (d.fixed || []).forEach(f => items.push(`<li class="fixed">Fixed: ${esc(f)}</li>`));
  (d.checks || []).filter(ch => ch.status !== 'pass').forEach(ch => items.push(`<li class="${ch.status}">${esc(ch.label)}${ch.detail ? ` — ${esc(ch.detail)}` : ''}</li>`));
  (d.issues || []).forEach(i => items.push(`<li class="warn">${esc(i.problem)}</li>`));
  if (!items.length) items.push(d.rechecked ? '<li>Length, sound, picture and sentence edges fine.</li>'
    : '<li>Text clear of faces, words spelled right, sound and length fine.</li>');
  return `<div class="doc-report"><div class="head"><span>Clip doctor: ${head}</span>${d.postable ? `<span>${d.postable}/10</span>` : ''}</div>
    <ul>${items.join('')}</ul>${d.best ? `<div class="hint" style="margin-top:8px">Best thing about it: ${esc(d.best)}</div>` : ''}
    ${d.rechecked ? '<div class="hint" style="margin-top:8px">Measured again after your edit. Claude’s look at the picture was of the earlier version.</div>' : ''}</div>`;
}

/* ---------- caption grouping (mirrors captions.py) ---------- */
function groupWords(words) {
  const { max_words, max_chars, max_gap } = CONFIG.caption_grouping;
  const lines = []; let line = [];
  for (const w of words) {
    if (!w.w) continue;
    if (line.length) {
      const gap = w.start - line[line.length - 1].end;
      const tooLong = line.map(x => x.w).join(' ').length + w.w.length > max_chars;
      const ended = /[.!?]$/.test(line[line.length - 1].w);
      if (gap > max_gap || tooLong || line.length >= max_words || ended) { lines.push(line); line = []; }
    }
    line.push(w);
  }
  if (line.length) lines.push(line);
  return lines;
}

let EDIT = { words: [], lines: [], wave: null, anim: null, mode: 'render' };
function buildStyleChips() {
  $('ed-styles').innerHTML = CONFIG.caption_styles.map(s =>
    `<button type="button" class="chip" data-style="${s.id}"><span class="swatch" style="background:${s.active}"></span>${esc(s.label)}</button>`).join('');
  $('ed-styles').querySelectorAll('.chip').forEach(chip => chip.addEventListener('click', () => {
    $('ed-styles').querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
    chip.classList.add('on');
    setMode('style'); paintOverlay();
  }));
}
function buildLayoutChips() {
  $('ed-layouts').innerHTML = CONFIG.layouts.map(l => `<button type="button" class="chip" data-layout="${l.id}">${esc(l.label)}</button>`).join('');
  $('ed-layouts').querySelectorAll('.chip').forEach(chip => chip.addEventListener('click', () => {
    $('ed-layouts').querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
    chip.classList.add('on');
    toggleFraming(chip.dataset.layout);
  }));
}
function toggleFraming(layout) {
  // Auto shows the controls for what the last render actually used (a facecam split, the whole picture…)
  const fr = currentClip?.framing || {};
  const resolved = layout !== 'auto' ? layout : (fr.layout || (fr.kind === 'facecam' ? 'split' : 'fill'));
  $('ed-cropwrap').classList.toggle('hidden', resolved !== 'fill');
  $('ed-camwrap').classList.toggle('hidden', resolved !== 'split');
}
const chosen = (sel, attr, fallback) => document.querySelector(`${sel} .chip.on`)?.dataset[attr] || fallback;
const select = (sel, attr, value) => document.querySelectorAll(`${sel} .chip`).forEach(c => c.classList.toggle('on', c.dataset[attr] === value));
const cropLabel = v => v < .34 ? 'left' : v > .66 ? 'right' : 'centre';

function edPanel(name, tapped = false) {
  document.querySelectorAll('.ed-rail button').forEach(b => b.classList.toggle('on', b.dataset.panel === name));
  document.querySelectorAll('.ed-sec').forEach(s => s.classList.toggle('hidden', s.dataset.panel !== name));
  // On a phone the panel sits under the preview: bring it up so the tap visibly does something.
  if (tapped && matchMedia('(max-width: 820px)').matches) {
    const ed = $('drawer'), rail = document.querySelector('.ed-rail');
    ed.scrollTo({ top: rail.offsetTop - document.querySelector('.ed-top').offsetHeight, behavior: 'smooth' });
  }
}
document.querySelectorAll('.ed-rail button').forEach(b => b.addEventListener('click', () => edPanel(b.dataset.panel, true)));

async function openEditor(clip, panel = 'ask') {
  if (!clip) return;
  currentClip = clip;
  const e = clip.edits || {};
  $('ed-title').textContent = clip.hook || clip.title;
  $('ed-badge').textContent = `#${clip.rank}${clip.score ? ` · ${clip.score}` : ''}`;
  $('ed-video').poster = clip.thumb_url || '';
  $('ed-video').src = `${clip.video_url}?v=${Date.now()}`;
  $('ed-note').textContent = clip.reason || '';
  const card = (e.cards || [])[0];
  $('ed-cardblock').classList.toggle('hidden', !card);
  $('ed-hookblock').classList.toggle('hidden', !!card);
  $('ed-headlineblock').classList.toggle('hidden', !!card);
  if (card) {
    $('ed-cardhead').textContent = ({ label: 'Headline label — over the whole clip', title: 'Title bar — over the whole clip', bubble: 'Comment bubble — over the whole clip' })[card.kind] || 'On-screen text';
    $('ed-card').value = card.text || '';
    $('ed-cardon').checked = true;
  }
  $('ed-hook').value = card ? '' : (clip.hook || '');
  $('ed-hookon').checked = e.hook_on !== false;
  $('ed-headline').value = e.headline ?? clip.headline ?? '';
  $('ed-headlineon').checked = e.headline_on !== false;
  $('ed-stylewhy').textContent = e.style ? `Look: ${STYLE_NAME[e.style] || e.style}.${e.style_why ? ' ' + e.style_why : ''}${card && card.kind === 'title' ? ' Put [brackets] around the word to show in yellow.' : ''}` : '';
  const table = judgeTable(clip);
  $('ed-judge').innerHTML = table; $('ed-judge').classList.toggle('hidden', !table);
  $('ed-doctor').innerHTML = campaignCheckHTML(clip) + doctorReport(clip);
  const stitched = (clip.parts || []).length > 1;
  $('ed-trimblock').classList.toggle('hidden', stitched);
  $('ed-partsblock').classList.toggle('hidden', !stitched);
  if (stitched) {
    $('ed-partshead').textContent = `Stitched from ${clip.parts.length} parts of the video`;
    $('ed-parts').innerHTML = clip.parts.map(p => `<div class="part"><span class="role">${esc(p.role || '')}</span>
      <span>${fmt(p.start)}–${fmt(p.end)} (${(p.end - p.start).toFixed(1)} s)</span>${p.label ? `<span class="lbl">${esc(p.label)}</span>` : ''}</div>`).join('');
  }
  $('ed-capon').checked = e.captions_on !== false;
  $('ed-tighten').checked = e.tighten !== false;
  $('ed-motion').checked = e.motion !== false;
  delete $('ed-crop').dataset.touched;
  $('ed-pos').value = e.caption_position || 'bottom';
  $('ed-size').value = e.caption_size || 1;
  $('ed-sizeval').textContent = `${Math.round((e.caption_size || 1) * 100)}%`;
  $('ed-crop').value = e.crop_x ?? 0.5;
  $('ed-cropval').textContent = cropLabel(e.crop_x ?? 0.5);
  $('ed-caption').value = clip.caption || '';
  $('ed-tags').value = (clip.hashtags || []).join(' ');
  $('ed-text').value = (clip.words || []).map(w => w.w).join(' ');
  $('ed-framenote').textContent = clip.framing?.note || '';
  $('ed-savednote').textContent = clip.saved > 0 ? `The last render cut ${clip.saved.toFixed(1)} s of dead air.` : '';
  const cam = (e.facecam_manual && e.facecam) || clip.framing?.facecam || e.facecam || { x: 0, y: 0, w: .28, h: .30 };
  $('cam-x').value = Math.round(cam.x * 100); $('cam-y').value = Math.round(cam.y * 100);
  $('cam-w').value = Math.round(cam.w * 100); $('cam-h').value = Math.round(cam.h * 100);
  ['cam-x', 'cam-y', 'cam-w', 'cam-h'].forEach(id => delete $(id).dataset.touched);
  select('#ed-styles', 'style', e.caption_style || 'impact');
  select('#ed-layouts', 'layout', e.layout || 'auto');
  toggleFraming(e.layout || 'auto');
  EDIT.words = clip.words || [];
  EDIT.lines = groupWords(EDIT.words);
  setMode('render');
  setBackdrop(clip);
  const camp = currentJob && currentJob.campaign;
  $('ed-campaign').classList.toggle('hidden', !camp);
  if (camp) {
    const notes = (camp.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
    $('ed-campaign').innerHTML = `<b>${esc(camp.name)}</b> — this clip follows the campaign's rules, and re-rendering checks it again.${notes ? `<ul>${notes}</ul>` : ''}`;
  }
  edPanel(panel);
  $('ed-undo').classList.toggle('hidden', !clip.can_undo);
  renderEdAskLog();
  $('drawer').classList.remove('hidden');
  $('drawer').scrollTop = 0;
  overlayOpen();
  document.body.style.overflow = 'hidden';
  TRIM = { start: clip.start, end: clip.end };
  EDIT.wave = null;
  $('ed-render').disabled = false; $('ed-render').title = '';
  if (stitched) return;
  drawTimeline();
  try {
    EDIT.wave = await api(`/api/clips/${clip.id}/waveform`);
    if (EDIT.wave.missing) {
      $('ed-render').disabled = true;
      $('ed-render').title = 'The original video isn’t on this PC any more — run the video again to edit its clips';
    }
    setTrim(clip.start, clip.end, false);
    drawTimeline(); renderWordStrip();
  } catch { /* the timeline is a nicety, not a blocker */ }
}
function closeEditor(fromHistory = false) {
  if ($('drawer').classList.contains('hidden')) return;
  stopAnim(); $('ed-video').pause(); $('drawer').classList.add('hidden'); document.body.style.overflow = '';
  if (!fromHistory) setTimeout(overlayGone, 0);
}
$('ed-close').addEventListener('click', () => closeEditor());

/* The editor and the post kit sit on top of the video page. The phone's or
   browser's Back closes them, rather than leaving the video. */
let OVERLAY_HASH = null, IGNORE_POP = 0;
function overlayOpen() {
  if (OVERLAY_HASH !== null) return;
  OVERLAY_HASH = location.hash;
  history.pushState({ caOverlay: 1 }, '', location.href);
}
function overlayGone() {
  if (OVERLAY_HASH === null || !$('drawer').classList.contains('hidden') || !$('kit').classList.contains('hidden')) return;
  const was = OVERLAY_HASH; OVERLAY_HASH = null;
  if (history.state && history.state.caOverlay && location.hash === was) { IGNORE_POP++; history.back(); }
}
window.addEventListener('popstate', ev => {
  if (IGNORE_POP) { IGNORE_POP--; return; }
  if (OVERLAY_HASH !== null && location.hash === OVERLAY_HASH && !(ev.state && ev.state.caOverlay)) {
    OVERLAY_HASH = null; closeEditor(true); closeKit(true);
  }
});

/* ---------- timeline ---------- */
let TRIM = { start: 0, end: 0 };
function setTrim(start, end, redraw = true) {
  const w = EDIT.wave;
  if (w) { start = Math.max(w.start, Math.min(start, w.end - 1)); end = Math.min(w.end, Math.max(end, start + 1)); }
  TRIM = { start, end };
  $('tl-range').textContent = `${fmt(start)}–${fmt(end)} · ${(end - start).toFixed(1)} s`;
  if (redraw) { drawTimeline(); renderWordStrip(); }
}
function timeToX(t, width) { const w = EDIT.wave; return w ? ((t - w.start) / (w.end - w.start)) * width : 0; }
function xToTime(x, width) { const w = EDIT.wave; return w.start + (x / width) * (w.end - w.start); }
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function drawTimeline() {
  const canvas = $('tl-canvas');
  const box = canvas.parentElement.getBoundingClientRect();
  const width = Math.max(320, box.width), H = 96;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr; canvas.height = H * dpr;
  canvas.style.width = width + 'px'; canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, H);
  const w = EDIT.wave;
  if (!w || !w.points.length) {
    ctx.fillStyle = cssVar('--muted'); ctx.font = '13px "Plus Jakarta Sans", sans-serif';
    const msg = !w ? 'Loading the sound wave…' : w.missing
      ? 'The original video isn’t on this PC any more, so this clip can’t be trimmed or re-rendered.'
      : 'No sound wave for this clip';
    const lines = [''];             // wrapped to the timeline's width
    msg.split(' ').forEach(word => {
      const tryLine = lines[lines.length - 1] ? `${lines[lines.length - 1]} ${word}` : word;
      if (ctx.measureText(tryLine).width > width - 28 && lines[lines.length - 1]) lines.push(word);
      else lines[lines.length - 1] = tryLine;
    });
    lines.forEach((ln, i) => ctx.fillText(ln, 14, H / 2 + 4 + (i - (lines.length - 1) / 2) * 18));
    $('tl-select').style.display = 'none';
    return;
  }
  const n = w.points.length, barW = width / n, inC = cssVar('--accent'), outC = cssVar('--line-2');
  for (let i = 0; i < n; i++) {
    const t = w.start + (i / n) * (w.end - w.start);
    const h = Math.max(2, w.points[i] * (H - 16));
    ctx.fillStyle = t >= TRIM.start && t <= TRIM.end ? inC : outC;
    ctx.fillRect(i * barW, H / 2 - h / 2, Math.max(1, barW - 0.6), h);
  }
  const sx = timeToX(TRIM.start, width), ex = timeToX(TRIM.end, width);
  $('tl-select').style.display = '';
  $('tl-select').style.left = `${sx}px`;
  $('tl-select').style.width = `${Math.max(8, ex - sx)}px`;
}
function renderWordStrip() {
  const w = EDIT.wave;
  if (!w) return;
  $('tl-words').innerHTML = w.words.map(word =>
    `<span class="w ${word.start >= TRIM.start && word.end <= TRIM.end ? 'in' : ''}" data-start="${word.start}" data-end="${word.end}">${esc(word.w)}</span>`).join(' ');
  $('tl-words').querySelectorAll('.w').forEach(el => el.addEventListener('click', () => {
    const start = +el.dataset.start, end = +el.dataset.end;
    if (Math.abs(start - TRIM.start) <= Math.abs(end - TRIM.end)) setTrim(Math.max(0, start - 0.15), TRIM.end);
    else setTrim(TRIM.start, end + 0.2);
  }));
}
(function timelineDrag() {
  const tl = $('tl');
  let dragging = null;
  const width = () => tl.getBoundingClientRect().width;
  tl.addEventListener('pointerdown', (ev) => {
    if (!EDIT.wave) return;
    const handle = ev.target.closest('.tl-handle');
    if (handle) { dragging = handle.dataset.edge; tl.setPointerCapture(ev.pointerId); return; }
    const t = xToTime(ev.clientX - tl.getBoundingClientRect().left, width());
    dragging = Math.abs(t - TRIM.start) < Math.abs(t - TRIM.end) ? 'start' : 'end';
    move(ev); tl.setPointerCapture(ev.pointerId);
  });
  const move = (ev) => {
    if (!dragging || !EDIT.wave) return;
    const t = xToTime(ev.clientX - tl.getBoundingClientRect().left, width());
    if (dragging === 'start') setTrim(Math.min(t, TRIM.end - 1), TRIM.end);
    else setTrim(TRIM.start, Math.max(t, TRIM.start + 1));
  };
  tl.addEventListener('pointermove', move);
  ['pointerup', 'pointercancel'].forEach(e => tl.addEventListener(e, () => { dragging = null; }));
  window.addEventListener('resize', () => { if (!$('drawer').classList.contains('hidden')) drawTimeline(); });
})();
$('ed-video').addEventListener('timeupdate', () => {
  const w = EDIT.wave, video = $('ed-video'), head = $('tl-playhead');
  if (!w || !currentClip) return;
  if (currentClip.saved > 0 || (currentClip.parts || []).length > 1) { head.classList.add('hidden'); return; }
  head.classList.remove('hidden');
  head.style.left = `${timeToX(currentClip.start + video.currentTime, $('tl').getBoundingClientRect().width)}px`;
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') { if (!$('kit').classList.contains('hidden')) closeKit(); else closeEditor(); return; }
  if ($('drawer').classList.contains('hidden') || /input|textarea|select/i.test(ev.target.tagName)) return;
  if (!currentClip || (currentClip.parts || []).length > 1) return;
  const at = currentClip.start + $('ed-video').currentTime;
  if (ev.key === '[') { setTrim(at, TRIM.end); toast(`Starts at ${fmt(at)}`); }
  if (ev.key === ']') { setTrim(TRIM.start, at); toast(`Ends at ${fmt(at)}`); }
});

/* ---------- live caption preview ---------- */
function setMode(mode) {
  EDIT.mode = mode;
  document.querySelectorAll('.ptab').forEach(t => t.classList.toggle('on', t.dataset.mode === mode));
  $('ed-video').classList.toggle('faded', mode === 'style');
  $('ed-overlay').classList.toggle('hidden', mode !== 'style');
  $('ov-scrubwrap').classList.toggle('hidden', mode !== 'style');
  if (currentClip) setBackdrop(currentClip);
  if (mode === 'style') { $('ed-video').pause(); buildScrub(); paintOverlay(); startAnim(); }
  else stopAnim();
}
document.querySelectorAll('.ptab').forEach(t => t.addEventListener('click', () => setMode(t.dataset.mode)));
function buildScrub() {
  EDIT.lines = groupWords(editorWords());
  const scrub = $('ov-scrub');
  scrub.max = Math.max(0, EDIT.lines.length - 1);
  scrub.value = Math.min(+scrub.value, +scrub.max);
  $('ov-scrubval').textContent = `line ${(+scrub.value) + 1} of ${EDIT.lines.length || 1}`;
}
$('ov-scrub').addEventListener('input', () => { buildScrub(); paintOverlay(); });
function editorWords() {
  const typed = $('ed-text').value.trim(), base = currentClip?.words || [];
  return typed ? retimeWords(base, typed) : base;
}
function startAnim() { stopAnim(); let i = 0; EDIT.anim = setInterval(() => paintOverlay(i++), 420); }
function stopAnim() { if (EDIT.anim) clearInterval(EDIT.anim); EDIT.anim = null; }
function paintOverlay(tick = 0) {
  if (EDIT.mode !== 'style') return;
  const style = CONFIG.caption_styles.find(s => s.id === chosen('#ed-styles', 'style', 'impact'));
  if (!style) return;
  const stack = document.querySelector('.preview-stack');
  const scale = stack.clientWidth / CONFIG.frame.w, H = stack.clientHeight;
  const accent = $('accent').dataset.on === '1' ? $('accent').value : style.active;
  const line = EDIT.lines[+$('ov-scrub').value] || [];
  const active = line.length ? tick % line.length : 0;
  const size = style.size * (+$('ed-size').value) * scale;
  const capEl = $('ov-caption');
  const anton = style.font === 'Anton';
  capEl.style.font = `${anton ? 400 : 700} ${size}px ${anton ? '"Anton", Impact' : '"Poppins"'}, sans-serif`;
  capEl.style.textTransform = style.uppercase ? 'uppercase' : 'none';
  capEl.style.webkitTextStroke = style.box ? '0' : `${Math.max(1, style.outline_w * scale)}px ${style.outline}`;
  capEl.style.paintOrder = 'stroke fill';
  capEl.style.background = style.box ? 'rgba(0,0,0,0.6)' : 'transparent';
  capEl.style.padding = style.box ? `${6 * scale}px ${14 * scale}px` : '0';
  capEl.style.textShadow = style.box ? 'none' : `0 ${2 * scale}px ${4 * scale}px rgba(0,0,0,.8)`;
  capEl.innerHTML = line.map((w, i) => `<span style="color:${i === active ? accent : style.primary};display:inline-block;${
    i === active && style.pop ? 'transform:scale(1.1);' : ''}">${esc(w.w)}</span>`).join(' ');
  const pos = $('ed-pos').value, overlay = $('ed-overlay');
  overlay.style.justifyContent = pos === 'top' ? 'flex-start' : pos === 'middle' ? 'center' : 'flex-end';
  const margins = { bottom: 480, pop: 680 };
  capEl.style.marginBottom = margins[pos] ? `${(margins[pos] / CONFIG.frame.h) * H}px` : '0';
  capEl.style.marginTop = pos === 'top' ? `${(430 / CONFIG.frame.h) * H}px` : '0';
  const hook = $('ov-hook'), text = $('ed-hook').value.trim();
  hook.style.display = text && $('ed-hookon').checked && !$('ed-hookblock').classList.contains('hidden') ? 'block' : 'none';
  hook.textContent = style.uppercase ? text.toUpperCase() : text;
  hook.style.font = `${anton ? 400 : 700} ${size * 1.05}px ${anton ? '"Anton", Impact' : '"Poppins"'}, sans-serif`;
  hook.style.top = `${(200 / CONFIG.frame.h) * H}px`;
}
['ed-size', 'ed-pos', 'ed-hook', 'ed-hookon', 'ed-text'].forEach(id =>
  $(id).addEventListener('input', () => { if (EDIT.mode === 'style') { buildScrub(); paintOverlay(); } }));
function setBackdrop(clip) {
  const url = EDIT.mode === 'style' ? (clip.clean_url || clip.thumb_url) : clip.thumb_url;
  document.querySelector('.preview-stack').style.backgroundImage = url ? `url(${url}?v=${encodeURIComponent(clip.status)})` : 'none';
}
$('ed-size').addEventListener('input', ev => $('ed-sizeval').textContent = `${Math.round(ev.target.value * 100)}%`);
$('ed-crop').addEventListener('input', ev => { $('ed-cropval').textContent = cropLabel(+ev.target.value); ev.target.dataset.touched = '1'; });
['cam-x', 'cam-y', 'cam-w', 'cam-h'].forEach(id => $(id).addEventListener('input', () => { $(id).dataset.touched = '1'; }));
$('ed-download').addEventListener('click', () => {
  if (!currentClip) return;
  if (currentClip.compliance && currentClip.compliance.status === 'blocked') {
    toast('The campaign check blocked this clip — its post kit says why', true);
    const c = currentClip; closeEditor(); openKit(c); return;
  }
  location.href = `/api/clips/${currentClip.id}/download`;
});
$('ed-copy').addEventListener('click', () => {
  const tags = $('ed-tags').value.split(/\s+/).filter(Boolean).map(t => '#' + t.replace(/^#/, '')).join(' ');
  copyText(`${$('ed-caption').value}\n\n${tags}`.trim(), 'Caption and hashtags copied');
});
function retimeWords(original, text) {
  const tokens = text.trim().split(/\s+/).filter(Boolean);
  if (!tokens.length || !original.length) return [];
  if (tokens.length === original.length) return original.map((w, i) => ({ ...w, w: tokens[i] }));
  const first = original[0].start, last = original[original.length - 1].end, step = (last - first) / tokens.length;
  return tokens.map((t, i) => ({ w: t, start: +(first + i * step).toFixed(3), end: +(first + (i + 1) * step).toFixed(3) }));
}
/* The caption and hashtags save as you leave each box. */
async function saveClipText() {
  if (!currentClip) return;
  const caption = $('ed-caption').value, hashtags = $('ed-tags').value.split(/\s+/).filter(Boolean);
  if (caption === (currentClip.caption || '') && hashtags.map(t => t.replace(/^#/, '')).join(' ') === (currentClip.hashtags || []).join(' ')) return;
  try {
    const updated = await post(`/api/clips/${currentClip.id}/text`, { caption, hashtags });
    currentClip = { ...currentClip, caption: updated.caption, hashtags: updated.hashtags };
    const inJob = currentJob && currentJob.clips.find(c => c.id === currentClip.id);
    if (inJob) { inJob.caption = updated.caption; inJob.hashtags = updated.hashtags; }
    toast('Caption saved');
  } catch (err) { toast(err.message, true); }
}
$('ed-caption').addEventListener('change', saveClipText);
$('ed-tags').addEventListener('change', saveClipText);
$('ed-render').addEventListener('click', async () => {
  if (!currentClip) return;
  const done = busy($('ed-render'), 'Rendering…');
  const cards = (currentClip.edits || {}).cards || [];
  const payload = {
    start: TRIM.start || currentClip.start, end: TRIM.end || currentClip.end,
    headline: $('ed-headline').value.trim(), headline_on: $('ed-headlineon').checked && !cards.length,
    captions_on: $('ed-capon').checked, tighten: $('ed-tighten').checked,
    caption_style: chosen('#ed-styles', 'style', 'impact'), caption_position: $('ed-pos').value,
    caption_size: parseFloat($('ed-size').value), layout: chosen('#ed-layouts', 'layout', 'auto'),
    crop_x: parseFloat($('ed-crop').value), motion: $('ed-motion').checked,
    accent: $('accent').dataset.on === '1' ? $('accent').value : '',
    logo: $('opt-logo').checked, logo_corner: $('logocorner').value,
  };
  if (cards.length) payload.cards = $('ed-cardon').checked ? [{ ...cards[0], text: $('ed-card').value.trim() }] : [];
  else { payload.hook = $('ed-hook').value; payload.hook_on = $('ed-hookon').checked; }
  if ($('ed-crop').dataset.touched === '1') payload.crop_auto = false;
  if (['cam-x', 'cam-y', 'cam-w', 'cam-h'].some(id => $(id).dataset.touched === '1')) payload.facecam = {
    x: +$('cam-x').value / 100, y: +$('cam-y').value / 100, w: +$('cam-w').value / 100, h: +$('cam-h').value / 100 };
  const typed = $('ed-text').value.trim(), original = (currentClip.words || []).map(w => w.w).join(' ');
  const spanSame = (currentClip.parts || []).length > 1
    || (Math.abs(payload.start - currentClip.start) < 0.05 && Math.abs(payload.end - currentClip.end) < 0.05);
  if (typed && typed !== original && spanSame) payload.words = retimeWords(currentClip.words, typed);
  if ((currentClip.parts || []).length > 1) { delete payload.start; delete payload.end; }
  try {
    const doctorBefore = JSON.stringify(currentClip.doctor || null);
    await post(`/api/clips/${currentClip.id}/render`, payload);
    let updated = await waitForRender(currentClip.id);
    // the clip doctor measures the new version right after it renders — wait a moment for that report
    for (let i = 0; currentClip.doctor && i < 8 && JSON.stringify(updated.doctor || null) === doctorBefore; i++) {
      await new Promise(r => setTimeout(r, 1000));
      updated = await api(`/api/clips/${currentClip.id}`);
    }
    currentClip = updated;
    $('ed-undo').classList.toggle('hidden', !updated.can_undo);
    setMode('render');
    $('ed-video').src = `${updated.video_url}?v=${Date.now()}`;
    $('ed-video').poster = `${updated.thumb_url}?v=${Date.now()}`;
    setBackdrop(updated);
    $('ed-text').value = (updated.words || []).map(w => w.w).join(' ');
    $('ed-savednote').textContent = updated.saved > 0 ? `Cut ${updated.saved.toFixed(1)} s of dead air — ${(updated.duration - updated.saved).toFixed(1)} s long now.` : '';
    $('ed-doctor').innerHTML = campaignCheckHTML(updated) + doctorReport(updated);
    toast('Clip re-rendered');
    if (currentJob) watchJob(currentJob.id);
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});
async function waitForRender(clipId, timeout = 240000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    await new Promise(r => setTimeout(r, 1500));
    const clip = await api(`/api/clips/${clipId}`);
    if (clip.status === 'ready') {
      if (clip.render_error) throw new Error(`Couldn’t re-render — the clip is unchanged. ${clip.render_error}`);
      return clip;
    }
    if (clip.status === 'failed') throw new Error(clip.render_error || clip.reason || 'The render failed');
  }
  throw new Error('The render is taking too long — check back on the video page');
}

/* =====================================================================
   Tell ClipAgent what to change — typed, in your own words
   ===================================================================== */
const ASK_EXAMPLES = ['Clip 1: make it shorter, keep the punchline', 'Bigger captions on every clip',
  'Clip 2: change the top text to …', 'All clips: karaoke captions', 'Clip 3: start one sentence earlier'];
const ED_ASK_EXAMPLES = ['Shorter — keep the punchline', 'Start a sentence earlier', 'Bigger, yellow captions',
  'New top text: …', 'Turn the camera moves off'];
let ASK_SCOPE = new Set(), ASK_POLL = null, ASKS = [];

function fillExamples(box, list, target) {
  box.innerHTML = list.map(t => `<button type="button" class="ex">${esc(t)}</button>`).join('');
  box.querySelectorAll('.ex').forEach(b => b.addEventListener('click', () => {
    const el = $(target), t = b.textContent;
    el.value = el.value.trim() ? `${el.value.trim()} ${t}` : t;
    el.focus();
    const dots = el.value.indexOf('…');                     // put the cursor where the words go
    if (dots >= 0) el.setSelectionRange(dots, dots + 1);
  }));
}
fillExamples($('ask-examples'), ASK_EXAMPLES, 'ask-text');
fillExamples($('ed-ask-examples'), ED_ASK_EXAMPLES, 'ed-ask');

function renderAskBox(job) {
  const main = job.clips.filter(c => !c.alt_of && ['ready', 'rendering'].includes(c.status));
  const show = job.status === 'done' && main.length > 0;
  $('ask').classList.toggle('hidden', !show);
  if (!show) return;
  [...ASK_SCOPE].forEach(id => { if (!main.some(c => c.id === id)) ASK_SCOPE.delete(id); });
  const sig = main.map(c => c.id).join() + '|' + [...ASK_SCOPE].join();
  if ($('ask-chips').dataset.sig === sig) return;              // leave it alone while polling
  $('ask-chips').dataset.sig = sig;
  $('ask-chips').innerHTML = `<button type="button" class="chip ${ASK_SCOPE.size ? '' : 'on'}" data-id="">All clips</button>`
    + main.map(c => `<button type="button" class="chip ${ASK_SCOPE.has(c.id) ? 'on' : ''}" data-id="${c.id}">#${c.rank}</button>`).join('');
  $('ask-chips').querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
    if (!ch.dataset.id) ASK_SCOPE.clear();
    else if (ASK_SCOPE.has(ch.dataset.id)) ASK_SCOPE.delete(ch.dataset.id);
    else ASK_SCOPE.add(ch.dataset.id);
    renderAskBox(currentJob);
  }));
}

const ASK_STATE = { waiting: 'Waiting its turn', making: 'Making it…', done: 'Done', failed: 'Couldn’t change it' };
function askItemHTML(a, newest) {
  const clips = (a.clips || []).map(c => {
    const live = currentJob && currentJob.clips.find(x => x.id === c.id);
    const state = !c.remake ? (c.text_saved ? 'Caption saved' : (c.problems || []).length ? 'Not changed' : 'Nothing to change')
      : ASK_STATE[c.status] || c.status;
    const undo = newest && c.remake && c.status === 'done' && live && live.can_undo
      ? `<button type="button" class="undo-link" data-undo="${c.id}">Undo</button>` : '';
    return `<li class="${c.status}"><b>#${esc(c.label)}</b> <span class="st">${c.status === 'making' ? '<span class="spin"></span> ' : ''}${state}</span>${
      c.note ? ` — ${esc(c.note)}` : ''}${(c.problems || []).map(p => `<div class="prob">${esc(p)}</div>`).join('')}${undo}</li>`;
  }).join('');
  return `<div class="ask-item">
    <div class="you">${esc(a.text)}</div>
    <div class="ca">
      ${a.error ? `<p class="cant">${esc(a.error)}</p>` : ''}
      ${a.question ? `<p class="q">${esc(a.question)}</p>` : ''}
      ${a.understood ? `<p>${esc(a.understood)}</p>` : ''}
      ${(a.cant || []).map(c => `<p class="cant">${esc(c)}</p>`).join('')}
      ${clips ? `<ul class="ask-clips">${clips}</ul>` : ''}
      ${a.status === 'reading' ? '<p class="hint"><span class="spin"></span> Reading it…</p>' : ''}
    </div></div>`;
}
function wireUndoLinks(box) {
  box.querySelectorAll('[data-undo]').forEach(b => b.addEventListener('click', () => undoClip(b.dataset.undo)));
}
async function loadAsks(jobId) {
  try { ASKS = (await api(`/api/jobs/${jobId}/asks`)).asks || []; } catch (e) { return; }
  if (!currentJob || currentJob.id !== jobId) { /* the page may still be loading the clips */ }
  const shown = ASKS.slice(0, 4);
  $('ask-log').innerHTML = shown.length ? shown.map((a, i) => askItemHTML(a, i === 0)).join('')
    + (ASKS.length > 4 ? `<p class="hint">${ASKS.length - 4} earlier request${ASKS.length - 4 > 1 ? 's' : ''} not shown.</p>` : '') : '';
  wireUndoLinks($('ask-log'));
  renderEdAskLog();
  const live = ASKS.find(a => a.status === 'working' || a.status === 'reading');
  clearTimeout(ASK_POLL);
  if (live) ASK_POLL = setTimeout(() => loadAsks(jobId), 2500);
}
function renderEdAskLog() {
  if (!currentClip) return;
  const mine = ASKS.filter(a => (a.clips || []).some(c => c.id === currentClip.id)).slice(0, 2);
  $('ed-ask-log').innerHTML = mine.map((a, i) => askItemHTML(a, i === 0)).join('');
  wireUndoLinks($('ed-ask-log'));
}

async function sendAsk(body, button) {
  const done = busy(button, 'Reading…');
  try { return await post(`/api/jobs/${currentJob.id}/ask`, body); }
  catch (err) { toast(err.message, true); return null; }
  finally { done(); }
}
async function askFromPage() {
  if (!currentJob) return;
  const text = $('ask-text').value.trim();
  if (!text) { toast('Type what you’d like changed', true); $('ask-text').focus(); return; }
  const out = await sendAsk({ text, clip_ids: [...ASK_SCOPE] }, $('ask-go'));
  if (!out) return;
  $('ask-text').value = '';
  if (out.question) toast('ClipAgent has a question — see below');
  await loadAsks(currentJob.id);
  watchJob(currentJob.id);
}
$('ask-go').addEventListener('click', askFromPage);
$('ask-text').addEventListener('keydown', ev => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) askFromPage(); });
$('ask-text').addEventListener('input', () => autoGrow($('ask-text')));

async function askFromEditor() {
  if (!currentClip || !currentJob) return;
  const text = $('ed-ask').value.trim();
  if (!text) { toast('Type what you’d like changed on this clip', true); $('ed-ask').focus(); return; }
  const clip = currentClip;
  const out = await sendAsk({ text, focus: clip.id }, $('ed-ask-go'));
  if (!out) return;
  $('ed-ask').value = '';
  await loadAsks(currentJob.id);
  watchJob(currentJob.id);
  const mine = (out.clips || []).find(c => c.id === clip.id);
  if (!mine || !mine.remake) return;
  const done = busy($('ed-ask-go'), 'Making it…');
  try {
    let updated = await waitForRender(clip.id, 600000);
    await new Promise(r => setTimeout(r, 1500));          // the clip doctor's fresh measurements
    updated = await api(`/api/clips/${clip.id}`);
    if (currentClip && currentClip.id === clip.id && !$('drawer').classList.contains('hidden')) {
      await openEditor(updated, 'ask');
      setMode('render');
      $('ed-video').src = `${updated.video_url}?v=${Date.now()}`;
    }
    toast('Clip changed — press Undo if you liked it better before');
  } catch (err) { toast(err.message, true); }
  finally { done(); loadAsks(currentJob.id); }
}
$('ed-ask-go').addEventListener('click', askFromEditor);
$('ed-ask').addEventListener('keydown', ev => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) askFromEditor(); });

async function undoClip(clipId) {
  try {
    const updated = await post(`/api/clips/${clipId}/undo`, {});
    toast('Put back the version from before');
    if (currentClip && currentClip.id === clipId && !$('drawer').classList.contains('hidden')) {
      await openEditor(updated, document.querySelector('.ed-rail button.on')?.dataset.panel || 'ask');
      $('ed-video').src = `${updated.video_url}?v=${Date.now()}`;
    }
    if (currentJob) { watchJob(currentJob.id); loadAsks(currentJob.id); }
  } catch (err) { toast(err.message, true); }
}
$('ed-undo').addEventListener('click', () => { if (currentClip) undoClip(currentClip.id); });

/* =====================================================================
   Campaigns
   ===================================================================== */
let CAMP = { list: [], draft: null, brief: '', edits: {}, editingId: null, card: null, use: null, files: [], srcFile: null };
const PLAT = () => CONFIG.campaign.platforms;
const modeLabel = m => ({ overlay: 'Clip bank — the brand gives you finished clips; ClipAgent adds the hook and checks each one', source: 'Clip from footage — you cut your own clips out of longer videos' })[m] || (CONFIG.campaign.modes || {})[m] || m;

async function campHome() {
  try { const { campaigns } = await api('/api/campaigns'); CAMP.list = campaigns; }
  catch (err) { toast(err.message, true); }
  renderCampList();
}
function renderCampList() {
  const list = CAMP.list || [];
  const cards = list.map(c => {
    const pay = c.pay || {}, st = c.stats || {};
    const facts = [`<span class="tag">${c.mode === 'overlay' ? 'Clip bank' : 'Clip from footage'}</span>`]
      .concat((c.platforms || []).map(p => `<span class="tag">${esc(PLAT()[p] || p)}</span>`));
    if (pay.min_views) facts.push(`<span class="tag">Pays after ${short(+pay.min_views)} views</span>`);
    return `<div class="ccard" data-id="${c.id}">
      <div class="top"><div class="name">${esc(c.name)}</div>${pay.per_1k ? `<span class="rate">$${(+pay.per_1k).toFixed(2)} / 1K</span>` : ''}</div>
      <div class="facts">${facts.join('')}</div>
      <div class="nums"><div><b>${st.videos || 0}</b><span>${st.videos === 1 ? 'video' : 'videos'} made</span></div><div><b>${short(st.views || 0)}</b><span>views</span></div><div><b>${money$(st.earned || 0)}</b><span>earned</span></div></div>
      <div class="acts">
        <a class="btn small" href="#/campaign/${c.id}">Make clips</a>
        <a class="btn ghost small" href="#/campaign/${c.id}/rules">Rules</a>
        <button class="btn ghost small c-del" type="button" title="Delete this campaign">Delete</button>
      </div>
    </div>`;
  });
  cards.push(`<div class="ccard new" role="button" tabindex="0" id="camp-new-card"><b>New campaign</b><span>Paste a brief to set one up</span></div>`);
  $('camp-list').innerHTML = cards.join('');
  $('camp-new-card').addEventListener('click', () => { location.hash = '#/campaigns/new'; });
  $('camp-list').querySelectorAll('.c-del').forEach(btn => btn.addEventListener('click', async () => {
    const id = btn.closest('.ccard').dataset.id;
    if (btn.dataset.armed !== '1') { btn.dataset.armed = '1'; btn.textContent = 'Delete it?'; btn.classList.add('danger'); return; }
    const { campaigns } = await api(`/api/campaigns/${id}`, { method: 'DELETE' });
    CAMP.list = campaigns; campHome(); toast('Campaign deleted');
  }));
}
$('camp-new').addEventListener('click', () => { location.hash = '#/campaigns/new'; });
$('camp-cancel').addEventListener('click', () => { location.hash = '#/campaigns'; });
$('camp-read').addEventListener('click', async () => {
  const brief = $('camp-brief').value.trim();
  if (brief.length < 40) return toast('Paste the whole brief first', true);
  const done = busy($('camp-read'), 'Claude is reading it…');
  try {
    const r = await post('/api/campaigns/read', { brief });
    CAMP.draft = r.rulebook; CAMP.brief = r.brief; CAMP.card = r.card; CAMP.edits = {}; CAMP.editingId = null;
    location.hash = '#/campaigns/review';
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});
async function campOpen(id, view) {
  try {
    const c = await api(`/api/campaigns/${id}`);
    if (view === 'rules') {
      CAMP.draft = c.rulebook; CAMP.brief = c.brief; CAMP.card = c.card; CAMP.edits = {}; CAMP.editingId = c.id;
      renderRulebook();
    } else campUse(c);
  } catch (err) { toast(err.message, true); location.hash = '#/campaigns'; }
}

/* ---------- the rulebook ---------- */
const lines = v => (v || '').split('\n').map(s => s.trim()).filter(Boolean);
const quote = (q, line) => q ? `<span class="quote">“${esc(q.length > 160 ? q.slice(0, 160) + '…' : q)}”${line ? ` <i>line ${line}</i>` : ''}</span>` : '';
function renderRulebook() {
  const rb = CAMP.draft, card = CAMP.card, r = card.resolved;
  const capt = rb.caption || {}, pay = rb.pay || {}, len = rb.length || {}, postr = rb.posting || {};
  const listOf = items => (items || []).map(i => i.text).join('\n');
  const unverified = items => (items || []).filter(i => !i.verified).map(i => i.text);
  const ov = CAMP.edits.overrides || rb.overrides || {};
  const grey = (rb.grey || []).map(g => {
    const decided = ov[g.perm];
    return `<div class="grey ${decided ? 'decided' : ''}" data-perm="${g.perm}">
      <div><b>${esc(CONFIG.campaign.permissions[g.perm] || g.perm)}?</b> ${esc(g.question)}</div>
      ${quote(g.quote, g.line)}
      <div class="btnrow"><button type="button" class="btn small ghost g-yes ${decided === 'yes' ? 'picked' : ''}" aria-pressed="${decided === 'yes'}">Allow it</button>
        <button type="button" class="btn ghost small g-no ${decided === 'no' ? 'picked' : ''}" aria-pressed="${decided === 'no'}">Keep it off</button>
        <span class="hint g-state">${decided === 'yes' ? 'You allowed it' : decided === 'no' ? 'You kept it off' : ''}</span></div>
    </div>`;
  }).join('');
  const posting = [postr.public && 'Posts must be public', postr.comments_on && 'Likes and comments on', postr.no_paid_boost && 'No paid boosting',
    postr.no_duplicates && 'Each post once per account', postr.collab === 'yes' && 'Collab posts allowed'].filter(Boolean);
  $('camp-card').innerHTML = `
    <div class="rulebook">
      <h1 style="margin-bottom:6px">${CAMP.editingId ? 'Campaign rules' : 'Check what Claude found'}</h1>
      <p class="hint" style="margin-bottom:18px">${CAMP.editingId ? 'Change anything that’s wrong. Every clip made for this campaign follows these.' : 'Read it over and fix anything wrong. Nothing is saved until you press Save campaign.'}</p>
      <div class="rb-head">
        <div class="field"><label for="rb-name">Name</label><input id="rb-name" type="text" value="${esc(rb.name)}"></div>
        <div class="field"><label for="rb-mode">Kind of campaign</label><select id="rb-mode">${
          Object.keys(CONFIG.campaign.modes).map(k => `<option value="${k}" ${rb.mode === k ? 'selected' : ''}>${k === 'overlay' ? 'Clip bank — post the brand’s clips whole' : 'Clip from footage — cut your own clips'}</option>`).join('')}</select></div>
      </div>
      ${rb.mode_quote && rb.mode_quote.quote ? `<div>${quote(rb.mode_quote.quote, rb.mode_quote.line)}</div>` : ''}
      ${(rb.notes || []).length ? `<div class="rb-warn">${rb.notes.map(n => `<div>${esc(n)}</div>`).join('')}</div>` : ''}
      ${grey ? `<h5>The brief isn't clear about these — they stay off until you decide</h5>${grey}` : ''}
      <h5>What you may do to the footage</h5>
      <div id="rb-perms"></div>
      <div class="rb-grid">
        <div>
          <h5>The caption must include one of these lines</h5>
          <textarea id="rb-oneof" rows="4" placeholder="One line per row, copied exactly">${esc(listOf(capt.one_of))}</textarea>
          ${unverified(capt.one_of).length ? `<div class="hint warnnote">Not in the brief word for word, so left out of captions: ${unverified(capt.one_of).map(esc).join(', ')}</div>` : ''}
          <h5>Required hashtags</h5>
          <input id="rb-tags" type="text" value="${esc((capt.hashtags || []).map(t => t.text).join(' '))}" placeholder="#BrandPartner">
          <label class="toggle small"><input id="rb-owntags" type="checkbox" ${capt.other_hashtags === 'yes' ? 'checked' : ''}><span>Let me add my own hashtags too ${capt.other_hashtags === 'unstated' ? '(the brief doesn’t say)' : capt.other_hashtags === 'no' ? '(the brief says no)' : ''}</span></label>
          ${(r.mentions || []).length ? `<div class="hint">Must tag: ${r.mentions.map(esc).join(' ')}</div>` : ''}
        </div>
        <div>
          <h5>Hooks the brand gave (used first)</h5>
          <textarea id="rb-hooks" rows="4" placeholder="None in the brief — Claude writes them inside its rules">${esc(listOf((rb.hooks || {}).examples))}</textarea>
          ${(r.caption_examples || []).length ? `<h5>Example captions (for style)</h5><div class="hint">${r.caption_examples.map(esc).join(' / ')}</div>` : ''}
          ${(r.tone_avoid || []).length ? `<h5>Never write about</h5><div class="hint">${r.tone_avoid.map(esc).join(', ')}</div>` : ''}
          ${(r.hook_rules || []).length ? `<h5>On-screen text rules</h5><div class="hint">${r.hook_rules.map(esc).join(' ')}</div>` : ''}
        </div>
        <div>
          <h5>Length</h5>
          <div class="row tight">
            <div class="field"><label for="rb-min">Shortest (s)</label><input id="rb-min" type="number" min="0" value="${len.min ?? ''}"></div>
            <div class="field"><label for="rb-max">Longest (s)</label><input id="rb-max" type="number" min="0" value="${len.max ?? ''}"></div>
          </div>
          ${len.quote ? `<div>${quote(len.quote, len.line)}</div>` : ''}
          <label class="toggle small"><input id="rb-full" type="checkbox" ${(rb.full_clip || {}).value ? 'checked' : ''}><span>Post each clip whole, start to end</span></label>
          <h5>The brand's logo on screen</h5>
          <select id="rb-logo">${Object.entries(card.brand_logo_options || {}).map(([k, v]) => `<option value="${k}" ${((rb.brand_logo || {}).value || 'unstated') === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>
          ${(rb.brand_logo || {}).quote ? `<div>${quote(rb.brand_logo.quote, rb.brand_logo.line)}</div>` : ''}
          <h5>Pay — this drives the Money page</h5>
          <div class="row tight">
            <div class="field"><label for="rb-cpm">$ per 1K views</label><input id="rb-cpm" type="number" step="0.01" min="0" value="${pay.per_1k ?? ''}"></div>
            <div class="field"><label for="rb-minv">Pays after</label><input id="rb-minv" type="number" min="0" value="${pay.min_views ?? ''}" placeholder="views"></div>
            <div class="field"><label for="rb-maxp">Max $ a post</label><input id="rb-maxp" type="number" min="0" value="${pay.max_per_post ?? ''}"></div>
          </div>
          <h5>Platforms</h5>
          <div id="rb-plats" class="chips">${Object.entries(PLAT()).map(([k, v]) => `<button type="button" class="chip ${(rb.platforms || []).includes(k) ? 'on' : ''}" data-p="${k}">${esc(v)}</button>`).join('')}</div>
          ${posting.length ? `<h5>Posting rules</h5><div class="hint">${posting.join('. ')}.</div>` : ''}
        </div>
      </div>
      ${(rb.other_rules || []).length ? `<h5>Everything else the brief says</h5><ul class="rb-other">${rb.other_rules.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      <div class="btnrow" style="margin-top:22px">
        <button id="rb-save" type="button" class="btn">${CAMP.editingId ? 'Save changes' : 'Save campaign'}</button>
        <button id="rb-discard" type="button" class="btn ghost">${CAMP.editingId ? 'Back' : 'Discard'}</button>
      </div>
    </div>`;
  renderPerms();
  $('camp-card').querySelectorAll('.grey').forEach(el => {
    const perm = el.dataset.perm;
    const set = v => {
      CAMP.edits.overrides = { ...(CAMP.edits.overrides || CAMP.draft.overrides || {}), [perm]: v };
      el.classList.add('decided');
      el.querySelector('.g-state').textContent = v === 'yes' ? 'You allowed it' : 'You kept it off';
      el.querySelector('.g-yes').classList.toggle('picked', v === 'yes'); el.querySelector('.g-yes').setAttribute('aria-pressed', v === 'yes');
      el.querySelector('.g-no').classList.toggle('picked', v === 'no'); el.querySelector('.g-no').setAttribute('aria-pressed', v === 'no');
      previewCard();
    };
    el.querySelector('.g-yes').addEventListener('click', () => set('yes'));
    el.querySelector('.g-no').addEventListener('click', () => set('no'));
  });
  $('rb-plats').querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => ch.classList.toggle('on')));
  $('rb-mode').addEventListener('change', () => previewCard());
  $('rb-save').addEventListener('click', saveRulebook);
  $('rb-discard').addEventListener('click', () => { location.hash = CAMP.editingId ? `#/campaign/${CAMP.editingId}` : '#/campaigns'; });
}
function renderPerms() {
  const rows = CAMP.card.permissions, ov = CAMP.edits.overrides || CAMP.draft.overrides || {};
  $('rb-perms').innerHTML = `<table class="perms">${rows.map(p => `
    <tr><td class="pl">${esc(p.label)}</td>
      <td><span class="perm-pill ${p.allowed ? 'yes' : 'no'}">${p.allowed ? 'Allowed' : 'Not allowed'}</span></td>
      <td class="why">${esc(p.why)} ${quote(p.quote, p.line)}</td>
      <td><select data-k="${p.key}" aria-label="${esc(p.label)}">
        <option value="" ${!ov[p.key] ? 'selected' : ''}>Go by the brief</option>
        <option value="yes" ${ov[p.key] === 'yes' ? 'selected' : ''}>Allow</option>
        <option value="no" ${ov[p.key] === 'no' ? 'selected' : ''}>Don't allow</option></select></td></tr>`).join('')}</table>`;
  $('rb-perms').querySelectorAll('select').forEach(sel => sel.addEventListener('change', () => {
    const o = { ...(CAMP.edits.overrides || CAMP.draft.overrides || {}) };
    if (sel.value) o[sel.dataset.k] = sel.value; else delete o[sel.dataset.k];
    CAMP.edits.overrides = o;
    previewCard();
  }));
}
function collectEdits() {
  const num = id => $(id).value === '' ? null : +$(id).value;
  return {
    ...CAMP.edits, overrides: CAMP.edits.overrides || CAMP.draft.overrides || {},
    name: $('rb-name').value.trim(), mode: $('rb-mode').value,
    pay: { per_1k: num('rb-cpm'), min_views: num('rb-minv'), max_per_post: num('rb-maxp') },
    length: { min: num('rb-min'), max: num('rb-max') },
    full_clip: $('rb-full').checked, brand_logo: $('rb-logo') ? $('rb-logo').value : undefined,
    platforms: [...$('rb-plats').querySelectorAll('.chip.on')].map(c => c.dataset.p),
    caption: {
      one_of: lines($('rb-oneof').value), hashtags: $('rb-tags').value.split(/[\s,]+/).filter(Boolean),
      other_hashtags: $('rb-owntags').checked ? 'yes' : ((CAMP.draft.caption || {}).other_hashtags === 'yes' ? 'no' : (CAMP.draft.caption || {}).other_hashtags || 'unstated'),
    },
    hook_examples: lines($('rb-hooks').value),
  };
}
async function previewCard() {
  try {
    const r = await post('/api/campaigns/preview', { rulebook: CAMP.draft, edits: { overrides: CAMP.edits.overrides || {}, mode: $('rb-mode').value }, brief: CAMP.brief });
    CAMP.card = r.card; renderPerms();
  } catch (err) { toast(err.message, true); }
}
async function saveRulebook() {
  const edits = collectEdits();
  if (!edits.name) return toast('Give the campaign a name', true);
  const done = busy($('rb-save'), 'Saving…');
  try {
    const saved = CAMP.editingId
      ? await api(`/api/campaigns/${CAMP.editingId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ edits }) })
      : await post('/api/campaigns', { brief: CAMP.brief, rulebook: CAMP.draft, edits });
    toast(CAMP.editingId ? 'Rules saved' : 'Campaign saved');
    CAMP.draft = null;
    location.hash = `#/campaign/${saved.id}`;
  } catch (err) { toast(err.message, true); }
  finally { done(); }
}

/* ---------- making clips for a campaign ---------- */
function ruleChips(card) {
  const r = card.resolved, chips = [];
  if (r.full_clip) chips.push(['Posted whole', 'no']);
  const NO = { crop: 'No cropping', zoom: 'No zooms or camera moves', cut: 'No cuts inside a clip', audio: 'Leave the sound as it is',
    stitch: 'No joining moments', music: 'No added music', outside: 'No outside footage', speed: 'No speed changes',
    watermark: r.brand_logo === 'forbidden' ? 'No logos or watermarks' : 'No watermark of your own' };
  card.permissions.filter(p => !p.allowed && NO[p.key]).forEach(p => chips.push([NO[p.key], 'no']));
  if (r.min_len) chips.push([`At least ${r.min_len} s`, '']);
  if (r.max_len) chips.push([`At most ${r.max_len} s`, '']);
  (r.hashtags || []).forEach(t => chips.push([t.startsWith('#') ? t : '#' + t, 'hash']));
  if ((r.one_of || []).length) chips.push([`${r.one_of.length} required caption line${r.one_of.length > 1 ? 's' : ''}`, 'hash']);
  if (r.brand_logo === 'required') chips.push(['Brand logo on screen', '']);
  if (r.brand_logo === 'forbidden' && !chips.some(([t]) => t === 'No logos or watermarks')) chips.push(['No logos', 'no']);
  if (r.posting && r.posting.no_duplicates) chips.push(['Once per account', '']);
  const pay = r.pay || {};
  if (pay.per_1k) chips.push([`$${(+pay.per_1k).toFixed(2)} per 1K views`, 'pay']);
  if (pay.min_views) chips.push([`Pays after ${(+pay.min_views).toLocaleString()} views`, 'pay']);
  if (pay.max_per_post) chips.push([`Max $${(+pay.max_per_post).toLocaleString()} a post`, 'pay']);
  return chips.map(([t, k]) => `<span class="rule-chip ${k}">${esc(t)}</span>`).join('');
}
function renderLogo(c) {
  const need = (c.card.resolved || {}).brand_logo || 'unstated';
  const bl = (c.rulebook || {}).brand_logo || {};
  const has = !!c.logo;
  $('cu-logo').classList.toggle('hidden', need === 'forbidden' || (need === 'unstated' && !has && c.mode === 'overlay'));
  $('cu-logo-thumb').classList.toggle('hidden', !has);
  if (has) $('cu-logo-img').src = c.logo.url;
  $('cu-logo-add').textContent = has ? 'Replace' : 'Add logo';
  $('cu-logo-del').classList.toggle('hidden', !has);
  $('cu-logo').classList.toggle('need', need === 'required' && !has);
  $('cu-logo-need').textContent = {
    required: has ? 'Required by the brief — it goes on every clip.' : 'Required by the brief — add the logo file before making clips.',
    allowed: has ? 'Goes on every clip.' : 'The brief allows it. Add one if you have the file.',
    forbidden: "The brief doesn't allow logos.", unstated: has ? 'Goes on every clip.' : 'Optional — only if the brand gives you a logo file.',
  }[need];
  const more = [];
  if (bl.quote && need !== 'unstated') more.push(`“${bl.quote.length > 120 ? bl.quote.slice(0, 120) + '…' : bl.quote}”`);
  if ((bl.rules || []).length) more.push(bl.rules.join(' '));
  $('cu-logo-more').innerHTML = more.map(esc).join(' ') + (bl.link && !has ? ` Get it here: <a href="${esc(bl.link)}" target="_blank" rel="noopener">${esc(bl.link)}</a>` : '');
}
$('cu-logo-add').addEventListener('click', () => $('cu-logo-file').click());
$('cu-logo-file').addEventListener('change', async ev => {
  const f = ev.target.files[0]; ev.target.value = '';
  if (!f || !CAMP.use) return;
  const form = new FormData(); form.append('file', f);
  try { const c = await api(`/api/campaigns/${CAMP.use.id}/logo`, { method: 'POST', body: form }); CAMP.use = { ...CAMP.use, logo: c.logo }; renderLogo(CAMP.use); toast('Logo added — it goes on every clip'); }
  catch (err) { toast(err.message, true); }
});
$('cu-logo-del').addEventListener('click', async () => {
  if (!CAMP.use) return;
  try { const c = await api(`/api/campaigns/${CAMP.use.id}/logo`, { method: 'DELETE' }); CAMP.use = { ...CAMP.use, logo: c.logo }; renderLogo(CAMP.use); toast('Logo removed'); }
  catch (err) { toast(err.message, true); }
});

async function campUse(c) {
  CAMP.use = c; CAMP.files = []; clearSrcFile();
  $('cu-name').textContent = c.name;
  $('cu-mode').textContent = modeLabel(c.mode);
  $('cu-chips').innerHTML = ruleChips(c.card);
  renderLogo(c);
  $('cu-overlay').classList.toggle('hidden', c.mode !== 'overlay');
  $('cu-source').classList.toggle('hidden', c.mode === 'overlay');
  $('cu-dropname').textContent = 'As many as you like';
  $('cu-url').value = ''; $('cu-links').value = '';
  const allowed = c.card.resolved.platforms.length ? c.card.resolved.platforms : ['tiktok', 'youtube', 'instagram'];
  $('cu-platforms').innerHTML = allowed.map((p, i) => `<button type="button" class="chip ${i === 0 || c.mode === 'overlay' ? 'on' : ''}" data-p="${p}">${esc(PLAT()[p] || p)}</button>`).join('');
  $('cu-platforms').querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
    if (ch.classList.contains('on') && $('cu-platforms').querySelectorAll('.chip.on').length === 1) { toast('Pick at least one place to post'); return; }
    ch.classList.toggle('on');
  }));
  const links = (c.rulebook.footage || {}).links || [];
  if (c.mode === 'overlay' && links.length)
    $('cu-dropname').innerHTML = `From its clip bank: ${links.map(l => `<a href="${esc(l)}" target="_blank" rel="noopener">${esc(l)}</a>`).join(', ')} — download the edits, then drop them here`;
  if (c.mode === 'source') {
    placeShared('campaign');
    lockShared(c.card);
    const off = c.card.permissions.filter(p => !p.allowed && ['cut', 'crop', 'zoom', 'stitch', 'audio', 'captions', 'hook', 'watermark'].includes(p.key));
    $('cu-locks').textContent = off.length ? `The brief doesn't allow: ${off.map(p => p.label.toLowerCase()).join(', ')}. Those stay off.` : '';
  } else placeShared('make');
  // what it has produced so far
  const st = (CAMP.list.find(x => x.id === c.id) || {}).stats;
  try {
    if (!st) { const { campaigns } = await api('/api/campaigns'); CAMP.list = campaigns; }
    const s = (CAMP.list.find(x => x.id === c.id) || {}).stats || {};
    $('cu-stats').innerHTML = `<div class="stat"><b>${s.videos || 0}</b><span>${s.videos === 1 ? 'video' : 'videos'} made</span></div><div class="stat"><b>${s.posts || 0}</b><span>${s.posts === 1 ? 'post' : 'posts'} tracked</span></div>
      <div class="stat"><b>${short(s.views || 0)}</b><span>views</span></div><div class="stat"><b>${money$(s.earned || 0)}</b><span>earned (estimate)</span></div>`;
    await fetchVideos();
    const runs = VIDEOS.filter(v => v.campaign && v.campaign.id === c.id);
    $('cu-runs').innerHTML = runs.length ? runs.map(v => vcard(v, { noCampaign: true })).join('') : '<div class="hint">Nothing made for this campaign yet.</div>';
  } catch { /* stats are extra */ }
}
$('cu-rules').addEventListener('click', () => { if (CAMP.use) location.hash = `#/campaign/${CAMP.use.id}/rules`; });

const cuDrop = $('cu-drop');
cuDrop.addEventListener('click', (ev) => { if (ev.target.tagName !== 'A') $('cu-files').click(); });
['dragenter', 'dragover'].forEach(e => cuDrop.addEventListener(e, ev => { ev.preventDefault(); cuDrop.classList.add('hot'); }));
['dragleave', 'drop'].forEach(e => cuDrop.addEventListener(e, ev => { ev.preventDefault(); cuDrop.classList.remove('hot'); }));
cuDrop.addEventListener('drop', ev => setCampFiles(ev.dataTransfer.files));
$('cu-files').addEventListener('change', ev => setCampFiles(ev.target.files));
function setCampFiles(list) {
  CAMP.files = [...(list || [])].filter(f => f.type.startsWith('video/') || /\.(mp4|mov|m4v|webm|mkv)$/i.test(f.name));
  const mb = CAMP.files.reduce((s, f) => s + f.size, 0) / 1e6;
  $('cu-dropname').textContent = CAMP.files.length
    ? `${CAMP.files.length} clip${CAMP.files.length > 1 ? 's' : ''}, ${mb.toFixed(0)} MB: ${CAMP.files.map(f => f.name).slice(0, 4).join(', ')}${CAMP.files.length > 4 ? '…' : ''}`
    : 'As many as you like';
}
const srcDrop = $('cu-srcdrop');
['dragenter', 'dragover'].forEach(e => srcDrop.addEventListener(e, ev => { ev.preventDefault(); srcDrop.classList.add('hot'); }));
['dragleave', 'drop'].forEach(e => srcDrop.addEventListener(e, ev => { ev.preventDefault(); srcDrop.classList.remove('hot'); }));
const setSrcFile = f => {
  if (!f) return;
  CAMP.srcFile = f; $('cu-url').value = ''; srcDrop.classList.add('has-file');
  $('cu-srcname').textContent = `${f.name} — ${(f.size / 1e6).toFixed(0)} MB, ready`;
  $('cu-srcpick').textContent = 'Choose another';
};
const clearSrcFile = () => {
  CAMP.srcFile = null; srcDrop.classList.remove('has-file'); $('cu-srcfile').value = '';
  $('cu-srcname').textContent = 'or drop the video file here'; $('cu-srcpick').textContent = 'Choose a file';
};
srcDrop.addEventListener('drop', ev => setSrcFile(ev.dataTransfer.files[0]));
$('cu-srcpick').addEventListener('click', () => $('cu-srcfile').click());
$('cu-srcfile').addEventListener('change', ev => setSrcFile(ev.target.files[0]));
$('cu-url').addEventListener('input', () => { autoGrow($('cu-url')); if (CAMP.srcFile && $('cu-url').value.trim()) clearSrcFile(); });

/* Switches the brief forbids: off and greyed out, so what you see is what runs. */
const SHARED_LOCKS = [['opt-tighten', 'cut'], ['opt-fillers', 'cut'], ['opt-frame', 'crop'], ['opt-motion', 'zoom'],
  ['opt-motion', 'crop'], ['opt-structure', 'stitch'], ['opt-headline', 'hook'], ['opt-logo', 'watermark']];
let SHARED_SAVED = null;      // the Make page's own choices, put back when a campaign's locks come off
function lockShared(card) {
  unlockShared();
  SHARED_SAVED = { checks: Object.fromEntries(SHARED_LOCKS.map(([id]) => [id, $(id).checked])), layout: $('layout').value };
  const allowed = card.resolved.allowed;
  SHARED_LOCKS.forEach(([id, key]) => {
    if (!allowed[key]) { const el = $(id); el.checked = false; el.disabled = true; el.closest('label').classList.add('locked'); }
  });
  if (!allowed.crop) { $('layout').value = 'blur'; $('layout').disabled = true; }
}
function unlockShared() {
  SHARED_LOCKS.forEach(([id]) => { const el = $(id); el.disabled = false; el.closest('label').classList.remove('locked'); });
  $('layout').disabled = false;
  if (SHARED_SAVED) {
    Object.entries(SHARED_SAVED.checks).forEach(([id, on]) => { $(id).checked = on; });
    $('layout').value = SHARED_SAVED.layout;
    SHARED_SAVED = null;
  }
}
$('cu-go').addEventListener('click', async () => {
  const c = CAMP.use;
  if (!c) return;
  const platforms = [...$('cu-platforms').querySelectorAll('.chip.on')].map(ch => ch.dataset.p).join(',');
  if (!platforms) return toast('Pick at least one place to post', true);
  const done = busy($('cu-go'), 'Starting…');
  try {
    let job_id;
    if (c.mode === 'overlay') {
      const links = $('cu-links').value.trim();
      if (!CAMP.files.length && !links) throw new Error("Drop in the campaign's clips first");
      const form = new FormData();
      CAMP.files.forEach(f => form.append('files', f));
      form.append('links', links); form.append('versions', $('cu-versions').value || 1);
      form.append('hook_style', $('cu-style').value); form.append('hook_color', $('cu-color').value);
      form.append('hook_position', $('cu-pos').value); form.append('hook_hold', $('cu-hold').value);
      form.append('bars', $('cu-bars').value); form.append('platforms', platforms);
      ({ job_id } = await api(`/api/campaigns/${c.id}/jobs`, { method: 'POST', body: form }));
    } else {
      const links = linksIn($('cu-url').value), url = links[0];
      if (!url && !CAMP.srcFile) throw new Error('Paste the video’s link, or drop the file in');
      if (!CAMP.srcFile && links.length > 1) {        // several videos: queued, one after another
        await post('/api/batch', { urls: links, campaign_id: c.id, settings: { ...settings(), platforms: platforms.split(',') } });
        toast(`${links.length} videos queued for ${c.name} — they run one after another`);
        location.hash = '#/videos'; refreshWorking();
        return;
      }
      const form = new FormData();
      if (CAMP.srcFile) form.append('file', CAMP.srcFile); else form.append('url', url);
      Object.entries({ ...settings(), platforms }).forEach(([k, v]) => form.append(k, v));
      form.append('campaign_id', c.id);
      ({ job_id } = await api('/api/jobs', { method: 'POST', body: form }));
    }
    location.hash = `#/video/${job_id}`;
    refreshWorking();
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});

/* ---------- the post kit ---------- */
let KIT = null;
const CHECK_ICON = { pass: '✓', fail: '✗', warn: '!', skip: '–' };
function openKit(clip) {
  if (!clip) return;
  KIT = clip;
  const gate = clip.compliance || { status: 'check', summary: 'Not checked yet.', checks: [] };
  const p = clip.post || {};
  $('kit-title').textContent = clip.hook || clip.title;
  $('kit-badge').textContent = GATE_LABEL[gate.status] || gate.status;
  $('kit-badge').className = `gate-pill ${gate.status}`;
  $('kit-video').src = clip.video_url ? `${clip.video_url}?v=${Date.now()}` : '';
  $('kit-video').poster = clip.thumb_url || '';
  $('kit-summary').textContent = gate.summary;
  $('kit-summary').className = `kit-summary ${gate.status}`;
  $('kit-checks').innerHTML = (gate.checks || []).map(ch => `<li class="${ch.status}"><span class="ic">${CHECK_ICON[ch.status] || '?'}</span>
    <span><b>${esc(ch.label)}</b> <span class="hint">${esc(ch.detail)}</span></span></li>`).join('');
  $('kit-caption').value = p.text || '';
  $('kit-steps').innerHTML = (p.checklist || []).map(s => `<li>${esc(s)}</li>`).join('');
  const blocked = gate.status === 'blocked';
  $('kit-download').disabled = blocked;
  $('kit-download').textContent = blocked ? 'Blocked — fix it first' : 'Download';
  $('kit-anyway').classList.toggle('hidden', !blocked);
  $('kit-anyway').dataset.armed = ''; $('kit-anyway').textContent = 'Download anyway';
  $('kit-edit').classList.toggle('hidden', !!clip.overlay);
  $('kit-hookblock').classList.toggle('hidden', !clip.overlay);
  if (clip.overlay) {
    const e = clip.edits || {}, look = CONFIG.campaign.look;
    $('kit-hook').value = clip.hook || '';
    $('kit-hooknote').textContent = [clip.reason, (clip.framing || {}).note].filter(Boolean).join(' ');
    $('kit-style').value = e.hook_style || look.hook_style; $('kit-color').value = e.hook_color || look.hook_color;
    $('kit-pos').value = e.hook_position || look.hook_position; $('kit-hold').value = String(e.hook_hold || look.hook_hold);
  }
  $('kit').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  overlayOpen();
}
function closeKit(fromHistory = false) {
  if ($('kit').classList.contains('hidden')) return;
  $('kit').classList.add('hidden'); $('kit-video').pause(); document.body.style.overflow = '';
  if (!fromHistory) setTimeout(overlayGone, 0);
}
$('kit-close').addEventListener('click', () => closeKit());
$('kit').addEventListener('click', ev => { if (ev.target === $('kit')) closeKit(); });
$('kit-copy').addEventListener('click', () => copyText($('kit-caption').value, 'Caption copied — paste it as it is'));
$('kit-download').addEventListener('click', () => { if (KIT) location.href = `/api/clips/${KIT.id}/download`; });
$('kit-anyway').addEventListener('click', (ev) => {
  const btn = ev.currentTarget;
  if (btn.dataset.armed !== '1') { btn.dataset.armed = '1'; btn.textContent = 'It breaks the brief — download anyway?'; return; }
  btn.dataset.armed = ''; btn.textContent = 'Download anyway';
  if (KIT) location.href = `/api/clips/${KIT.id}/download?anyway=1`;
});
$('kit-edit').addEventListener('click', () => { const c = KIT; closeKit(); openEditor(c); });
$('kit-render').addEventListener('click', async () => {
  if (!KIT) return;
  const done = busy($('kit-render'), 'Rendering and checking…');
  try {
    await post(`/api/clips/${KIT.id}/render`, { hook: $('kit-hook').value.trim(), hook_style: $('kit-style').value,
      hook_color: $('kit-color').value, hook_position: $('kit-pos').value, hook_hold: $('kit-hold').value });
    const updated = await waitForRender(KIT.id, 300000);
    openKit(updated);
    toast(updated.compliance ? `Checked: ${GATE_LABEL[updated.compliance.status]}` : 'Re-rendered');
    if (currentJob) watchJob(currentJob.id);
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});
function fillHookSelects() {
  const c = CONFIG.campaign;
  const styles = Object.entries(c.hook_styles), colours = Object.keys(c.hook_colors).map(k => [k, cap(k)]), positions = Object.entries(c.hook_positions);
  [['cu-style', styles], ['kit-style', styles], ['cu-color', colours], ['kit-color', colours], ['cu-pos', positions], ['kit-pos', positions]]
    .forEach(([id, pairs]) => fill($(id), pairs));
  $('cu-style').value = c.look.hook_style; $('cu-color').value = c.look.hook_color; $('cu-pos').value = c.look.hook_position;
}

/* =====================================================================
   Money
   ===================================================================== */
const mWhen = (t) => {
  if (!t) return '';
  const d = new Date(t * 1000), days = Math.abs(Date.now() - d) / 864e5;
  return d.toLocaleString([], days < 6 ? { weekday: 'short', hour: 'numeric', minute: '2-digit' } : { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
};
function mTable(rows, cols, empty) {
  if (!rows.length) return `<div class="empty-row">${empty || 'Nothing yet.'}</div>`;
  return `<table><thead><tr>${cols.map(c => `<th class="${c[2] || ''}">${c[0]}</th>`).join('')}</tr></thead><tbody>${
    rows.map(r => `<tr>${cols.map(c => `<td class="${c[2] || ''}" data-l="${c[0]}">${c[1](r)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function renderChart(daily) {
  const box = $('m-chart');
  if (!daily.length || daily.every(d => !d.views)) {
    box.innerHTML = '<div class="bars-empty">Your views per day show here once a posted clip has views.</div>';
    return;
  }
  const max = Math.max(1, ...daily.map(d => d.views));
  const top = daily.reduce((a, d) => (d.views > a.views ? d : a), daily[0]);
  const today = new Date().toLocaleDateString('en-CA');
  const label = d => d.date === today ? 'Today' : new Date(d.date + 'T12:00:00').toLocaleDateString([], { day: 'numeric', month: 'short' });
  const every = innerWidth < 600 ? 4 : daily.length > 10 ? 2 : 1;      // spaced so the dates never collide
  box.innerHTML = `<div class="bars">${daily.map(d => {
    const day = new Date(d.date + 'T12:00:00');
    const h = Math.max(d.views ? 2 : 0, Math.round((d.views / max) * 100));
    return `<div class="b" style="--h:${h}%" title="${day.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })}: ${d.views.toLocaleString()} views">
      <i></i>${d === top || (d.date === today && d.views) ? `<em>${short(d.views)}</em>` : ''}</div>`;
  }).join('')}</div>
    <div class="bars-x">${daily.map((d, i) => `<span>${(daily.length - 1 - i) % every === 0 ? label(d) : ''}</span>`).join('')}</div>`;
}
async function moneyLoad() {
  let m;
  try { m = await api('/api/money'); } catch (err) { toast(err.message, true); return; }
  const d = m.dashboard;
  $('m-stats').innerHTML = `<div><div class="lbl">Earned so far (estimate)</div><div class="big">${money$(d.earned)}</div></div>
    ${d.views > 0 && !d.earned ? '<div class="earn-note">Nothing counted yet: the campaign needs its $ per 1K views set in its rules, or the posts are still under its minimum views.</div>' : ''}
    <div class="sub"><div><b>${short(d.views)}</b><span>views</span></div><div><b>${short(d.views_24h)}</b><span>last 24 h</span></div><div><b>${d.posts}</b><span>posts live</span></div></div>`;
  renderChart(d.daily || []);
  $('m-posts').innerHTML = mTable(m.posts, [
    ['#', p => p.n],
    ['When', p => p.status === 'planned' ? `<span class="planned">Planned ${mWhen(p.planned_at)}</span>` : mWhen(p.posted_at)],
    ['Where', p => `${esc(PLAT_NAMES[p.platform] || p.platform || '')} ${esc(p.account || '')}`],
    ['Clip', p => esc((p.hook || p.title || '').slice(0, 80)), 'clip-cell'],
    ['Views', p => p.status !== 'posted' ? '' : p.platform === 'instagram'
      ? `<input class="m-views" data-id="${p.id}" type="number" min="0" value="${p.views || ''}" placeholder="type views" aria-label="Views">` : (p.views || 0).toLocaleString(), 'num'],
    ['Earned', p => p.status === 'posted' ? money$(p.earned) : '', 'num'],
    ['', p => p.status === 'planned'
      ? `<input class="m-live" data-id="${p.id}" type="text" placeholder="Live? Paste its link" aria-label="Post link">`
      : `${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">Open</a> · ` : ''}<a href="#" class="m-check" data-id="${p.id}">Update views</a>`],
  ], 'No posts yet. When a clip is live, paste its link above — or send <b>/posted link</b> to your Telegram bot.');
  $('m-camps').innerHTML = mTable(d.by_campaign, [['Campaign', g => esc(g.name), 'wrap'], ['Posts', g => g.posts, 'num'], ['Views', g => g.views.toLocaleString(), 'num'], ['Earned', g => money$(g.earned), 'num']]);
  $('m-styles').innerHTML = mTable(d.by_style.filter(g => g.name !== '—'), [['Look', g => esc(STYLE_NAME[g.name] || g.name)], ['Posts', g => g.posts, 'num'], ['Average views', g => g.avg_views.toLocaleString(), 'num']],
    m.posts.some(p => p.status === 'posted')
      ? 'Shows up once a few posted clips used one of ClipAgent’s looks — then it leans on what works for you.'
      : 'Shows up once you’ve posted clips. After a few posts of a look, ClipAgent uses what works for you.');
  $('m-tt').value = (m.accounts.tiktok || []).join(' ');
  $('m-ig').value = (m.accounts.instagram || []).join(' ');
  $('m-yt').value = (m.accounts.youtube || []).join(' ');
  $('m-perday').value = String(m.per_day || 3);
  $('m-watchlist').innerHTML = m.watch.length ? m.watch.map(w =>
    `<div class="watchrow"><span>${esc(w.url)}</span><button class="btn ghost small m-unwatch" type="button" data-url="${esc(w.url)}">Stop</button></div>`).join('')
    : '<div class="hint">Not watching any channels yet.</div>';
  document.querySelectorAll('.m-check').forEach(a => a.addEventListener('click', async (e) => {
    e.preventDefault(); a.textContent = 'Checking…';
    try { await post(`/api/posts/${a.dataset.id}/check`); } catch (err) { toast(err.message, true); }
    moneyLoad();
  }));
  document.querySelectorAll('.m-live').forEach(inp => inp.addEventListener('change', async () => {
    try { await post(`/api/posts/${inp.dataset.id}`, { url: inp.value.trim() }); toast('Tracking it'); moneyLoad(); }
    catch (err) { toast(err.message, true); }
  }));
  document.querySelectorAll('.m-views').forEach(inp => inp.addEventListener('change', async () => {
    await post(`/api/posts/${inp.dataset.id}`, { views: +inp.value || 0 }); moneyLoad();
  }));
  document.querySelectorAll('.m-unwatch').forEach(b => b.addEventListener('click', async () => {
    await post('/api/unwatch', { url: b.dataset.url }); moneyLoad();
  }));
}
$('m-add').addEventListener('click', async () => {
  const url = $('m-url').value.trim();
  if (!/^https?:\/\//.test(url)) return toast('Paste the post’s link first', true);
  const done = busy($('m-add'), 'Adding…');
  try { await post('/api/posts', { url }); $('m-url').value = ''; toast('Tracking it — views fill in within a minute'); setTimeout(moneyLoad, 5000); moneyLoad(); }
  catch (err) { toast(err.message, true); }
  finally { done(); }
});
$('m-saveacc').addEventListener('click', async () => {
  const split = (id) => $(id).value.split(/[\s,]+/).filter(Boolean);
  try {
    await post('/api/money/settings', { accounts: { tiktok: split('m-tt'), instagram: split('m-ig'), youtube: split('m-yt') }, per_day: +$('m-perday').value });
    toast('Accounts saved');
  } catch (err) { toast(err.message, true); }
});
$('m-watch').addEventListener('click', async () => {
  const url = $('m-watchurl').value.trim();
  if (!/^https?:\/\//.test(url)) return toast('Paste a channel link first', true);
  const done = busy($('m-watch'), 'Adding…');
  try { await post('/api/watch', { url }); $('m-watchurl').value = ''; toast('Watching it'); moneyLoad(); }
  catch (err) { toast(err.message, true); }
  finally { done(); }
});

/* =====================================================================
   Settings
   ===================================================================== */
function renderSettings() {
  applyTheme(themeChoice());
  const tg = CONFIG.telegram || {}, k = CONFIG.keys || {};
  $('set-telegram').innerHTML = tg.connected
    ? `<div class="set-row"><span class="dot ok"></span>Connected. ClipAgent messages you when clips are ready, when something goes wrong, and at each planned post.</div>
       <div class="hint">Send <b>/help</b> to your bot to see everything it can do — like sending it a link to clip.</div>`
    : tg.on
      ? `<div class="set-row"><span class="dot warn"></span>Almost there — open your bot in Telegram and press <b>Start</b>.</div>`
      : `<div class="set-row"><span class="dot"></span>Not set up yet.</div>
         <ol class="set-steps"><li>In Telegram, message <b>@BotFather</b> and send <b>/newbot</b>.</li>
         <li>Put the token it gives you in ClipAgent's <b>.env</b> file as <b>TELEGRAM_BOT_TOKEN=</b>.</li>
         <li>Close and reopen ClipAgent, then press <b>Start</b> in your bot.</li></ol>`;
  $('set-keys').innerHTML = `
    <div class="set-row"><span class="dot ${k.claude ? 'ok' : 'bad'}"></span>Claude — ${k.claude ? 'connected' : 'missing: add ANTHROPIC_API_KEY to the .env file'}</div>
    <div class="set-row"><span class="dot ${k.whisper ? 'ok' : 'bad'}"></span>Transcription — ${k.whisper ? 'connected' : 'missing: add WHISPER_API_KEY to the .env file'}</div>`;
}
/* Brand choices live in this browser, so they survive a reload and every run uses them. */
const BRAND_KEY = 'ca-brand';
function loadBrand() {
  let b = {};
  try { b = JSON.parse(localStorage.getItem(BRAND_KEY) || '{}') || {}; } catch (e) { b = {}; }
  $('logocorner').value = CONFIG.logo_corners.includes(b.corner) ? b.corner : 'top-right';
  if (/^#[0-9a-f]{6}$/i.test(b.accent || '')) { $('accent').value = b.accent; $('accent').dataset.on = '1'; }
  else $('accent').dataset.on = '0';
  showAccent();
}
function saveBrand() {
  try { localStorage.setItem(BRAND_KEY, JSON.stringify({ corner: $('logocorner').value, accent: $('accent').dataset.on === '1' ? $('accent').value : '' })); }
  catch (e) { /* private window: it still applies until the page closes */ }
}
function showAccent() {
  const on = $('accent').dataset.on === '1';
  $('accent').classList.toggle('off', !on);
  $('accentclear').classList.toggle('on', !on);
  $('accentclear').setAttribute('aria-pressed', on ? 'false' : 'true');
  $('accentclear').textContent = on ? 'Use each look’s own' : '✓ Each look’s own colour';
}
$('accent').addEventListener('input', () => { $('accent').dataset.on = '1'; showAccent(); saveBrand(); paintOverlay(); });
$('accentclear').addEventListener('click', () => { $('accent').dataset.on = '0'; showAccent(); saveBrand(); paintOverlay(); });
$('logocorner').addEventListener('change', () => { saveBrand(); toast('Logo corner saved'); });
function setLogo(has) {
  CONFIG.has_logo = has;
  $('logopreview').classList.toggle('hidden', !has);
  $('logoclear').classList.toggle('hidden', !has);
  $('logopick').textContent = has ? 'Replace' : 'Upload PNG';
  if (has) $('logopreview').src = `/media/logo.png?v=${Date.now()}`;
}
$('logopick').addEventListener('click', () => $('logoinput').click());
$('logoinput').addEventListener('change', async (ev) => {
  const file = ev.target.files[0]; ev.target.value = '';
  if (!file) return;
  const form = new FormData(); form.append('file', file);
  try { await api('/api/brand/logo', { method: 'POST', body: form }); setLogo(true); $('opt-logo').checked = true; toast('Logo saved — it’s on for your next video'); }
  catch (err) { toast(err.message, true); }
});
$('logoclear').addEventListener('click', async () => {
  await api('/api/brand/logo', { method: 'DELETE' }); setLogo(false); $('opt-logo').checked = false; toast('Logo removed');
});

/* =====================================================================
   Edits — short music edits cut from your videos
   ===================================================================== */
const EM = { styles: [], effects: {}, grades: {}, lengths: [], flashes: {}, rights: '', sources: [], sounds: [],
  picked: new Set(), style: 'velocity', sound: '', length: null, camps: [], list: [], listPoll: null, loaded: false };
const EDIT_SAMPLES = {
  velocity: '<div class="es es-velocity"><b>SEVEN<br>YEARS</b><i class="es-flash"></i></div>',
  aura: '<div class="es es-aura"><span class="es-hook">bro was down to $500 and still…</span></div>',
  flow: '<div class="es es-flow"><i></i><i></i><i></i></div>',
  cinematic: '<div class="es es-cine"><span class="es-sub">the market pays patience</span></div>',
  motivation: '<div class="es es-moti"><b><span>STICK</span> <span>TO</span> <span>THE</span> <span class="y">PLAN</span></b></div>',
  funny: '<div class="es es-funny"><span class="es-meme">HIS FACE WHEN IT HIT 😭</span></div>',
  money: '<div class="es es-money"><b>$2M</b><span>IN ONE YEAR</span></div>',
};
const EDIT_SHORT = {
  velocity: 'Cuts on every beat, slow-mo and a glitch on the drop', aura: 'Slow-mo, dark and cold, a lore hook',
  flow: 'Smooth cuts that carry the movement on', cinematic: 'Film look, his words, music under',
  motivation: 'Black and white, his line word by word', funny: 'Punchlines back to back, meme text',
  money: 'Warm gold, money lines on screen',
};
const THEME_EXAMPLES = ['his best trading advice', 'the funniest moments', 'money and wins', 'discipline and mindset'];
const EDIT_STEPS = [
  ['Pick the moments', /picking|matching|waiting|checking the words/i],
  ['Fit it to the beat', /fitting|laying/i],
  ['Render', /render/i],
  ['Check', /checking it|done/i],
];
const styleOf = (id) => EM.styles.find(s => s.id === id) || EM.styles[0];
const lenLabel = (s) => `${Math.round(s || 0)} s`;

async function editsMeta() {
  if (EM.loaded) return;
  const st = await api('/api/edit-styles');
  Object.assign(EM, { styles: st.styles, effects: st.effects, grades: st.grades, lengths: st.lengths,
    flashes: st.flashes, rights: st.rights, loaded: true });
}

/* ---------- songs: one player for the page ---------- */
function stopSong() {
  const a = $('em-audio');
  if (a && !a.paused) a.pause();
  document.querySelectorAll('.song .play.on').forEach(b => b.classList.remove('on'));
}
function playSong(id, btn) {
  const a = $('em-audio');
  if (btn.classList.contains('on')) { stopSong(); return; }
  stopSong();
  a.src = `/media/sound/${id}`;
  a.play().then(() => btn.classList.add('on')).catch(() => toast('Couldn’t play that song in the browser', true));
  a.onended = () => btn.classList.remove('on');
}
function energyCurve(s, w = 150, h = 30) {
  const e = s.energy || [];
  if (!e.length) return '';
  const step = w / Math.max(1, e.length - 1);
  const pts = e.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - v * (h - 4)).toFixed(1)}`).join(' ');
  const dx = s.duration ? (s.drop / s.duration) * w : -10;
  return `<svg class="curve" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="0,${h} ${pts} ${w},${h}" /><line x1="${dx.toFixed(1)}" x2="${dx.toFixed(1)}" y1="0" y2="${h}" /></svg>`;
}

/* ---------- make an edit ---------- */
async function editsHome(fromJob) {
  try { await editsMeta(); } catch (err) { toast(err.message, true); return; }
  const [src, snd] = await Promise.all([api('/api/edit-sources').catch(() => ({ sources: [] })),
    api('/api/sounds').catch(() => ({ sounds: [] }))]);
  EM.sources = src.sources; EM.sounds = snd.sounds;
  if (fromJob) EM.picked = new Set([fromJob]);
  [...EM.picked].forEach(id => { if (!EM.sources.some(s => s.id === id)) EM.picked.delete(id); });
  if (!EM.picked.size && EM.sources.length) EM.picked.add(EM.sources[0].id);
  if (EM.sound && !EM.sounds.some(s => s.id === EM.sound)) EM.sound = '';
  if (!EM.sound && EM.sounds.length && styleOf(EM.style).needs_music) EM.sound = EM.sounds[0].id;
  $('em-rights').textContent = EM.rights;
  renderEditSources(); renderEditStyles(); renderSongs(); renderEditLengths(); loadEditCamps();
  loadEditList();
}

function renderEditSources() {
  const box = $('em-sources');
  if (!EM.sources.length) {
    box.innerHTML = '<div class="empty"><b>No videos to cut from yet</b>Make clips from a video first — then it shows up here.<br><a class="btn" href="#/make">Make clips</a></div>';
    return;
  }
  box.innerHTML = EM.sources.map(s => `<button type="button" class="foot ${EM.picked.has(s.id) ? 'on' : ''}" data-id="${s.id}" aria-pressed="${EM.picked.has(s.id)}">
      <span class="tick" aria-hidden="true"></span>
      <span class="fposter">${s.poster ? `<img src="${s.poster}" alt="" loading="lazy" onerror="this.remove()">` : ''}</span>
      <span class="ft">${esc(niceTitle(s))}</span>
      <span class="fm">${s.duration ? mins(s.duration) : ''}${s.campaign_id ? ' · campaign' : ''}</span>
    </button>`).join('');
  box.querySelectorAll('.foot').forEach(b => b.addEventListener('click', () => {
    const id = b.dataset.id;
    if (EM.picked.has(id)) { if (EM.picked.size > 1) EM.picked.delete(id); else toast('Keep at least one video — the edit is cut from it'); }
    else EM.picked.add(id);
    renderEditSources();
  }));
}

function renderEditStyles() {
  $('em-styles').innerHTML = EM.styles.map(s => `<button type="button" class="look ${s.id === EM.style ? 'on' : ''}" data-style="${s.id}" aria-pressed="${s.id === EM.style}">
      <span class="tick" aria-hidden="true"></span>
      <div class="frame">${EDIT_SAMPLES[s.id] || ''}</div>
      <div class="name">${esc(s.name)}</div><div class="sub">${esc(EDIT_SHORT[s.id] || s.what)}</div>
    </button>`).join('');
  $('em-styles').querySelectorAll('.look').forEach(b => b.addEventListener('click', () => {
    EM.style = b.dataset.style;
    const st = styleOf(EM.style);
    if (st.needs_music && !EM.sound && EM.sounds.length) EM.sound = EM.sounds[0].id;
    renderEditStyles(); renderSongs(); renderEditLengths(); loadEditCamps();
  }));
  const st = styleOf(EM.style);
  $('em-style-hint').textContent = `${st.what} ` + (st.needs_music
    ? `It’s cut to a song: the cuts land on its beats and the best moment on its drop. Music only — his voice is off (you can bring it back later).`
    : 'His words play whole. A song underneath is optional — cuts still land on its beats.');
}

function renderSongs() {
  const st = styleOf(EM.style);
  const box = $('em-songs');
  let html = EM.sounds.map(s => `<div class="song ${s.id === EM.sound ? 'on' : ''}" data-id="${s.id}" role="radio" tabindex="0" aria-checked="${s.id === EM.sound}">
      <button type="button" class="play" aria-label="Play ${esc(s.name)}"></button>
      <div class="s-main"><b>${esc(s.name)}</b><span>${s.bpm ? Math.round(s.bpm) + ' BPM · ' : ''}${fmt(s.duration)}${s.drop ? ` · drops at ${fmt(s.drop)}` : ''}</span></div>
      ${energyCurve(s)}
      <button type="button" class="x" title="Remove this song" aria-label="Remove ${esc(s.name)}">×</button>
    </div>`).join('');
  if (st.music_optional) html += `<div class="song none ${!EM.sound ? 'on' : ''}" data-id="" role="radio" tabindex="0" aria-checked="${!EM.sound}">
      <span class="play off" aria-hidden="true"></span><div class="s-main"><b>No music</b><span>Just his voice</span></div></div>`;
  if (!EM.sounds.length && st.needs_music) html = `<div class="empty"><b>${esc(st.name)} edits are cut to a song</b>Add a song you’re allowed to use — an MP3, or a video whose sound you want.<br><button type="button" class="btn" id="em-addsong2">Add a song</button></div>`;
  box.innerHTML = html;
  $('em-addsong2')?.addEventListener('click', () => $('em-songfile').click());
  box.querySelectorAll('.song').forEach(row => {
    const pick = () => { EM.sound = row.dataset.id; renderSongs(); };
    row.addEventListener('click', ev => { if (!ev.target.closest('button')) pick(); });
    row.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(); } });
    row.querySelector('.play:not(.off)')?.addEventListener('click', ev => playSong(row.dataset.id, ev.currentTarget));
    row.querySelector('.x')?.addEventListener('click', async () => {
      const s = EM.sounds.find(x => x.id === row.dataset.id);
      if (!confirm(`Remove “${s.name}” from your songs? Edits already made keep their sound.`)) return;
      try {
        await api(`/api/sounds/${s.id}`, { method: 'DELETE' });
        EM.sounds = EM.sounds.filter(x => x.id !== s.id);
        if (EM.sound === s.id) EM.sound = EM.sounds[0]?.id || '';
        stopSong(); renderSongs();
      } catch (err) { toast(err.message, true); }
    });
  });
  if (st.needs_music && !EM.sound && EM.sounds.length) { EM.sound = EM.sounds[0].id; renderSongs(); }
}
$('em-addsong').addEventListener('click', () => $('em-songfile').click());
$('em-songfile').addEventListener('change', async ev => {
  const file = ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  const done = busy($('em-addsong'), 'Reading the beats…');
  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('name', file.name.replace(/\.[^.]+$/, ''));
    const res = await fetch('/api/sounds', { method: 'POST', body: fd });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || 'Couldn’t add that song');
    EM.sounds.unshift(body.sound);
    EM.sound = body.sound.id;
    renderSongs();
    toast(`Added — ${Math.round(body.sound.bpm)} BPM, drops at ${fmt(body.sound.drop)}`);
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});

function renderEditLengths() {
  const best = styleOf(EM.style).length;
  const on = EM.length || best;
  $('em-lengths').innerHTML = EM.lengths.map(l => `<button type="button" class="chip ${l === on ? 'on' : ''}" data-l="${l}">${l} s${l === best ? ' · best' : ''}</button>`).join('');
  $('em-lengths').querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => { EM.length = +c.dataset.l; renderEditLengths(); }));
}
fillExamples($('em-themes'), THEME_EXAMPLES, 'em-theme');

async function loadEditCamps() {
  try { EM.camps = (await api(`/api/edit-campaigns?style=${EM.style}`)).campaigns; } catch { EM.camps = []; }
  const sel = $('em-campaign'), keep = sel.value;
  fill(sel, [['', 'No campaign']].concat(EM.camps.map(c => [c.id, c.name])));
  sel.value = EM.camps.some(c => c.id === keep) ? keep : '';
  campNote();
}
function campNote() {
  const c = EM.camps.find(x => x.id === $('em-campaign').value);
  const note = $('em-camp-note');
  note.classList.remove('warnnote');
  if (!c) { note.textContent = ''; return; }
  const st = styleOf(EM.style);
  let text = c.refusal || '';
  if (!text && !c.music && st.needs_music) text = `${c.name}: the brief doesn’t allow added music, so ${st.name} can’t be made for it. Pick Cinematic, Motivation or Funny with “No music” — or switch music on in the campaign’s rules if the brief allows it.`;
  else if (!text) text = [c.music ? 'Music is allowed.' : 'No added music — pick “No music”.'].concat(c.notes).join(' ');
  note.textContent = text;
  note.classList.toggle('warnnote', !!(c.refusal || (!c.music && (st.needs_music || EM.sound))));
}
$('em-campaign').addEventListener('change', campNote);

$('em-go').addEventListener('click', async () => {
  if (!EM.picked.size) { toast('Pick at least one video to cut the edit from', true); return; }
  const st = styleOf(EM.style);
  if (st.needs_music && !EM.sound) { toast(`${st.name} edits are cut to a song — add or pick one first`, true); return; }
  const done = busy($('em-go'), 'Starting…');
  try {
    const { id } = await post('/api/edits', {
      style: EM.style, sources: [...EM.picked], sound: EM.sound, theme: $('em-theme').value.trim(),
      length: EM.length || st.length, campaign_id: $('em-campaign').value,
    });
    $('em-theme').value = '';
    location.hash = `#/edit/${id}`;
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});

function ecard(e) {
  const working = e.status === 'running' || e.status === 'queued';
  const tag = working ? `<span class="tag accent">${/waiting/i.test(e.stage || '') ? 'Waiting' : `Making ${e.progress || 0}%`}</span>`
    : e.status === 'failed' ? '<span class="tag bad">Didn’t work</span>'
    : e.verdict === 'blocked' ? '<span class="tag bad">Blocked</span>' : e.verdict === 'check' ? '<span class="tag warn">Check first</span>'
    : '<span class="tag good">Ready</span>';
  return `<a class="vcard" href="#/edit/${e.id}">
    <div class="poster">${e.thumb_url ? `<img src="${e.thumb_url}" alt="" loading="lazy">` : working ? '<span class="spin"></span>' : PH_ICON}</div>
    <div>
      <div class="t" title="${esc(e.title)}">${esc(e.title)}</div>
      <div class="facts">${tag}<span class="tag">${esc(e.style_name)}</span>${e.length ? `<span class="tag">${lenLabel(e.length)}</span>` : ''}</div>
      ${working ? `<div class="mini-bar"><i style="width:${e.progress || 0}%"></i></div>` : ''}
      ${e.status === 'failed' && e.error ? `<div class="why">${esc(e.error)}</div>` : `<div class="when">${ago(e.created_at)}</div>`}
    </div></a>`;
}
async function loadEditList() {
  const render = () => {
    $('em-list').innerHTML = EM.list.length ? EM.list.map(ecard).join('')
      : '<div class="empty"><b>No edits yet</b>Pick a video, a style and a song above, then press Make edit.</div>';
  };
  try { EM.list = (await api('/api/edits')).edits; } catch { EM.list = []; }
  render();
  clearInterval(EM.listPoll);
  if (EM.list.some(e => e.status === 'running' || e.status === 'queued'))
    EM.listPoll = setInterval(async () => {
      try { EM.list = (await api('/api/edits')).edits; } catch { return; }
      render();
      if (!EM.list.some(e => e.status === 'running' || e.status === 'queued')) { clearInterval(EM.listPoll); refreshWorking(); }
    }, 4000);
}

/* ---------- one edit ---------- */
let EE = { id: null, edit: null, poll: null, draft: {}, moments: null, video: '' };
const EDIT_ASK_EXAMPLES = ['Faster', 'More flashes', 'Black and white', 'Put the line about … on the drop',
  'Use a different song', 'Hook about …'];
fillExamples($('ee-ask-examples'), EDIT_ASK_EXAMPLES, 'ee-ask-text');

async function openEdit(id) {
  clearInterval(EE.poll);
  EE = { id, edit: null, poll: null, draft: {}, moments: null, video: '' };
  $('ee-title').textContent = 'Loading…'; $('ee-sub').innerHTML = ''; $('ee-notes').innerHTML = '';
  $('ee-video').removeAttribute('src'); $('ee-video').load(); $('ee-empty').classList.remove('hidden');
  $('ee-ask-log').innerHTML = ''; $('ee-versions-pick').classList.add('hidden');
  try {
    await editsMeta();
    EM.sounds = (await api('/api/sounds')).sounds;
  } catch (err) { toast(err.message, true); }
  await tickEdit();
  EE.poll = setInterval(tickEdit, 2000);
}
const editWorking = (e) => e && (e.status === 'running' || e.status === 'queued');
async function tickEdit() {
  let e;
  try { e = await api(`/api/edits/${EE.id}`); }
  catch (err) {
    clearInterval(EE.poll); EE.poll = null;
    $('ee-title').textContent = 'Edit not found';
    $('ee-sub').innerHTML = `<span class="hint">${esc(err.message)}</span>`;
    return;
  }
  if (location.hash !== `#/edit/${EE.id}`) { clearInterval(EE.poll); return; }
  const fresh = !EE.edit || EE.edit.status !== e.status;     // first load, or it just finished
  EE.edit = e;
  const has = e.moments.length > 0;                 // nothing to change until the moments are picked
  $('ee-ask').classList.toggle('hidden', !has);
  document.querySelectorAll('.ee-card').forEach(c => c.classList.toggle('hidden', !has));
  renderEditHead(e); renderEditProgress(e); renderEditPreview(e); renderEditAsks(e);
  if (!Object.keys(EE.draft).length && (fresh || !EE.moments)) renderEditControls(e);
  if (!editWorking(e)) { clearInterval(EE.poll); EE.poll = null; refreshWorking(); }
}
function watchEdit() { clearInterval(EE.poll); tickEdit(); EE.poll = setInterval(tickEdit, 2000); }

function renderEditHead(e) {
  $('ee-title').textContent = e.title || 'Edit';
  document.title = `${e.title || 'Edit'} — ClipAgent`;
  const tags = [`<span class="tag accent">${esc(e.style_name)}</span>`];
  if (e.length) tags.push(`<span class="tag">${lenLabel(e.length)}</span>`);
  tags.push(e.sound ? `<span class="tag">♪ ${esc(e.sound.name)}${e.sound.bpm ? ` · ${Math.round(e.sound.bpm)} BPM` : ''}</span>` : '<span class="tag">No music</span>');
  if (e.moments.length) tags.push(`<span class="tag">${e.moments.filter(m => !m.off).length} moments</span>`);
  const camp = (EM.camps || []).find(c => c.id === e.campaign_id);
  if (e.campaign_id) tags.push(`<a class="tag info" href="#/campaign/${e.campaign_id}">${esc(camp ? camp.name : 'Campaign')}</a>`);
  if (e.created_at) tags.push(`<span class="tag">${ago(e.created_at)}</span>`);
  $('ee-sub').innerHTML = tags.join('');
}

function renderEditProgress(e) {
  const box = $('ee-progress');
  const working = editWorking(e), failed = e.status === 'failed';
  box.classList.toggle('hidden', !working && !failed);
  box.classList.toggle('failed', failed);
  if (!working && !failed) return;
  let at = EDIT_STEPS.findIndex(([, re]) => re.test(e.stage || ''));
  if (at < 0) at = failed ? 0 : 0;
  $('ee-steps').innerHTML = EDIT_STEPS.map(([label], i) => {
    const cls = failed && i === at ? 'fail' : i < at ? 'done' : i === at ? (failed ? 'fail' : 'on') : '';
    return `<span class="s ${cls}"><i></i>${label}</span>`;
  }).join('');
  $('ee-bar').style.width = `${failed ? 100 : e.progress || 0}%`;
  $('ee-stage').textContent = failed ? (e.error || 'It didn’t work.') + (e.video_url ? ' The version before is still below.' : '') : (e.stage || 'Working…');
  $('ee-pct').textContent = failed ? '' : `${e.progress || 0}%`;
  $('ee-retry').classList.toggle('hidden', !failed);
  $('ee-retry').textContent = e.moments.length ? 'Try again — your moments are kept' : 'Try again';
}
$('ee-retry').addEventListener('click', () => remakeEdit({}, $('ee-retry')));

const CHECKS_HTML = (g) => `<ul class="checks">${(g.checks || []).map(c =>
  `<li class="${c.status}"><span class="ic">${CHECK_ICON[c.status] || '–'}</span><span><b>${esc(c.label)}</b> ${esc(c.detail || '')}</span></li>`).join('')}</ul>`;
function renderEditPreview(e) {
  const v = $('ee-video');
  if (e.video_url && EE.video !== e.video_url) { EE.video = e.video_url; v.src = e.video_url; v.poster = e.thumb_url || ''; }
  $('ee-empty').classList.toggle('hidden', !!e.video_url);
  $('ee-empty').querySelector('span:last-child').textContent = e.status === 'failed' ? 'No video yet' : 'Making your edit…';
  const g = e.compliance;
  $('ee-gate').innerHTML = g ? `<div class="panel-block ee-gate"><div class="kit-summary ${g.status}">${esc(GATE_LABEL[g.status] || g.status)} — ${esc((g.summary || '').replace(/^(Blocked|Check before posting):\s*/, ''))}</div>${CHECKS_HTML(g)}</div>` : '';
  const blocked = g && g.status === 'blocked';
  $('ee-download').disabled = !e.video_url || blocked;
  $('ee-download').title = blocked ? 'Blocked by the campaign check — see why above' : '';
  $('ee-anyway').classList.toggle('hidden', !blocked);
  $('ee-copy').disabled = !e.post_text;
  $('ee-undo').classList.toggle('hidden', !(e.can_undo && !editWorking(e)));
  $('ee-notes').innerHTML = (e.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
}
$('ee-download').addEventListener('click', () => { location.href = `/api/edits/${EE.id}/download`; });
$('ee-anyway').addEventListener('click', () => {
  if (confirm('This edit breaks the campaign’s brief, so it will likely be rejected. Download it anyway?'))
    location.href = `/api/edits/${EE.id}/download?anyway=1`;
});
$('ee-copy').addEventListener('click', () => EE.edit && copyText(EE.edit.post_text, 'Caption and hashtags copied'));
$('ee-undo').addEventListener('click', async () => {
  try { await post(`/api/edits/${EE.id}/undo`, {}); toast('Put back the version from before'); EE.draft = {}; EE.moments = null; EE.video = ''; watchEdit(); }
  catch (err) { toast(err.message, true); }
});

/* the controls: changes collect in EE.draft until Re-make */
function draftChanged() {
  const n = Object.keys(EE.draft).length;
  $('ee-dirty').textContent = n ? 'You’ve changed things — press Re-make to see them (no new moments are picked).' : '';
  $('ee-dirty').classList.toggle('dirty', !!n);
}
const val = (k, fallback) => (k in EE.draft ? EE.draft[k] : fallback);
function segSet(id, v) { document.querySelectorAll(`#${id} button`).forEach(b => { b.classList.toggle('on', b.dataset.v === v); b.setAttribute('aria-checked', b.dataset.v === v); }); }

function renderEditControls(e) {
  const style = val('style', e.style), st = styleOf(style);
  $('ee-styles').innerHTML = EM.styles.map(s => `<button type="button" class="chip ${s.id === style ? 'on' : ''}" data-s="${s.id}" title="${esc(s.what)}">${esc(s.name)}</button>`).join('');
  $('ee-styles').querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
    EE.draft.style = c.dataset.s;
    const ns = styleOf(c.dataset.s);
    if (ns.needs_music && !val('sound', e.settings.sound) && EM.sounds.length) EE.draft.sound = EM.sounds[0].id;
    renderEditControls(e); draftChanged();
  }));
  const sound = val('sound', e.settings.sound || '');
  const songOpts = EM.sounds.map(s => [s.id, `${s.name}${s.bpm ? ` · ${Math.round(s.bpm)} BPM` : ''}`]);
  fill($('ee-song'), (st.music_optional || !EM.sounds.length ? [['', 'No music']] : []).concat(songOpts));
  $('ee-song').value = sound;
  fill($('ee-grade'), Object.entries(EM.grades));
  $('ee-grade').value = val('grade', e.grade);
  const len = val('length', e.settings.length);
  $('ee-lengths').innerHTML = EM.lengths.map(l => `<button type="button" class="chip ${l === len ? 'on' : ''}" data-l="${l}">${l} s</button>`).join('');
  $('ee-lengths').querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => { EE.draft.length = +c.dataset.l; renderEditControls(e); draftChanged(); }));
  $('ee-pacewrap').classList.toggle('hidden', st.pace !== 'beat');
  segSet('ee-pace', val('pace', e.pace)); segSet('ee-flashes', val('flashes', e.flashes));
  const fx = { ...e.effects, ...(EE.draft.effects || {}) };
  $('ee-effects').innerHTML = Object.entries(EM.effects).map(([k, label]) =>
    `<label class="toggle small"><input type="checkbox" data-fx="${k}" ${fx[k] ? 'checked' : ''}><span>${esc(label)}</span></label>`).join('');
  $('ee-effects').querySelectorAll('input').forEach(i => i.addEventListener('change', () => {
    EE.draft.effects = { ...(EE.draft.effects || {}), [i.dataset.fx]: i.checked }; draftChanged();
  }));
  const pct = (v) => +v ? `${Math.round(v * 100)}%` : 'Off';
  $('ee-voice').value = val('voice', e.voice); $('ee-voiceval').textContent = pct($('ee-voice').value);
  $('ee-music').value = val('music', e.music); $('ee-musicval').textContent = pct($('ee-music').value);
  $('ee-music').disabled = !sound;
  renderPart(e);
  $('ee-hook').value = val('hook', e.hook);
  $('ee-caption').value = e.caption || '';
  $('ee-tags').value = (e.hashtags || []).join(' ');
  $('ee-posthint').textContent = e.campaign_id ? 'The campaign’s own lines and hashtags are kept.' : '';
  $('ee-checklist').innerHTML = (e.checklist || []).map(s => `<li>${esc(s)}</li>`).join('');
  if (!EE.moments) EE.moments = e.moments.map(m => ({ ...m }));
  renderMoments(st);
  draftChanged();
}
['ee-pace', 'ee-flashes'].forEach(id => document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener('click', () => {
  EE.draft[id === 'ee-pace' ? 'pace' : 'flashes'] = b.dataset.v; segSet(id, b.dataset.v); draftChanged();
})));
$('ee-song').addEventListener('change', () => {
  EE.draft.sound = $('ee-song').value; delete EE.draft.song_start;
  $('ee-music').disabled = !EE.draft.sound; renderPart(EE.edit); draftChanged();
});

/* the part of the song: the loudness curve, the stretch used, and the drop */
function renderPart(e) {
  const wrap = $('ee-partwrap');
  const songId = val('sound', e.settings.sound || '');
  const s = EM.sounds.find(x => x.id === songId);
  wrap.classList.toggle('hidden', !s || !(s.energy || []).length);
  if (!s || !(s.energy || []).length) return;
  const W = 600, H = 46, dur = s.duration || 1, len = val('length', e.settings.length);
  const sameSong = songId === (e.settings.sound || '');
  const st = styleOf(val('style', e.style));
  let a, b, auto;
  if ('song_start' in EE.draft) { auto = EE.draft.song_start == null; a = auto ? null : EE.draft.song_start; }
  else { auto = e.song_start == null; a = sameSong && e.song_part ? e.song_part[0] : e.song_start; }
  const changed = 'song_start' in EE.draft || !sameSong || 'length' in EE.draft || 'style' in EE.draft;
  if (a == null || (auto && changed)) {
    // the automatic part: a beat edit starts a few beats before the drop; a voice edit lines the drop up with its best line
    const pre = st.pace === 'beat' ? (st.pre_beats || 4) * 60 / (s.bpm || 120) : len / 2;
    a = Math.max(0, Math.min(dur - len, (s.drop || 0) - pre));
  }
  b = !changed && sameSong && e.song_part ? e.song_part[1] : Math.min(dur, a + len);
  const x = t => (t / dur) * W;
  const step = W / Math.max(1, s.energy.length - 1);
  const pts = s.energy.map((v, i) => `${(i * step).toFixed(1)},${(H - 2 - v * (H - 6)).toFixed(1)}`).join(' ');
  $('ee-part').innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="0,${H} ${pts} ${W},${H}"/>
      <rect class="win" x="${x(a).toFixed(1)}" y="1" width="${Math.max(2, x(b) - x(a)).toFixed(1)}" height="${H - 2}" rx="3"/>
      <line x1="${x(s.drop).toFixed(1)}" x2="${x(s.drop).toFixed(1)}" y1="0" y2="${H}"/></svg>`;
  $('ee-partval').textContent = `${auto ? 'Auto · ' : ''}${fmt(a)}–${fmt(b)} of ${fmt(dur)}`;
  $('ee-part').setAttribute('aria-valuetext', `starts at ${fmt(a)}`);
  $('ee-partauto').disabled = auto;
}
function pickPart(t) {
  const s = EM.sounds.find(x => x.id === val('sound', EE.edit.settings.sound || ''));
  if (!s) return;
  EE.draft.song_start = Math.max(0, Math.min(s.duration - 5, Math.round(t * 10) / 10));
  renderPart(EE.edit); draftChanged();
}
$('ee-part').addEventListener('click', ev => {
  const r = $('ee-part').getBoundingClientRect();
  const s = EM.sounds.find(x => x.id === val('sound', EE.edit.settings.sound || ''));
  if (s) pickPart(((ev.clientX - r.left) / r.width) * s.duration);
});
$('ee-part').addEventListener('keydown', ev => {
  if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
  ev.preventDefault();
  const cur = EE.draft.song_start ?? (EE.edit.song_part ? EE.edit.song_part[0] : 0);
  pickPart(cur + (ev.key === 'ArrowRight' ? 2 : -2));
});
$('ee-partauto').addEventListener('click', () => { EE.draft.song_start = null; renderPart(EE.edit); draftChanged(); });
$('ee-grade').addEventListener('change', () => { EE.draft.grade = $('ee-grade').value; draftChanged(); });
['voice', 'music'].forEach(k => $(`ee-${k}`).addEventListener('input', () => {
  EE.draft[k] = +$(`ee-${k}`).value;
  $(`ee-${k}val`).textContent = +$(`ee-${k}`).value ? `${Math.round($(`ee-${k}`).value * 100)}%` : 'Off';
  draftChanged();
}));
$('ee-hook').addEventListener('input', () => { EE.draft.hook = $('ee-hook').value; draftChanged(); });

function renderMoments(st) {
  const shown = st.text === 'punch' || st.text === 'quote' || st.text === 'meme';
  const list = EE.moments || [];
  $('ee-mcount').textContent = list.length ? `(${list.filter(m => !m.off).length} of ${list.length} on)` : '';
  $('ee-moments').innerHTML = list.map((m, i) => `<div class="mrow ${m.off ? 'off' : ''} ${m.drop ? 'drop' : ''}" data-i="${i}">
      <img class="mthumb" src="${m.thumb}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
      <div class="mmain">
        <input class="mtext" type="text" maxlength="160" value="${esc(m.text || '')}" placeholder="${shown ? 'No words on screen' : 'Words aren’t shown in this style'}" aria-label="Words on screen for this moment">
        <div class="mmeta">${m.drop ? '<b>On the drop</b> · ' : ''}${m.length} s at ${fmt(m.start)} · ${esc(m.source_title || '')}</div>
      </div>
      <div class="mctl">
        <button type="button" class="btn ghost small mdrop" ${m.drop ? 'disabled' : ''} title="Land this moment on the song’s drop">${m.drop ? 'On the drop' : 'Put on drop'}</button>
        <label class="toggle small"><input type="checkbox" class="mon" ${m.off ? '' : 'checked'}><span>On</span></label>
        <button type="button" class="btn ghost small mup" ${i === 0 ? 'disabled' : ''} aria-label="Move up">↑</button>
        <button type="button" class="btn ghost small mdown" ${i === list.length - 1 ? 'disabled' : ''} aria-label="Move down">↓</button>
      </div></div>`).join('');
  const changed = () => { EE.draft.moments = EE.moments.map(m => ({ id: m.id, text: m.text || '', off: !!m.off, drop: !!m.drop })); draftChanged(); };
  $('ee-moments').querySelectorAll('.mrow').forEach(row => {
    const i = +row.dataset.i, m = EE.moments[i];
    row.querySelector('.mtext').addEventListener('input', ev => { m.text = ev.target.value; changed(); });
    row.querySelector('.mon').addEventListener('change', ev => {
      if (!ev.target.checked && EE.moments.filter(x => !x.off).length <= 1) { ev.target.checked = true; toast('Keep at least one moment on'); return; }
      m.off = !ev.target.checked; changed(); renderMoments(st);
    });
    row.querySelector('.mdrop').addEventListener('click', () => { EE.moments.forEach(x => { x.drop = x === m; }); m.off = false; changed(); renderMoments(st); });
    const move = (d) => { const j = i + d; [EE.moments[i], EE.moments[j]] = [EE.moments[j], EE.moments[i]]; changed(); renderMoments(st); };
    row.querySelector('.mup').addEventListener('click', () => move(-1));
    row.querySelector('.mdown').addEventListener('click', () => move(1));
  });
}

async function remakeEdit(changes, btn) {
  const done = busy(btn, 'Starting…');
  try {
    await post(`/api/edits/${EE.id}/remake`, changes);
    EE.draft = {}; EE.moments = null; draftChanged();
    toast(changes.repick ? 'Picking new moments — the old version stays until the new one is ready' : 'Re-making it — Undo puts this version back');
    watchEdit();
  } catch (err) { toast(err.message, true); }
  finally { done(); }
}
$('ee-remake').addEventListener('click', () => {
  if (!Object.keys(EE.draft).length) { toast('Change something first — or press New moments for a fresh pick'); return; }
  remakeEdit({ ...EE.draft }, $('ee-remake'));
});
$('ee-repick').addEventListener('click', () => remakeEdit({ ...EE.draft, repick: true }, $('ee-repick')));

$('ee-versions').addEventListener('click', () => {
  const e = EE.edit; if (!e) return;
  const box = $('ee-versions-pick');
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  const others = EM.sounds.filter(s => s.id !== e.settings.sound);
  const st = styleOf(e.style);
  if (!others.length && !(st.music_optional && e.settings.sound)) { toast('Add another song first — on the Edits page'); return; }
  box.innerHTML = '<div class="hint">Same moments and words, cut to each song you tick — post them and see which one wins.</div>'
    + others.map(s => `<label class="toggle small"><input type="checkbox" value="${s.id}" checked><span>${esc(s.name)}${s.bpm ? ` · ${Math.round(s.bpm)} BPM` : ''}</span></label>`).join('')
    + (st.music_optional && e.settings.sound ? '<label class="toggle small"><input type="checkbox" value=""><span>No music</span></label>' : '')
    + '<div class="btnrow"><button type="button" class="btn small" id="ee-versions-go">Make them</button></div>';
  box.classList.remove('hidden');
  $('ee-versions-go').addEventListener('click', async () => {
    const ids = [...box.querySelectorAll('input:checked')].map(i => i.value);
    if (!ids.length) { toast('Tick at least one song', true); return; }
    const done = busy($('ee-versions-go'), 'Starting…');
    try {
      const out = await post(`/api/edits/${EE.id}/versions`, { sounds: ids });
      toast(`Making ${out.ids.length} version${out.ids.length > 1 ? 's' : ''} — they show up in Your edits`);
      box.classList.add('hidden');
      location.hash = '#/edits';
    } catch (err) { toast(err.message, true); }
    finally { done(); }
  });
});

/* typed changes */
function renderEditAsks(e) {
  const asks = (e.asks || []).slice().reverse();
  $('ee-ask-log').innerHTML = asks.slice(0, 3).map(a => `<div class="ask-item"><div class="you">${esc(a.text)}</div><div class="ca">
      ${a.question ? `<p class="q">${esc(a.question)}</p>` : ''}${a.understood ? `<p>${esc(a.understood)}</p>` : ''}
      ${(a.cant || []).map(c => `<p class="cant">${esc(c)}</p>`).join('')}
      ${a.changed ? `<p class="hint">${editWorking(e) ? '<span class="spin"></span> Making it…' : 'Done — Undo puts the version before back.'}</p>` : ''}
    </div></div>`).join('');
}
async function askEdit() {
  const text = $('ee-ask-text').value.trim();
  if (!text) { toast('Type what you’d like changed', true); $('ee-ask-text').focus(); return; }
  if (Object.keys(EE.draft).length && !confirm('You have changes you haven’t re-made yet. Drop them and do this instead?')) return;
  const done = busy($('ee-ask-go'), 'Reading…');
  try {
    const out = await post(`/api/edits/${EE.id}/ask`, { text });
    $('ee-ask-text').value = '';
    EE.draft = {}; EE.moments = null;
    if (out.reply.question) toast('ClipAgent has a question — see below');
    EE.edit = null;
    watchEdit();
  } catch (err) { toast(err.message, true); }
  finally { done(); }
}
$('ee-ask-go').addEventListener('click', askEdit);
$('ee-ask-text').addEventListener('keydown', ev => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) askEdit(); });
$('ee-ask-text').addEventListener('input', () => autoGrow($('ee-ask-text')));

$('ee-savepost').addEventListener('click', async () => {
  const done = busy($('ee-savepost'), 'Saving…');
  try {
    const e = await post(`/api/edits/${EE.id}/post`, { caption: $('ee-caption').value, hashtags: $('ee-tags').value.split(/[\s,]+/).filter(Boolean) });
    EE.edit = e; renderEditPreview(e); toast('Caption saved');
  } catch (err) { toast(err.message, true); }
  finally { done(); }
});
$('ee-delete').addEventListener('click', async () => {
  if (!confirm('Delete this edit and its video? Your footage and songs stay.')) return;
  try { await api(`/api/edits/${EE.id}`, { method: 'DELETE' }); toast('Edit deleted'); location.hash = '#/edits'; }
  catch (err) { toast(err.message, true); }
});
