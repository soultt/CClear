/* ==========================================================================
   CClear · front-end controller (vanilla JS)
   ========================================================================== */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PAGE_META = {
  overview: ['总览', '磁盘概况与一键优化'],
  cleaner: ['垃圾清理', '临时文件 / 缓存 / 回收站清理'],
  registry: ['注册表清理', '失效项扫描 · 安全分级 · 备份后清理'],
  large: ['大文件转移', '定位大文件并迁移到其他磁盘'],
  dir: ['目录迁移', '目录搬家 · 联接 · 注册表同步'],
  apps: ['应用迁移', '已安装程序整体迁移'],
  journal: ['迁移记录', '可回滚的操作历史'],
  settings: ['设置', '迁移默认值与安全维护'],
  about: ['关于', '版本信息 · 开源仓库'],
};

const state = {
  overview: {},
  settings: {},
  drives: [],
  targets: [],
  junk: { cats: [], sel: new Set(), sizes: {} },
  junkGroup: '全部',
  reg: { cats: [], sel: new Set() },
  regLevel: '全部',
  large: { files: [], sel: new Set() },
  apps: { list: [], sel: new Set(), filter: 'all', query: '', running: new Set(), sort: { key: '', dir: 'asc' } },
  journal: [],
  dirPlan: null,
  target: '',
  appTarget: '',
  busy: false,
};

/* ---------------------------------------------------------------- helpers */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const bytes = (n) => {
  n = Number(n) || 0;
  if (n < 1024) return `${Math.round(n)} B`;
  const u = ['KB', 'MB', 'GB', 'TB']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${u[i]}`;
};
const timeStr = (t) => {
  if (!t) return '–';
  const d = new Date(t * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const shortPath = (p, head = 26, tail = 24) => {
  if (!p) return '–';
  return p.length > head + tail + 3 ? `${p.slice(0, head)}…${p.slice(-tail)}` : p;
};

async function api(path, body, opts = {}) {
  const init = { method: opts.method || (body === undefined ? 'GET' : 'POST'), headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) { /* ignore */ }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return res.status === 204 ? {} : res.json();
}

function toast(title, body = '', kind = 'info', ms = 4600) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<div class="t-title">${esc(title)}</div>${body ? `<div class="t-body">${esc(body)}</div>` : ''}`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(30px)'; el.style.transition = 'all .3s'; }, ms);
  setTimeout(() => el.remove(), ms + 350);
}

/* Give a click an immediate, unmistakable visual answer: the button turns
   disabled and shows a spinner + label while the action runs. Toasts alone can
   be missed (and animation may be paused in a background tab), which makes a
   slow action look like a dead button. */
async function withBusy(btn, busyText, fn) {
  if (!btn) return fn();
  if (btn.disabled) return undefined;            // ignore double clicks
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<svg class="spin"><use href="#i-refresh"/></svg>${esc(busyText)}`;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

const icon = (name, style = '') => `<svg${style ? ` style="${style}"` : ''}><use href="#i-${name}"/></svg>`;

/* ------------------------------------------------------------- job runner */
/* Several tasks can run at the same time (a scan while a clean-up finishes, a
   registry backup while a migration runs, …). The bar therefore keeps one row
   per job: a job that ends retires only its own row and the bar disappears when
   the last row is gone - never before. The log panel belongs to the job whose
   日志 button was toggled last. */
const jobRows = new Map();          // jobId -> { row, logs: [html], open: bool }
let jobLogOwner = null;             // jobId the log panel is showing

const JOB_ICON = { running: 'play', pending: 'play', done: 'check', failed: 'alert', cancelled: 'alert' };
const JOB_STATUS_ZH = { done: '已完成', failed: '失败', cancelled: '已取消' };

function jobBar() { $('#jobbar').classList.toggle('show', jobRows.size > 0); }

function ensureJobRow(jobId, title) {
  let entry = jobRows.get(jobId);
  if (entry) return entry;
  const row = document.createElement('div');
  row.className = 'jobrow';
  row.dataset.job = jobId;
  row.innerHTML = `
    <svg class="ji" style="width:17px;color:var(--accent-2)"><use href="#i-play"/></svg>
    <div class="title"></div>
    <div class="progress"><i></i></div>
    <div class="pct">0%</div>
    <div class="msg"></div>
    <button class="btn sm ghost" data-act="log">日志</button>
    <button class="btn sm danger" data-act="cancel">取消</button>`;
  row.querySelector('.title').textContent = title;
  row.querySelector('[data-act="log"]').onclick = () => toggleJobLog(jobId);
  row.querySelector('[data-act="cancel"]').onclick = () => cancelJob(jobId);
  $('#jobRows').appendChild(row);
  entry = { row, logs: [] };
  jobRows.set(jobId, entry);
  jobBar();
  return entry;
}

function dropJobRow(jobId) {
  const entry = jobRows.get(jobId);
  if (!entry) return;
  entry.row.remove();
  jobRows.delete(jobId);
  if (jobLogOwner === jobId) {
    jobLogOwner = null;
    $('#jobLog').style.display = 'none';
    $('#jobLog').innerHTML = '';
  }
  jobBar();
}

function jobLogLines(j) {
  return (j.logs || []).map((l) =>
    `<div class="l ${l.level}"><span class="t">${timeStr(l.t).slice(-8)}</span><span class="m">${esc(l.message)}</span></div>`);
}

function toggleJobLog(jobId) {
  const panel = $('#jobLog');
  if (jobLogOwner === jobId && panel.style.display !== 'none') {
    logClose();
    return;
  }
  jobLogOwner = jobId;
  const entry = jobRows.get(jobId);
  panel.innerHTML = entry ? entry.logs.join('') : '';
  panel.style.display = 'block';
  panel.scrollTop = panel.scrollHeight;
}

function logClose() {
  jobLogOwner = null;
  const panel = $('#jobLog');
  panel.style.display = 'none';
  panel.innerHTML = '';
}

function cancelJob(jobId) {
  const entry = jobRows.get(jobId);
  const btn = entry && entry.row.querySelector('[data-act="cancel"]');
  if (btn) { btn.disabled = true; btn.textContent = '取消中…'; }
  api(`/api/jobs/${jobId}/cancel`, {}).catch((e) => toast('取消失败', e.message, 'error'));
}

function renderJob(j) {
  const entry = ensureJobRow(j.id, j.title || j.kind);
  const { row } = entry;
  const live = j.status === 'running' || j.status === 'pending';
  row.querySelector('.title').textContent = j.title || j.kind;
  row.querySelector('.progress > i').style.width = `${Math.max(2, j.percent)}%`;
  row.querySelector('.pct').textContent = j.percent >= 100 ? '100%' : `${Math.floor(j.percent)}%`;
  row.querySelector('.msg').textContent = j.message || '';
  row.querySelector('.ji').innerHTML = `<use href="#i-${JOB_ICON[j.status] || 'play'}"/>`;
  row.classList.toggle('settled', !live);
  row.classList.toggle('bad', j.status === 'failed' || j.status === 'cancelled');
  row.querySelector('[data-act="cancel"]').style.display = live ? '' : 'none';
  const lines = jobLogLines(j);
  if (lines.length) {
    entry.logs.push(...lines);
    if (entry.logs.length > 400) entry.logs.splice(0, entry.logs.length - 400);
    if (jobLogOwner === j.id) {
      const panel = $('#jobLog');
      panel.insertAdjacentHTML('beforeend', lines.join(''));
      panel.scrollTop = panel.scrollHeight;
    }
  }
  if (!live) row.dataset.status = j.status;
}

function trackJob(jobId, title, handlers = {}) {
  return new Promise((resolve) => {
    let offset = 0;
    ensureJobRow(jobId, title);
    const tick = async () => {
      let j;
      try {
        j = await api(`/api/jobs/${jobId}?offset=${offset}`);
      } catch (err) {
        toast('无法获取任务状态', err.message, 'error');
        dropJobRow(jobId);
        resolve(null);
        return;
      }
      offset = j.log_count;
      renderJob(j);
      if (typeof handlers.onProgress === 'function') handlers.onProgress(j);
      if (!['running', 'pending'].includes(j.status)) {
        // Keep this job's own row readable, then retire it - other jobs keep
        // their rows (and their progress) untouched.
        setTimeout(() => dropJobRow(jobId), j.status === 'done' ? 2500 : 6000);
        if (j.status === 'failed') toast('任务失败', j.error || '', 'error', 8000);
        else if (j.status === 'cancelled') toast('已取消', title || '', 'warn');
        if (handlers.onDone) handlers.onDone(j);
        resolve(j);
        return;
      }
      setTimeout(tick, 550);
    };
    setTimeout(tick, 350);
  });
}

async function runJob(path, body, title, handlers) {
  try {
    const { job_id } = await api(path, body);
    return await trackJob(job_id, title, handlers);
  } catch (err) {
    toast('请求失败', err.message, 'error', 8000);
    return null;
  }
}

/* ------------------------------------------------------------------ modal */
function openModal(html, wide = false) {
  const modal = $('#modal');
  modal.className = `modal${wide ? ' wide' : ''}`;
  modal.innerHTML = html;
  $('#overlay').classList.add('show');
}
function closeModal() { $('#overlay').classList.remove('show'); $('#modal').innerHTML = ''; }
$('#overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

function confirmModal({ title, body, confirmText = '确认', danger = false, onConfirm }) {
  openModal(`
    <div class="modal-head">${icon(danger ? 'alert' : 'info', 'width:18px;color:var(--warn)')}<h3>${esc(title)}</h3></div>
    <div class="modal-body"><div style="font-size:13.5px;line-height:1.8;color:var(--text-dim)">${body}</div></div>
    <div class="modal-foot"><div class="spacer"></div>
      <button class="btn" data-close>取消</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" data-ok>${esc(confirmText)}</button>
    </div>`);
  $('[data-close]').onclick = closeModal;
  $('[data-ok]').onclick = async () => { closeModal(); await onConfirm(); };
}

/* ------------------------------------------------------------------- nav */
$$('.nav-item').forEach((el) => el.addEventListener('click', () => go(el.dataset.page)));
document.addEventListener('click', (e) => {
  const g = e.target.closest('[data-goto]');
  if (g) go(g.dataset.goto);
});

/* Every external link (sidebar footer, about page) goes to the system browser:
   the desktop window has no tabs or address bar, so loading the site inside it
   would leave the user with no way back to the app. */
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-external]');
  if (!a) return;
  e.preventDefault();
  openExternal(a.dataset.external || a.getAttribute('href') || '');
});

function go(page) {
  $$('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.page === page));
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${page}`));
  const [t, c] = PAGE_META[page] || ['', ''];
  $('#pageTitle').textContent = t; $('#pageCrumb').textContent = c;
  $('.content').scrollTop = 0;
  if (page === 'journal') loadJournal();
  if (page === 'apps' && !state.apps.list.length) loadApps();
  if (page === 'settings') fillSettings();
  if (page === 'about') fillAbout();
  if (page === 'registry') renderReg();
  if (page === 'dir') renderDrivePick(state.target);
}

/* =========================================================================
   OVERVIEW
   ========================================================================= */
async function loadOverview() {
  try {
    const o = await api('/api/overview');
    state.overview = o;
    state.drives = o.drives || [];
    state.targets = o.targets || [];
    state.settings = o.settings || {};
    state.target = state.settings.target_drive || (state.targets[0] && state.targets[0].letter) || '';
    state.appTarget = state.target;
    renderOverview();
    renderDriveOptions();
    renderDrivePick(state.target);
  } catch (err) {
    toast('无法连接后端', err.message, 'error');
  }
}

function renderOverview() {
  const o = state.overview;
  const sys = o.system || {};
  const usedPct = sys.total ? (sys.used / sys.total) * 100 : 0;
  $('#heroRing').style.setProperty('--p', usedPct.toFixed(1));
  $('#heroPct').textContent = `${usedPct.toFixed(0)}%`;
  $('#heroTitle').textContent = sys.total
    ? `${o.system_drive} 盘已用 ${bytes(sys.used)}，剩余 ${bytes(sys.free)}`
    : '磁盘信息不可用';
  $('#heroText').textContent = usedPct > 88
    ? '系统盘空间已经告急！建议先清理垃圾缓存，再把大文件与应用迁移到其他磁盘，可以立刻腾出可观空间。'
    : 'CClear 可以清理垃圾缓存、把大文件与目录迁到其他磁盘，并自动同步注册表与快捷方式，迁移后原位置会留下目录联接，程序照常运行。';

  $('#stFree').textContent = bytes(sys.free || 0);
  const freePct = sys.total ? (sys.free / sys.total) * 100 : 0;
  $('#stFreeHint').textContent = `共 ${bytes(sys.total || 0)} · 可用 ${freePct.toFixed(0)}%`;

  const q = o.quick || {};
  $('#stJunk').textContent = q.total_human || '未扫描';
  $('#navCleanPill').textContent = q.total_human || '–';
  $('#stJunkHint').textContent = o.last_scan ? `上次扫描 ${timeStr(o.last_scan)}` : '勾选项目后点击“扫描选中项”开始估算';

  const jn = o.journal || {};
  $('#stFreed').textContent = bytes(jn.freed_bytes || 0);
  $('#stFreedHint').textContent = `共 ${jn.migrated || 0} 次迁移 · ${jn.active || 0} 项生效`;
  $('#navJournalPill').textContent = jn.total || 0;

  const sf = (o.system_files || []).reduce((a, b) => a + b.size, 0);
  $('#stSysFiles').textContent = bytes(sf);
  $('#topDrive').textContent = o.system_drive || 'C:';
  $('#sideAdmin').innerHTML = o.admin
    ? `<span style="color:var(--success)">● 管理员权限</span>`
    : `<span style="color:var(--warn)">● 普通权限</span>`;
  $('#sideVersion').textContent = `v${o.version || '-'} · 本地运行`;
  $('#sideTarget').textContent = state.target ? `目标盘：${state.target}` : '未设置目标盘';
  $('#btnElevate').style.display = o.admin ? 'none' : '';
  $('#btnElevate2').style.display = o.admin ? 'none' : '';
  $('#setAdmin').textContent = o.admin ? '已获得' : '未获得（部分功能受限）';
  $('#setSysDrive').textContent = o.system_drive || '–';
  $('#setOs').textContent = `${(o.os || {}).name || '–'}`;
  $('#setDataDir').textContent = (o.paths || {}).data || '–';
  $('#setBackupDir').textContent = (o.paths || {}).backups || '–';

  $('#driveList').innerHTML = state.drives.map((d) => {
    const p = d.total ? (d.used / d.total) * 100 : 0;
    const cls = p > 90 ? 'danger' : p > 78 ? 'warn' : '';
    return `<div class="drive-row">
      <div class="drive-top">
        <div class="drive-badge ${d.is_system ? 'sys' : ''}">${d.letter.replace(':', '')}</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:13px;font-weight:600">${esc(d.label || '本地磁盘')} ${d.is_system ? '<span class="badge plain" style="margin-left:6px">系统盘</span>' : ''}</div>
          <div class="small muted">${d.fs || ''} · 剩余 ${bytes(d.free)} / ${bytes(d.total)}</div>
        </div>
        <div class="small dim">${p.toFixed(0)}%</div>
      </div>
      <div class="meter ${cls}"><i style="width:${p.toFixed(1)}%"></i></div>
    </div>`;
  }).join('') || '<div class="empty small">未检测到可用磁盘</div>';

  if ((o.hotspots || []).length) renderHotspots({ children: o.hotspots }, true);
  loadRecent();
}

function renderHotspots(node, fromCache = false) {
  const list = (node.children || []).filter((c) => c.size > 0);
  if (!list.length) { $('#hotspotList').innerHTML = '<div class="empty small">没有可展示的数据</div>'; return; }
  const max = Math.max(...list.map((c) => c.size));
  $('#hotspotList').innerHTML = list.slice(0, 14).map((c) => `
    <div class="bar-item">
      <div>
        <div class="nm"><span class="nowrap" title="${esc(c.path)}">${esc(c.name)}</span><i>${bytes(c.size)}</i></div>
        <div class="meter"><i style="width:${((c.size / max) * 100).toFixed(1)}%"></i></div>
      </div>
      <div class="small dim" style="text-align:right">
        <button class="btn sm ghost" data-hot-migrate="${esc(c.path)}" title="迁移该目录">迁移</button>
      </div>
    </div>`).join('');
}

async function loadRecent() {
  try {
    const { records } = await api('/api/journal');
    state.journal = records;
    const rows = records.slice(0, 5);
    $('#recentList').innerHTML = rows.length ? `<div class="table-wrap" style="max-height:none"><table>
      <thead><tr><th style="width:150px">时间</th><th>操作</th><th>路径</th><th class="num" style="width:90px">大小</th><th style="width:90px">状态</th></tr></thead>
      <tbody>${rows.map(recentRow).join('')}</tbody></table></div>` : '<div class="empty small">暂无操作记录</div>';
  } catch (e) { /* ignore */ }
}

function recentRow(r) {
  const kind = typeLabel(r.action);
  return `<tr>
    <td class="small muted">${timeStr(r.time)}</td>
    <td><span class="badge ${kind.cls}">${kind.text}</span></td>
    <td class="mono nowrap" title="${esc(r.src || r.label || '')}">${esc(shortPath(r.src || r.label || '–', 30, 26))}</td>
    <td class="num">${r.size_bytes ? bytes(r.size_bytes) : '–'}</td>
    <td>${r.reverted ? '<span class="badge plain">已还原</span>' : '<span class="badge ok">完成</span>'}</td>
  </tr>`;
}

const typeLabel = (a) => ({
  move_app: { text: '应用迁移', cls: 'violet' },
  move_dir: { text: '目录迁移', cls: 'info' },
  move_file: { text: '文件转移', cls: 'info' },
  clean: { text: '垃圾清理', cls: 'safe' },
  regclean: { text: '注册表清理', cls: 'violet' },
  delete_files: { text: '删除文件', cls: 'caution' },
}[a] || { text: a || '操作', cls: 'plain' });

document.addEventListener('click', (e) => {
  const m = e.target.closest('[data-hot-migrate]');
  if (m) { go('dir'); $('#dirSrc').value = m.dataset.hotMigrate; updateSteps(2); }
});

/* =========================================================================
   CLEANER
   ========================================================================= */
const RISK_RANK = { safe: 0, caution: 1, advanced: 2 };
const RISK_TEXT = { safe: '安全', caution: '注意', advanced: '高级' };
const RISK_LEVELS = [
  { key: 'safe', label: '安全', hint: '可直接清理，不影响系统与已安装程序' },
  { key: 'caution', label: '注意', hint: '可能影响下次启动或需重新下载，请确认后清理' },
  { key: 'advanced', label: '高级', hint: '涉及系统回退或重建成本较高，建议逐项确认' },
];

/* Cards are always ordered by safety level (safe → caution → advanced), then
   by the declared group order, so the riskiest items never appear first. */
function sortByRisk(list) {
  return [...list].sort((a, b) => (RISK_RANK[a.risk] ?? 9) - (RISK_RANK[b.risk] ?? 9));
}

function renderJunk() {
  const cats = sortByRisk(state.junk.cats);
  const grid = $('#junkGrid');
  if (!cats.length) { grid.innerHTML = '<div class="empty">暂无清理项</div>'; return; }
  const list = state.junkGroup === '全部' ? cats : cats.filter((c) => c.group === state.junkGroup);
  if (!list.length) { grid.innerHTML = '<div class="empty">该分组下暂无清理项</div>'; updateCleanButton(); return; }
  grid.innerHTML = RISK_LEVELS.map((lv) => {
    const items = list.filter((c) => c.risk === lv.key);
    if (!items.length) return '';
    const scannedItems = items.filter((c) => state.junk.sizes[c.id] !== undefined);
    const total = scannedItems.reduce((a, c) => a + (state.junk.sizes[c.id] || 0), 0);
    return `<div class="risk-group">
      <div class="risk-group-head">
        <span class="dot ${lv.key}"></span>
        <span class="lv ${lv.key}">${lv.label}</span>
        <span class="cnt">${items.length} 项${scannedItems.length ? ` · 已扫描 ${scannedItems.length} 项 · ${bytes(total)}` : ''}</span>
        <span class="hint">${lv.hint}</span>
      </div>
      <div class="junk-grid">${items.map(junkCard).join('')}</div>
    </div>`;
  }).filter(Boolean).join('');
  updateCleanButton();
}

function junkCard(c) {
  const on = state.junk.sel.has(c.id);
  const size = state.junk.sizes[c.id];
  const locked = c.admin && !state.overview.admin;
  const items = (c.items || []).slice(0, 6);
  return `<div class="junk-card ${on ? 'on' : ''} ${locked ? 'locked' : ''}" data-cat="${c.id}">
    <div class="junk-top">
      <div class="junk-icon">${icon(catIcon(c.icon), 'width:18px;height:18px')}</div>
      <div style="flex:1;min-width:0">
        <div class="junk-name">${esc(c.name)}
          <span class="badge ${c.risk}">${RISK_TEXT[c.risk] || c.risk}</span>
          ${c.admin ? `<span class="badge plain" title="需要管理员权限">${icon('lock', 'width:11px;height:11px')}管理员</span>` : ''}
        </div>
        ${items.length ? `<div class="junk-items">${items.map((i) => `<div title="${esc(i.path)}">${esc(i.path)}</div>`).join('')}</div>` : ''}
      </div>
      <span class="check ${on ? 'on' : ''}" data-toggle="${c.id}"></span>
    </div>
    <div class="junk-desc">${esc(c.desc)}</div>
    <div class="junk-meta">
      <div class="junk-size ${size ? '' : 'zero'}">${size === undefined ? '待扫描' : bytes(size)}</div>
      <div class="spacer"></div>
      <span class="badge plain">${c.group}</span>
    </div>
  </div>`;
}

const catIcon = (i) => ({ temp: 'folder', bug: 'alert', image: 'chart', update: 'refresh', windows: 'disk', log: 'file', speed: 'play', recycle: 'undo', history: 'history', globe: 'open', chrome: 'app', edge: 'app', firefox: 'app', code: 'package', package: 'package', trash: 'trash' }[i] || 'trash');

function updateCleanButton() {
  const ids = [...state.junk.sel];
  const size = ids.reduce((a, id) => a + (state.junk.sizes[id] || 0), 0);
  const scanned = ids.filter((id) => state.junk.sizes[id] !== undefined).length;
  const btn = $('#btnJunkClean');
  btn.disabled = ids.length === 0;
  btn.innerHTML = `${icon('broom')}清理选中项${ids.length ? `（${ids.length} 项${scanned ? ` · ${bytes(size)}` : ' · 未扫描'}）` : ''}`;
}

$('#junkGrid').addEventListener('click', (e) => {
  const card = e.target.closest('[data-cat]');
  if (!card) return;
  const id = card.dataset.cat;
  const cat = state.junk.cats.find((x) => x.id === id);
  if (cat && cat.admin && !state.overview.admin) {
    toast('需要管理员权限', `“${cat.name}”需要管理员权限：请点击右上角「获取管理员权限」，在 UAC 提示中确认后会以管理员身份重新启动（本窗口会自动关闭）。`, 'warn', 9000);
    return;
  }
  if (state.junk.sel.has(id)) state.junk.sel.delete(id); else state.junk.sel.add(id);
  card.classList.toggle('on', state.junk.sel.has(id));
  const chk = $('.check', card); if (chk) chk.classList.toggle('on', state.junk.sel.has(id));
  updateCleanButton();
});

$$('[data-group]').forEach((chip) => chip.addEventListener('click', () => {
  $$('[data-group]').forEach((c) => c.classList.remove('on'));
  chip.classList.add('on');
  state.junkGroup = chip.dataset.group;
  renderJunk();
}));

$('#btnSelectAll').onclick = () => {
  state.junk.sel.clear();
  state.junk.cats.forEach((c) => { if (!(c.admin && !state.overview.admin)) state.junk.sel.add(c.id); });
  renderJunk();
  updateCleanButton();
};
$('#btnSelectNone').onclick = () => { state.junk.sel.clear(); renderJunk(); };
$('#btnSelectSafe').onclick = () => {
  state.junk.sel.clear();
  state.junk.cats
    .filter((c) => c.risk === 'safe' && !(c.admin && !state.overview.admin))
    .forEach((c) => state.junk.sel.add(c.id));
  renderJunk();
  const blocked = state.junk.cats.filter((c) => c.risk === 'safe' && c.admin && !state.overview.admin).length;
  toast('已选择安全项',
    `共 ${state.junk.sel.size} 项，不含“注意/高级”类别${blocked ? `；另有 ${blocked} 项需要管理员权限，未选中` : ''}`,
    'ok');
};

$('#btnJunkScan').onclick = () => {
  // Scan only what the user ticked - an unchecked card is never analysed.
  const ids = [...state.junk.sel];
  if (!ids.length) {
    toast('请先选择项目', '勾选要扫描的清理项目（默认已选中安全项），再点击“扫描选中项”。', 'warn');
    return;
  }
  return scanJunk(ids);
};

async function scanJunk(ids) {
  await runJob('/api/junk/scan', { ids }, '扫描垃圾文件', {
    onDone: (j) => {
      if (j.status !== 'done') return;
      const res = j.result || {};
      const byId = new Map((res.categories || []).map((c) => [c.id, c]));
      // Only the ticked projects were scanned; keep the rest as they were.
      state.junk.sizes = {};
      state.junk.cats = state.junk.cats.map((c) => (byId.has(c.id) ? { ...c, ...byId.get(c.id) } : c));
      state.junk.cats.forEach((c) => { if (byId.has(c.id)) state.junk.sizes[c.id] = c.size; });
      const scannedTotal = (res.categories || []).reduce((a, c) => a + (c.size || 0), 0);
      state.overview.quick = { total: scannedTotal, total_human: res.total_human };
      $('#stJunk').textContent = res.total_human;
      $('#navCleanPill').textContent = res.total_human;
      $('#stJunkHint').textContent = `已扫描 ${ids.length} 个项目${ids.length < state.junk.cats.length ? ' · 部分结果' : ''}`;
      renderJunk();
      toast('扫描完成', `已扫描 ${ids.length} 个项目，共发现 ${res.total_human} 可清理内容`, 'ok');
    },
  });
};

$('#btnJunkClean').onclick = () => {
  const ids = [...state.junk.sel];
  if (!ids.length) return;
  const size = ids.reduce((a, id) => a + (state.junk.sizes[id] || 0), 0);
  const risky = ids.filter((id) => {
    const c = state.junk.cats.find((x) => x.id === id); return c && c.risk !== 'safe';
  });
  const adminNeeded = ids.filter((id) => {
    const c = state.junk.cats.find((x) => x.id === id); return c && c.admin;
  });
  let body = `<div>即将清理 <b>${ids.length}</b> 个类别，预计释放 <b>${bytes(size)}</b>。</div>`;
  body += `<ul style="margin:12px 0 0 18px;line-height:1.9">${ids.map((id) => {
    const c = state.junk.cats.find((x) => x.id === id) || {};
    return `<li>${esc(c.name || id)}${c.risk !== 'safe' ? ` <span class="badge ${c.risk}">${RISK_TEXT[c.risk] || c.risk}</span>` : ''}</li>`;
  }).join('')}</ul>`;
  if (risky.length && state.junk.sel.has('recycle_bin')) body += `<div class="note warn" style="margin-top:12px">${icon('alert', 'width:15px')}<div>包含回收站，清空后文件将无法恢复。</div></div>`;
  if (adminNeeded.length && !state.overview.admin) body += `<div class="note warn" style="margin-top:12px">${icon('lock', 'width:15px')}<div>其中 ${adminNeeded.length} 项需要管理员权限，普通权限下无法执行：请先点击右上角「获取管理员权限」，UAC 确认后本程序会以管理员身份重新启动，再执行即可。</div></div>`;
  const unscanned = ids.filter((id) => state.junk.sizes[id] === undefined);
  if (unscanned.length) body += `<div class="note warn" style="margin-top:12px">${icon('info', 'width:15px')}<div>其中 ${unscanned.length} 项尚未扫描，将按清理规则直接处理，释放大小未计入上方估算。</div></div>`;
  confirmModal({
    title: '确认清理所选项目？', body, confirmText: '开始清理', danger: true,
    onConfirm: async () => {
      await runJob('/api/junk/clean', { ids }, '清理垃圾文件', {
        onDone: (j) => {
          if (j.status !== 'done') return;
          const r = j.result || {};
          const fails = (r.details || []).filter((d) => d.ok === false);
          if (fails.length) {
            const reason = (fails[0].message || '').split('\n')[0].slice(0, 70);
            toast('清理完成，部分未成功',
              `${fails.length} 项未完成（${fails[0].category || ''}）：${reason}　｜　已释放 ${r.freed_human}`,
              'warn', 12000);
          } else {
            toast('清理完成', `释放 ${r.freed_human}，删除 ${r.removed} 项，跳过 ${r.skipped} 项`, 'ok', 7000);
          }
          state.junk.sel.clear();
          scanJunk(ids);
          loadOverview();
        },
      });
    },
  });
};

/* =========================================================================
   REGISTRY CLEANER
   ========================================================================= */
const REG_HINTS = {
  safe: '失效的启动项、应用路径与隐私记录，可直接清理',
  caution: '会删除卸载信息，请先确认对应程序确实已卸载',
  advanced: '涉及 COM 组件注册，建议保留备份一段时间',
};

function regCat(id) { return state.reg.cats.find((c) => c.id === id) || {}; }

function renderReg() {
  const grid = $('#regGrid');
  const cats = state.reg.cats;
  if (!cats.length) {
    grid.innerHTML = '<div class="empty">点击“扫描选中项”检查注册表残留</div>';
    updateRegButton();
    return;
  }
  const levels = RISK_LEVELS
    .filter((lv) => state.regLevel === '全部' || state.regLevel === lv.key)
    .map((lv) => {
      const list = cats.filter((c) => c.risk === lv.key);
      if (!list.length) return '';
      const total = list.reduce((a, c) => a + (c.count || 0), 0);
      const scannedCount = list.filter((c) => c.scanned).length;
      return `<div class="risk-group">
        <div class="risk-group-head">
          <span class="dot ${lv.key}"></span>
          <span class="lv ${lv.key}">${lv.label}</span>
          <span class="cnt">${list.length} 组${scannedCount ? ` · 已扫描 ${scannedCount} 组 · ${total} 项` : ''}</span>
          <span class="hint">${REG_HINTS[lv.key] || lv.hint}</span>
        </div>
        ${list.map(regCard).join('')}
      </div>`;
    }).filter(Boolean).join('');
  grid.innerHTML = levels || '<div class="empty">该安全等级下暂无项目</div>';
  updateRegButton();
}

function regCard(c) {
  const on = state.reg.sel.has(c.id);
  const locked = c.admin && !state.overview.admin;
  const scanned = !!c.scanned;
  const count = scanned ? (c.count || 0) : null;
  const items = scanned ? (c.items || []).slice(0, 6) : [];
  return `<div class="reg-card ${on ? 'on' : ''} ${locked ? 'locked' : ''}" data-reg="${c.id}">
    <div class="reg-card-top">
      <span class="check ${on ? 'on' : ''}"></span>
      <span class="name">${esc(c.name)}</span>
      <span class="badge ${c.risk}">${RISK_TEXT[c.risk] || c.risk}</span>
      ${c.admin ? `<span class="badge plain" title="需要管理员权限">${icon('lock', 'width:11px;height:11px')}管理员</span>` : ''}
      <span class="num">${count === null ? '待扫描' : `${count} 项`}</span>
    </div>
    <div class="reg-card-desc">${esc(c.desc)}</div>
    ${items.length ? `<div class="reg-items">${items.map((i) => `
      <div class="reg-item" title="${esc(i.key)}${i.target ? `\n${esc(i.target)}` : ''}">
        <span class="k">${esc(i.label)}</span><span class="t">${esc(i.issue)}${i.target ? ` · ${esc(shortPath(i.target, 26, 26))}` : ''}</span>
      </div>`).join('')}</div>` : ''}
  </div>`;
}

function updateRegButton() {
  const ids = [...state.reg.sel];
  const ready = ids.filter((id) => { const c = regCat(id); return c.scanned && c.count; });
  const items = ready.reduce((a, id) => a + (regCat(id).count || 0), 0);
  const btn = $('#btnRegClean');
  btn.disabled = ready.length === 0;
  btn.innerHTML = `${icon('shield')}清理选中项${ready.length ? `（${ready.length} 组 · ${items} 项）` : ''}`;
}

$('#regGrid').addEventListener('click', (e) => {
  const card = e.target.closest('[data-reg]');
  if (!card) return;
  const id = card.dataset.reg;
  const cat = regCat(id);
  if (cat.admin && !state.overview.admin) {
    toast('需要管理员权限', `“${cat.name}”需要管理员权限：请点击右上角「获取管理员权限」，在 UAC 提示中确认后会以管理员身份重新启动（本窗口会自动关闭）。`, 'warn', 9000);
    return;
  }
  if (state.reg.sel.has(id)) state.reg.sel.delete(id); else state.reg.sel.add(id);
  card.classList.toggle('on', state.reg.sel.has(id));
  const chk = $('.check', card); if (chk) chk.classList.toggle('on', state.reg.sel.has(id));
  updateRegButton();
});

$$('[data-reglevel]').forEach((chip) => chip.addEventListener('click', () => {
  $$('[data-reglevel]').forEach((c) => c.classList.remove('on'));
  chip.classList.add('on');
  state.regLevel = chip.dataset.reglevel;
  renderReg();
}));

$('#btnRegSelectAll').onclick = () => {
  state.reg.sel.clear();
  state.reg.cats.forEach((c) => { if (!(c.admin && !state.overview.admin)) state.reg.sel.add(c.id); });
  renderReg();
};
$('#btnRegSelectNone').onclick = () => { state.reg.sel.clear(); renderReg(); };
$('#btnRegSelectSafe').onclick = () => {
  state.reg.sel.clear();
  state.reg.cats
    .filter((c) => c.risk === 'safe' && !(c.admin && !state.overview.admin))
    .forEach((c) => state.reg.sel.add(c.id));
  renderReg();
  const blocked = state.reg.cats.filter((c) => c.risk === 'safe' && c.admin && !state.overview.admin).length;
  toast('已选择安全项',
    `共 ${state.reg.sel.size} 组，不含“注意/高级”类别${blocked ? `；另有 ${blocked} 组需要管理员权限，未选中` : ''}`,
    'ok');
};

$('#btnRegScan').onclick = () => {
  // Only the ticked projects are inspected; the rest stay "待扫描".
  const ids = [...state.reg.sel];
  if (!ids.length) {
    toast('请先选择项目', '勾选要检查的注册表项目（默认已选中安全项），再点击“扫描选中项”。', 'warn');
    return;
  }
  return scanReg(ids);
};

async function scanReg(ids) {
  await runJob('/api/regclean/scan', { ids }, '扫描注册表残留', {
    onDone: (j) => {
      if (j.status !== 'done') return;
      const res = j.result || {};
      const byId = new Map((res.categories || []).map((c) => [c.id, c]));
      state.reg.cats = state.reg.cats.map((c) => (byId.has(c.id) ? { ...c, ...byId.get(c.id), scanned: true } : c));
      renderReg();
      const found = res.total || 0;
      $('#navRegPill').textContent = found ? `${found} 项` : '0';
      $('#regNotice').innerHTML = found
        ? `<div class="note ok" style="margin-top:12px">${icon('check', 'width:15px')}<div>本次扫描 ${ids.length} 组项目，发现 <b>${found}</b> 项可清理的注册表残留；结果已按安全等级排列。</div></div>`
        : `<div class="note ok" style="margin-top:12px">${icon('check', 'width:15px')}<div>本次扫描 ${ids.length} 组项目，没有发现注册表残留。</div></div>`;
      toast('注册表扫描完成', found ? `发现 ${found} 项可清理残留` : '未发现残留', 'ok');
    },
  });
}

$('#btnRegClean').onclick = () => {
  const ids = [...state.reg.sel].filter((id) => { const c = regCat(id); return c.scanned && c.count; });
  if (!ids.length) {
    toast('请先扫描选中项', '清理前需要先扫描，确认要删除的残留数量。', 'info');
    return;
  }
  const items = ids.reduce((a, id) => a + (regCat(id).count || 0), 0);
  const risky = ids.filter((id) => regCat(id).risk !== 'safe');
  const adminNeeded = ids.filter((id) => regCat(id).admin);
  let body = `<div>即将删除 <b>${items}</b> 项注册表残留（${ids.length} 组）。删除前会自动导出这些注册表项。</div>`;
  body += `<ul style="margin:12px 0 0 18px;line-height:1.9">${ids.map((id) => {
    const c = regCat(id);
    return `<li>${esc(c.name || id)} <span class="badge ${c.risk}">${RISK_TEXT[c.risk] || c.risk}</span> · ${c.count || 0} 项</li>`;
  }).join('')}</ul>`;
  if (risky.length) body += `<div class="note warn" style="margin-top:12px">${icon('alert', 'width:15px')}<div>包含 ${risky.length} 组“注意/高级”项目。若清理后某个程序出现异常，可在数据目录的注册表备份中导入对应 .reg 文件恢复。</div></div>`;
  if (adminNeeded.length && !state.overview.admin) body += `<div class="note warn" style="margin-top:12px">${icon('lock', 'width:15px')}<div>其中 ${adminNeeded.length} 组需要管理员权限，普通权限下会被自动跳过：请先点击右上角「获取管理员权限」，UAC 确认后本程序会以管理员身份重新启动，再执行即可。</div></div>`;
  confirmModal({
    title: '确认清理注册表残留？', body, confirmText: '备份并清理', danger: true,
    onConfirm: async () => {
      await runJob('/api/regclean/clean', { ids }, '清理注册表残留', {
        onDone: (j) => {
          if (j.status !== 'done') return;
          const r = j.result || {};
          const fails = (r.details || []).filter((d) => d.ok === false);
          const skipped = r.skipped || 0;
          if (fails.length) {
            const reason = (fails[0].message || '').split('\n')[0].slice(0, 70);
            toast('清理完成，部分未成功',
              `${fails.length} 项未完成（${fails[0].category || ''}）：${reason}　｜　已删除 ${r.removed} 项`,
              'warn', 12000);
          } else {
            toast('注册表清理完成',
              `已删除 ${r.removed} 项${skipped ? `，跳过 ${skipped} 组需管理员权限的项目` : ''}；备份位于数据目录的 backups 下`,
              'ok', 8000);
          }
          state.reg.sel.clear();
          scanReg(ids);
          loadOverview();
        },
      });
    },
  });
};

/* =========================================================================
   LARGE FILES
   ========================================================================= */
$('#btnLargeScan').onclick = async () => {
  const min_mb = Number($('#largeMin').value);
  const include_windows = $('#largeWindows').checked;
  $('#largeBody').innerHTML = `<tr><td colspan="6"><div class="empty">正在扫描…</div></td></tr>`;
  await runJob('/api/files/scan', { min_mb, include_windows }, '扫描大文件', {
    onDone: (j) => {
      if (j.status !== 'done') { $('#largeBody').innerHTML = `<tr><td colspan="6"><div class="empty">扫描未完成</div></td></tr>`; return; }
      const res = j.result || {};
      state.large.files = res.files || [];
      state.large.sel.clear();
      $('#largeStats').textContent = `${res.count} 个文件 · 合计 ${res.total_human}`;
      renderLarge();
      toast('扫描完成', `找到 ${res.count} 个大于 ${res.min_human} 的文件`, 'ok');
    },
  });
};

function renderLarge() {
  const files = state.large.files;
  if (!files.length) { $('#largeBody').innerHTML = `<tr><td colspan="6"><div class="empty">没有找到符合条件的文件</div></td></tr>`; return; }
  $('#largeBody').innerHTML = files.map((f, i) => `
    <tr class="${state.large.sel.has(f.path) ? 'selected' : ''}">
      <td><span class="check ${state.large.sel.has(f.path) ? 'on' : ''}" data-large="${i}"></span></td>
      <td><div class="nowrap" title="${esc(f.path)}" style="max-width:260px;font-weight:500">${esc(f.name)}</div>
          <div class="small muted nowrap" style="max-width:260px">${esc(f.ext || '未知类型')}</div></td>
      <td class="mono small nowrap" title="${esc(f.dir)}" style="max-width:340px">${esc(shortPath(f.dir, 34, 22))}</td>
      <td class="num" style="font-weight:600">${esc(f.size_human)}</td>
      <td class="small muted">${timeStr(f.modified)}</td>
      <td class="actions">
        <button class="btn sm" data-lmove="${i}">${icon('folder-move', 'width:13px')}转移</button>
        <button class="btn sm ghost" data-lopen="${i}" title="在资源管理器中打开">${icon('open', 'width:13px')}</button>
        <button class="btn sm danger" data-ldel="${i}" title="删除">${icon('trash', 'width:13px')}</button>
      </td>
    </tr>`).join('');
  updateLargeSel();
}

function updateLargeSel() {
  const n = state.large.sel.size;
  const size = state.large.files.filter((f) => state.large.sel.has(f.path)).reduce((a, f) => a + f.size, 0);
  $('#largeSelInfo').textContent = n ? `已选 ${n} 项 · ${bytes(size)}` : '未选择';
  $('#largeCheckAll').className = `check ${n && n === state.large.files.length ? 'on' : n ? 'partial' : ''}`;
}

$('#largeBody').addEventListener('click', (e) => {
  const t = e.target.closest('[data-large],[data-lmove],[data-lopen],[data-ldel]');
  if (!t) return;
  if (t.dataset.large !== undefined) {
    const f = state.large.files[Number(t.dataset.large)];
    if (state.large.sel.has(f.path)) state.large.sel.delete(f.path); else state.large.sel.add(f.path);
    renderLarge(); return;
  }
  if (t.dataset.lopen !== undefined) { api('/api/reveal', { path: state.large.files[Number(t.dataset.lopen)].path }); return; }
  if (t.dataset.ldel !== undefined) return deleteLarge([state.large.files[Number(t.dataset.ldel)].path]);
  if (t.dataset.lmove !== undefined) return moveFiles([state.large.files[Number(t.dataset.lmove)].path]);
});

$('#largeCheckAll').onclick = () => {
  if (state.large.sel.size === state.large.files.length) state.large.sel.clear();
  else state.large.files.forEach((f) => state.large.sel.add(f.path));
  renderLarge();
};

$('#btnLargeMove').onclick = () => {
  if (!state.large.sel.size) return toast('未选择文件', '请先勾选要转移的文件', 'warn');
  moveFiles([...state.large.sel]);
};
$('#btnLargeDelete').onclick = () => {
  if (!state.large.sel.size) return toast('未选择文件', '请先勾选要删除的文件', 'warn');
  deleteLarge([...state.large.sel]);
};

async function moveFiles(paths) {
  const target = $('#largeTarget').value || state.target;
  if (!target) return toast('缺少目标盘', '请先在设置中选择目标磁盘', 'warn');
  const size = state.large.files.filter((f) => paths.includes(f.path)).reduce((a, f) => a + f.size, 0) ||
    paths.length * 0;
  openModal(`
    <div class="modal-head">${icon('folder-move', 'width:18px;color:var(--accent-2)')}<h3>转移 ${paths.length} 个文件到 ${esc(target)}</h3></div>
    <div class="modal-body">
      <div class="note">${icon('info', 'width:15px')}<div>文件会被复制到目标磁盘的 <span class="mono">${esc(target)}\\${esc(state.settings.group || 'Migrated')}</span> 目录，校验通过后删除原文件；
      由于是单文件，会在原位置尝试创建符号链接（需要管理员权限），失败也不影响使用。</div></div>
      <div style="margin-top:14px;max-height:240px;overflow:auto" class="mono small">
        ${paths.slice(0, 40).map((p) => `<div class="nowrap" title="${esc(p)}">${esc(shortPath(p, 40, 30))}</div>`).join('')}
        ${paths.length > 40 ? `<div class="muted">…共 ${paths.length} 个文件</div>` : ''}
      </div>
      <div style="margin-top:16px" class="row">
        <label class="switch"><input type="checkbox" id="mvLink" checked><span class="track"></span><span class="small dim">在原位置创建符号链接</span></label>
        <label class="switch"><input type="checkbox" id="mvHash"><span class="track"></span><span class="small dim">校验文件指纹</span></label>
      </div>
    </div>
    <div class="modal-foot"><div class="spacer"></div>
      <button class="btn" data-close>取消</button>
      <button class="btn primary" data-go>${icon('play')}开始转移</button></div>`);
  $('[data-close]').onclick = closeModal;
  $('[data-go]').onclick = async () => {
    const link = $('#mvLink').checked; const verify_hash = $('#mvHash').checked;
    closeModal();
    await runJob('/api/move/batch', { sources: paths, target, link, verify_hash, group: state.settings.group || 'Migrated' },
      `转移 ${paths.length} 个文件`, {
        onDone: (j) => {
          if (j.status !== 'done') return;
          const r = j.result || {};
          toast('转移完成', `成功 ${r.succeeded} 项，失败 ${r.failed} 项，释放 ${r.freed_human}`, r.failed ? 'warn' : 'ok', 7000);
          state.large.sel.clear();
          state.large.files = state.large.files.filter((f) => !paths.includes(f.path));
          renderLarge(); loadOverview();
        },
      });
  };
}

function deleteLarge(paths) {
  confirmModal({
    title: `删除 ${paths.length} 个文件？`,
    body: `<div>这些文件将被移动到回收站（可在回收站还原）。</div>
      <div class="mono small" style="margin-top:12px;max-height:200px;overflow:auto">${paths.slice(0, 30).map((p) => esc(shortPath(p, 40, 30))).join('<br>')}</div>`,
    confirmText: '删除', danger: true,
    onConfirm: async () => {
      await runJob('/api/files/delete', { paths, recycle: true }, '删除文件', {
        onDone: (j) => {
          if (j.status !== 'done') return;
          const r = j.result || {};
          toast('删除完成', `已删除 ${r.deleted.length} 个文件，释放 ${r.freed_human}`, 'ok');
          state.large.files = state.large.files.filter((f) => !paths.includes(f.path));
          state.large.sel.clear(); renderLarge(); loadOverview();
        },
      });
    },
  });
}

/* =========================================================================
   DIRECTORY MIGRATION
   ========================================================================= */
function updateSteps(n) {
  [1, 2, 3, 4].forEach((i) => {
    const el = $(`#step${i}`);
    el.classList.toggle('on', i === n);
    el.classList.toggle('done', i < n);
  });
}

function renderDirQuick() {
  const quick = [
    ['下载', '%USERPROFILE%\\Downloads'],
    ['桌面', '%USERPROFILE%\\Desktop'],
    ['文档', '%USERPROFILE%\\Documents'],
    ['图片', '%USERPROFILE%\\Pictures'],
    ['视频', '%USERPROFILE%\\Videos'],
    ['AppData\\Local', '%LOCALAPPDATA%'],
    ['AppData\\Roaming', '%APPDATA%'],
    ['程序安装目录', '%ProgramFiles%'],
  ];
  $('#dirQuick').innerHTML = quick.map(([label, p]) => `<div class="chip" data-quick="${p}">${label}</div>`).join('');
  $('#dirQuick').addEventListener('click', (e) => {
    const c = e.target.closest('[data-quick]'); if (!c) return;
    $('#dirSrc').value = c.dataset.quick; updateSteps(2);
  });
}

function renderDrivePick(target) {
  const drives = state.targets;
  $('#dirDrives').innerHTML = drives.length ? drives.map((d) => {
    const on = (target || state.target) === d.letter;
    const p = d.total ? (d.used / d.total) * 100 : 0;
    return `<div class="drive-card ${on ? 'on' : ''}" data-drive="${d.letter}">
      <div class="row"><div class="letter">${d.letter}</div><div class="spacer"></div>
        <span class="badge ${p > 88 ? 'advanced' : p > 70 ? 'caution' : 'safe'}">已用 ${p.toFixed(0)}%</span></div>
      <div class="free">${esc(d.label || '本地磁盘')} · 可用 ${bytes(d.free)}</div>
      <div class="meter ${p > 88 ? 'danger' : p > 70 ? 'warn' : ''}"><i style="width:${p.toFixed(1)}%"></i></div>
    </div>`;
  }).join('') : '<div class="empty small">没有可用的目标磁盘（需要非系统盘）</div>';
  $$('#dirDrives [data-drive]').forEach((el) => el.addEventListener('click', () => {
    state.target = el.dataset.drive;
    state.settings.target_drive = state.target;
    renderDrivePick(state.target); renderDriveOptions(); updateSteps(3);
  }));
}

/* ------------------------------------------------- in-page folder picker */
const fb = { path: '', parent: null, dirs: [], drives: [], selected: null, onPick: null, busy: false };

async function browseFolder(initial, onPick) {
  fb.onPick = onPick || null;
  fb.selected = null;
  fb.path = '';
  openModal(`
    <div class="modal-head">${icon('folder-open', 'width:18px')}<h3>选择目录</h3><div class="spacer"></div>
      <button class="btn ghost sm" data-fb-close>关闭</button></div>
    <div class="modal-body fb-body">
      <div class="fb-drives" id="fbDrives"></div>
      <div class="fb-crumb">${icon('folder', 'width:15px')}<span class="p" id="fbCrumb">正在读取…</span></div>
      <div class="fb-list" id="fbList"><div class="fb-empty">正在读取…</div></div>
    </div>
    <div class="modal-foot">
      <div class="small dim mono" id="fbSel" style="max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
      <div class="spacer"></div>
      <button class="btn" data-fb-close>取消</button>
      <button class="btn primary" id="fbOk">选择此目录</button>
    </div>`, true);

  $$('[data-fb-close]').forEach((b) => (b.onclick = () => { fb.onPick = null; closeModal(); }));
  $('#fbOk').onclick = () => {
    const picked = fb.selected || fb.path;
    if (!picked) return toast('请先选择一个目录', '', 'warn');
    const cb = fb.onPick;
    fb.onPick = null;
    closeModal();
    if (cb) cb(picked);
  };

  const start = (initial || '').trim() || 'C:\\';
  try {
    await fbLoad(start);
  } catch (err) {
    toast('无法读取目录', err.message, 'error', 7000);
    try { await fbLoad(''); } catch (e2) { /* ignore */ }
  }
}

async function fbLoad(path) {
  const data = await api(`/api/fs/dirs?path=${encodeURIComponent(path)}`);
  fb.path = data.path || '';
  fb.parent = data.parent || null;
  fb.dirs = data.dirs || [];
  fb.drives = data.drives || fb.drives;
  fb.selected = null;
  fbRender(data.error);
}

function fbRender(error) {
  $('#fbDrives').innerHTML = fb.drives.map((d) => {
    const on = fb.path && fb.path.toUpperCase().startsWith(d.letter.toUpperCase());
    return `<div class="chip ${on ? 'on' : ''}" data-fb-drive="${esc(d.letter)}">${esc(d.letter)}</div>`;
  }).join('') || '<div class="small dim">未检测到磁盘</div>';

  $('#fbCrumb').textContent = fb.path || '此电脑';

  const items = [];
  const upTo = fb.parent !== null && fb.parent !== undefined ? fb.parent : (fb.path ? '' : null);
  if (upTo !== null) {
    items.push(`<div class="fb-item" data-fb-up="1">${icon('arrow-right', 'width:15px;transform:rotate(-90deg)')}<div class="nm">${upTo ? '上一级' : '此电脑'}</div></div>`);
  }
  if (!fb.path) {
    items.push(fb.drives.map((d) => `<div class="fb-item" data-fb-go="${esc(d.letter)}\\">
      ${icon('disk', 'width:15px')}<div class="nm">${esc(d.label || d.letter)} <span class="small dim">${esc(d.letter)}\\</span></div></div>`).join(''));
  }
  if (error) items.push(`<div class="fb-empty">${esc(error)}</div>`);
  else if (fb.path && !fb.dirs.length) items.push('<div class="fb-empty">这个目录下没有子文件夹</div>');
  items.push(fb.dirs.map((d) => `<div class="fb-item ${d.hidden ? 'hidden' : ''}" data-fb-dir="${esc(d.name)}" data-fb-path="${esc(d.path)}">
    ${icon('folder', 'width:15px')}<div class="nm">${esc(d.name)}</div>
    ${d.link ? '<span class="tag">联接</span>' : ''}${d.hidden ? '<span class="tag">隐藏</span>' : ''}</div>`).join(''));

  $('#fbList').innerHTML = items.join('');
  const go = (p) => fbLoad(p).catch((e) => toast('无法打开目录', e.message, 'error', 6000));
  $$('#fbDrives [data-fb-drive]').forEach((el) => (el.onclick = () => go(el.dataset.fbDrive + '\\')));
  $$('#fbList [data-fb-up]').forEach((el) => (el.onclick = () => go(fb.parent !== null && fb.parent !== undefined ? fb.parent : '')));
  $$('#fbList [data-fb-go]').forEach((el) => (el.onclick = () => go(el.dataset.fbGo)));
  $$('#fbList [data-fb-dir]').forEach((el) => {
    el.onclick = () => {
      $$('#fbList .fb-item').forEach((x) => x.classList.remove('on'));
      el.classList.add('on');
      fb.selected = el.dataset.fbPath;
      $('#fbSel').textContent = fb.selected;
    };
    el.ondblclick = () => go(el.dataset.fbPath);
  });
  $('#fbSel').textContent = fb.path || '';
}

$('#btnPickDir').onclick = () => {
  const btn = $('#btnPickDir');
  if (btn.disabled) return;
  btn.disabled = true;
  browseFolder($('#dirSrc').value || 'C:\\', (picked) => {
    $('#dirSrc').value = picked;
    updateSteps(2);
    toast('已选择目录', picked, 'ok', 3000);
  }).catch((e) => toast('选择目录失败', e.message, 'error'))
    .finally(() => { btn.disabled = false; });
};

$('#btnDirPlan').onclick = async () => {
  const src = $('#dirSrc').value.trim();
  if (!src) return toast('缺少目录', '请先选择要迁移的目录', 'warn');
  if (!state.target) return toast('缺少目标盘', '请先选择目标磁盘', 'warn');
  $('#dirPlan').innerHTML = '<div class="empty small">正在分析…</div>';
  updateSteps(3);
  await runJob('/api/move/plan', { src, target: state.target }, '分析迁移方案', {
    onDone: (j) => {
      if (j.status !== 'done') { $('#dirPlan').innerHTML = `<div class="note error">${icon('alert', 'width:15px')}<div>分析失败：${esc(j.error || '')}</div></div>`; return; }
      state.dirPlan = j.result;
      renderDirPlan(j.result);
    },
  });
};

function renderDirPlan(p) {
  const ok = p.ok;
  const s = p.summary || {};
  const refs = p.registry_refs || [];
  $('#dirPlan').innerHTML = `
    ${ok ? `<div class="note ok">${icon('check', 'width:15px')}<div>方案可行，可以开始迁移。</div></div>`
    : `<div class="note error">${icon('alert', 'width:15px')}<div>${p.errors.map(esc).join('<br>')}</div></div>`}
    ${(p.warnings || []).length ? `<div class="note warn" style="margin-top:12px">${icon('alert', 'width:15px')}<div>${p.warnings.map(esc).join('<br>')}</div></div>` : ''}
    <div class="grid cols-3" style="margin-top:16px">
      <div class="card stat" style="box-shadow:none"><div class="label">源目录</div><div class="mono small nowrap" title="${esc(p.src)}">${esc(shortPath(p.src, 22, 16))}</div></div>
      <div class="card stat" style="box-shadow:none"><div class="label">数据量</div><div class="value" style="font-size:19px">${esc(s.size_human || '0 B')}</div><div class="hint">${s.count || 0} 个文件</div></div>
      <div class="card stat" style="box-shadow:none"><div class="label">注册表引用</div><div class="value" style="font-size:19px">${p.registry_count || 0}</div><div class="hint">将自动备份并改写</div></div>
    </div>
    <dl class="kv" style="margin-top:16px">
      <dt>迁移目标</dt><dd class="mono">${esc(p.dst)}</dd>
      <dt>跨分区</dt><dd>${p.drives_ok ? '是（可释放 C 盘空间）' : '否（同分区，仅整理位置）'}</dd>
      <dt>目录联接</dt><dd>迁移后原路径 ${esc(p.src)} 会变成指向新位置的联接</dd>
    </dl>
    ${refs.length ? `<div style="margin-top:16px"><div class="small dim" style="margin-bottom:8px">注册表引用（前 12 条）：</div>
      <div class="table-wrap" style="max-height:230px"><table><thead><tr><th>注册表项</th><th>值名称</th><th>当前内容</th></tr></thead>
      <tbody>${refs.slice(0, 12).map((r) => `<tr><td class="mono small nowrap" style="max-width:280px" title="${esc(r.key_path)}">${esc(r.key_path)}</td>
        <td class="small">${esc(r.name)}</td><td class="mono small nowrap" style="max-width:300px" title="${esc(r.value)}">${esc(shortPath(String(r.value), 30, 20))}</td></tr>`).join('')}</tbody></table></div></div>` : ''}`;
  $('#btnDirGo').disabled = !ok;
}

$('#btnDirGo').onclick = async () => {
  const p = state.dirPlan; if (!p || !p.ok) return;
  const link = $('#optLink').checked;
  const update_registry = $('#optRegistry').checked;
  const verify_hash = $('#optHash').checked;
  confirmModal({
    title: '确认开始迁移？',
    body: `<div>将把</div>
      <div class="mono small" style="margin:8px 0">${esc(p.src)}</div>
      <div>迁移到</div>
      <div class="mono small" style="margin:8px 0">${esc(p.dst)}</div>
      <div class="row" style="margin-top:10px">
        <span class="badge ${link ? 'safe' : 'caution'}">${link ? '创建目录联接' : '不创建联接'}</span>
        <span class="badge ${update_registry ? 'safe' : 'caution'}">${update_registry ? '同步注册表' : '不改注册表'}</span>
        <span class="badge plain">${verify_hash ? '指纹校验' : '数量校验'}</span>
      </div>`,
    confirmText: '开始迁移',
    onConfirm: async () => {
      updateSteps(4);
      await runJob('/api/move/execute', { src: p.src, dst: p.dst, link, update_registry, verify_hash }, '迁移目录', {
        onDone: (j) => {
          if (j.status !== 'done') return;
          const r = j.result || {};
          const warns = (r.warnings || []).length;
          toast('迁移完成', `已迁移 ${bytes(r.size_bytes)}${r.link ? '，并创建目录联接' : ''}${warns ? `，有 ${warns} 条提示` : ''}`, warns ? 'warn' : 'ok', 8000);
          if (warns) openModal(`<div class="modal-head">${icon('alert', 'width:18px;color:var(--warn)')}<h3>迁移完成，但有提示</h3></div>
            <div class="modal-body"><div class="note warn">${icon('alert', 'width:15px')}<div>${r.warnings.map(esc).join('<br>')}</div></div></div>
            <div class="modal-foot"><div class="spacer"></div><button class="btn primary" data-close>知道了</button></div>`);
          if ($('[data-close]')) $('[data-close]').onclick = closeModal;
          $('#btnDirGo').disabled = true; state.dirPlan = null;
          $('#dirPlan').innerHTML = '<div class="note ok">' + icon('check', 'width:15px') + '<div>迁移已完成。如需回退，请到“迁移记录”中还原。</div></div>';
          loadOverview(); updateSteps(1);
        },
      });
    },
  });
};

/* =========================================================================
   APPLICATIONS
   ========================================================================= */
async function loadApps(refresh = false) {
  $('#appsBody').innerHTML = `<tr><td colspan="8"><div class="empty">正在读取已安装应用…</div></td></tr>`;
  try {
    const res = await api(`/api/apps${refresh ? '?refresh=true' : ''}`);
    state.apps.list = res.apps || [];
    $('#appsStats').textContent = `共 ${res.total} 个 · 可迁移 ${res.migratable} 个`;
    detectRunning();
    renderApps();
  } catch (err) {
    $('#appsBody').innerHTML = `<tr><td colspan="8"><div class="empty">读取失败：${esc(err.message)}</div></td></tr>`;
  }
}

async function detectRunning() {
  // best effort: mark apps whose folder is on C: and may be running (heuristic by folder name)
  state.apps.running = new Set();
}

function appSortValue(a, key) {
  switch (key) {
    case 'name': return (a.name || '').toLowerCase();
    case 'publisher': return (a.publisher || '').toLowerCase();
    case 'version': return (a.version || '').toLowerCase();
    case 'status': return a.migratable ? 0 : 1;
    default: return '';
  }
}

function sortApps(list) {
  const { key, dir } = state.apps.sort;
  if (!key) return list;
  const mul = dir === 'asc' ? 1 : -1;
  return list.slice().sort((a, b) => {
    const byName = () => (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
    if (key === 'size') {
      const av = a.estimated_size > 0 ? a.estimated_size : null;
      const bv = b.estimated_size > 0 ? b.estimated_size : null;
      if (av === null && bv === null) return byName();
      if (av === null) return 1;   // unknown size always sinks to the bottom
      if (bv === null) return -1;
      return (av - bv) * mul || byName();
    }
    const av = appSortValue(a, key);
    const bv = appSortValue(b, key);
    if (av < bv) return -mul;
    if (av > bv) return mul;
    return byName();
  });
}

function updateAppSortIndicators() {
  $$('[data-appsort]').forEach((th) => {
    const active = state.apps.sort.key === th.dataset.appsort;
    th.classList.toggle('sorted', active);
    const arrow = $('.sarrow', th);
    if (arrow) arrow.textContent = active ? (state.apps.sort.dir === 'asc' ? '▲' : '▼') : '';
  });
}

function renderApps() {
  const q = state.apps.query.toLowerCase();
  let list = state.apps.list;
  if (q) list = list.filter((a) => `${a.name} ${a.publisher} ${a.folder} ${a.version}`.toLowerCase().includes(q));
  if (state.apps.filter === 'movable') list = list.filter((a) => a.migratable);
  if (state.apps.filter === 'locked') list = list.filter((a) => !a.migratable);
  if (state.apps.filter === 'running') list = list.filter((a) => state.apps.running.has(a.id));
  list = sortApps(list);
  updateAppSortIndicators();
  if (!list.length) { $('#appsBody').innerHTML = `<tr><td colspan="8"><div class="empty">没有匹配的应用</div></td></tr>`; return; }
  $('#appsBody').innerHTML = list.slice(0, 400).map((a) => `
    <tr class="${a.migratable ? '' : 'dim'}">
      <td><span class="check ${state.apps.sel.has(a.id) ? 'on' : ''}" data-appsel="${esc(a.id)}" ${a.migratable ? '' : 'style="opacity:.35"'}>${''}</span></td>
      <td>
        <div style="font-weight:500">${esc(a.name)}</div>
        <div class="small muted nowrap" style="max-width:280px">${esc(a.source === 'folder' ? '未注册的应用目录' : (a.registry_key || ''))}</div>
      </td>
      <td class="small dim nowrap" style="max-width:150px">${esc(a.publisher || '–')}</td>
      <td class="small dim">${esc(a.version || '–')}</td>
      <td class="mono small nowrap" style="max-width:300px" title="${esc(a.folder || '')}">${esc(a.folder ? shortPath(a.folder, 30, 22) : '未找到')}</td>
      <td class="num small">${a.estimated_size ? esc(a.estimated_human) : `<button class="btn sm ghost" data-appsize="${esc(a.id)}">计算</button>`}</td>
      <td>${a.migratable ? '<span class="badge safe">可迁移</span>' : `<span class="badge plain" title="${esc(a.reason)}">${esc(a.reason || '不可迁移')}</span>`}</td>
      <td class="actions">
        <button class="btn sm" data-appanalyse="${esc(a.id)}">分析</button>
        <button class="btn sm primary" data-appmove="${esc(a.id)}" ${a.migratable ? '' : 'disabled'}>迁移</button>
      </td>
    </tr>`).join('');
}

$('#appSearch').addEventListener('input', (e) => { state.apps.query = e.target.value; renderApps(); });
$$('[data-appfilter]').forEach((c) => c.addEventListener('click', () => {
  $$('[data-appfilter]').forEach((x) => x.classList.remove('on'));
  c.classList.add('on'); state.apps.filter = c.dataset.appfilter; renderApps();
}));
$('#btnAppsRefresh').onclick = () => loadApps(true);

$$('[data-appsort]').forEach((th) => th.addEventListener('click', () => {
  const key = th.dataset.appsort;
  const s = state.apps.sort;
  if (s.key === key) {
    s.dir = s.dir === 'asc' ? 'desc' : 'asc';
  } else {
    s.key = key;
    // Size is most useful largest-first; text columns read naturally A→Z.
    s.dir = key === 'size' ? 'desc' : 'asc';
  }
  renderApps();
}));

$('#appsBody').addEventListener('click', async (e) => {
  const t = e.target.closest('[data-appsel],[data-appsize],[data-appanalyse],[data-appmove]');
  if (!t) return;
  if (t.dataset.appsel) {
    const a = state.apps.list.find((x) => x.id === t.dataset.appsel);
    if (!a || !a.migratable) return;
    if (state.apps.sel.has(a.id)) state.apps.sel.delete(a.id); else state.apps.sel.add(a.id);
    renderApps(); return;
  }
  if (t.dataset.appsize) {
    const a = state.apps.list.find((x) => x.id === t.dataset.appsize);
    t.textContent = '计算中…'; t.disabled = true;
    try { const r = await api('/api/apps/size', { folder: a.folder }); a.estimated_size = r.size; a.estimated_human = r.size_human; renderApps(); }
    catch (err) { toast('计算失败', err.message, 'error'); t.textContent = '计算'; t.disabled = false; }
    return;
  }
  if (t.dataset.appanalyse) { const a = state.apps.list.find((x) => x.id === t.dataset.appanalyse); return analyseApp(a); }
  if (t.dataset.appmove) { const a = state.apps.list.find((x) => x.id === t.dataset.appmove); return analyseApp(a, true); }
});

async function analyseApp(a, autoGo = false) {
  if (!a) return;
  if (!state.appTarget) return toast('缺少目标盘', '请先选择目标磁盘', 'warn');
  toast('正在分析', `${a.name} · 检查目录与注册表引用…`, 'info', 3000);
  const j = await runJob('/api/apps/analyse', {
    folder: a.folder, target: state.appTarget, name: a.name, registry_key: a.registry_key || '',
  }, `分析应用 ${a.name}`);
  if (!j || j.status !== 'done') return;
  const p = j.result || {};
  const s = p.summary || {};
  a.estimated_size = s.size; a.estimated_human = s.size_human;
  const refs = p.registry_refs || [];
  openModal(`
    <div class="modal-head">${icon('app', 'width:18px;color:var(--accent-2)')}<h3>${esc(a.name)} · 迁移分析</h3>
      <div class="spacer"></div><span class="badge ${p.ok ? 'safe' : 'advanced'}">${p.ok ? '可以迁移' : '存在阻碍'}</span></div>
    <div class="modal-body">
      ${(p.errors || []).length ? `<div class="note error">${icon('alert', 'width:15px')}<div>${p.errors.map(esc).join('<br>')}</div></div>` : ''}
      ${(p.warnings || []).length ? `<div class="note warn" style="margin-top:10px">${icon('alert', 'width:15px')}<div>${p.warnings.map(esc).join('<br>')}</div></div>` : ''}
      <div class="grid cols-3" style="margin-top:14px">
        <div class="card stat" style="box-shadow:none"><div class="label">安装目录大小</div><div class="value" style="font-size:20px">${esc(s.size_human || '0 B')}</div><div class="hint">${s.count || 0} 个文件</div></div>
        <div class="card stat" style="box-shadow:none"><div class="label">注册表引用</div><div class="value" style="font-size:20px">${p.registry_count || 0}</div><div class="hint">迁移时自动改写</div></div>
        <div class="card stat" style="box-shadow:none"><div class="label">相关服务</div><div class="value" style="font-size:20px">${(p.services || []).length}</div><div class="hint">${(p.services || []).join('、') || '无'}</div></div>
      </div>
      <dl class="kv" style="margin-top:16px">
        <dt>当前目录</dt><dd class="mono">${esc(p.src)}</dd>
        <dt>迁移后</dt><dd class="mono">${esc(p.dst)}</dd>
        <dt>可执行文件</dt><dd class="mono small">${(p.exe_names || []).slice(0, 8).join('、') || '未识别'}</dd>
      </dl>
      <div class="row" style="margin-top:16px">
        <label class="switch"><input type="checkbox" id="apLink" checked><span class="track"></span><span class="small dim">创建目录联接（推荐）</span></label>
        <label class="switch"><input type="checkbox" id="apReg" checked><span class="track"></span><span class="small dim">同步注册表</span></label>
        <label class="switch"><input type="checkbox" id="apKill" checked><span class="track"></span><span class="small dim">迁移前结束其进程</span></label>
      </div>
      ${refs.length ? `<div style="margin-top:16px"><div class="small dim" style="margin-bottom:8px">将被改写的注册表项（前 10 条）</div>
        <div class="table-wrap" style="max-height:200px"><table><thead><tr><th>注册表项</th><th>值</th><th>内容</th></tr></thead><tbody>
        ${refs.slice(0, 10).map((r) => `<tr><td class="mono small nowrap" style="max-width:270px" title="${esc(r.key_path)}">${esc(r.key_path)}</td>
          <td class="small">${esc(r.name)}</td><td class="mono small nowrap" style="max-width:250px" title="${esc(r.value)}">${esc(shortPath(String(r.value), 26, 18))}</td></tr>`).join('')}
        </tbody></table></div></div>` : ''}
    </div>
    <div class="modal-foot"><div class="spacer"></div>
      <button class="btn" data-close>取消</button>
      <button class="btn primary" data-go ${p.ok ? '' : 'disabled'}>${icon('play')}开始迁移</button></div>`, true);
  $('[data-close]').onclick = closeModal;
  $('[data-go]').onclick = async () => {
    const link = $('#apLink').checked, update_registry = $('#apReg').checked, kill_first = $('#apKill').checked;
    closeModal();
    await runJob('/api/apps/migrate', {
      folder: a.folder, target: state.appTarget, name: a.name, id: a.id,
      registry_key: a.registry_key || '', uninstall: a.uninstall || '',
      link, update_registry, kill_first,
    }, `迁移应用 ${a.name}`, {
      onDone: (jj) => {
        if (jj.status !== 'done') return;
        const r = jj.result || {};
        toast('应用迁移完成', `${a.name} 已迁移 ${bytes(r.size_bytes)}${r.link ? '，并创建目录联接' : ''}`, (r.warnings || []).length ? 'warn' : 'ok', 8000);
        a.migratable = false; a.reason = '已迁移'; a.folder = null;
        renderApps(); loadOverview();
      },
    });
  };
  if (autoGo && p.ok) $('[data-go]').click();
}

/* =========================================================================
   JOURNAL
   ========================================================================= */
async function loadJournal() {
  try {
    const { records, stats } = await api('/api/journal');
    state.journal = records;
    $('#jnMoved').textContent = stats.migrated || 0;
    $('#jnFreed').textContent = bytes(stats.freed_bytes || 0);
    $('#jnCleans').textContent = stats.clean_runs || 0;
    $('#jnCleanBytes').textContent = bytes(stats.cleaned_bytes || 0);
    $('#navJournalPill').textContent = stats.total || 0;
    if (!records.length) { $('#journalBody').innerHTML = `<tr><td colspan="8"><div class="empty">暂无记录</div></td></tr>`; return; }
    $('#journalBody').innerHTML = records.map((r) => {
      const k = typeLabel(r.action);
      const canRevert = ['move_app', 'move_dir', 'move_file'].includes(r.action) && !r.reverted;
      return `<tr>
        <td class="small muted">${timeStr(r.time)}</td>
        <td><span class="badge ${k.cls}">${k.text}</span></td>
        <td><div class="nowrap" style="max-width:190px;font-weight:500" title="${esc(r.label || '')}">${esc(r.label || '–')}</div>
            ${r.strategy ? `<div class="small muted">${esc(r.strategy)}</div>` : ''}</td>
        <td class="mono small nowrap" style="max-width:210px" title="${esc(r.src || '')}">${esc(shortPath(r.src || '–', 24, 16))}</td>
        <td class="mono small nowrap" style="max-width:210px" title="${esc(r.dst || '')}">${esc(r.dst ? shortPath(r.dst, 24, 16) : '–')}</td>
        <td class="num small">${r.size_bytes ? esc(bytes(r.size_bytes)) : '–'}</td>
        <td>${r.reverted ? '<span class="badge plain">已还原</span>' : r.status === 'failed' ? '<span class="badge advanced">失败</span>' : '<span class="badge ok">完成</span>'}</td>
        <td class="actions">
          ${r.src ? `<button class="btn sm ghost" data-jopen="${esc(r.src)}" title="打开位置">${icon('open', 'width:13px')}</button>` : ''}
          ${canRevert ? `<button class="btn sm" data-jrevert="${esc(r.id)}">${icon('undo', 'width:13px')}还原</button>` : ''}
        </td>
      </tr>`;
    }).join('');
  } catch (e) { toast('读取记录失败', e.message, 'error'); }
}

$('#btnJournalRefresh').onclick = loadJournal;
$('#btnJournalClear').onclick = () => confirmModal({
  title: '清空所有记录？', body: '只删除记录文本，不影响已迁移的文件与注册表备份。',
  confirmText: '清空', danger: true,
  onConfirm: async () => { await api('/api/journal', undefined, { method: 'DELETE' }); loadJournal(); toast('记录已清空', '', 'ok'); },
});

$('#journalBody').addEventListener('click', (e) => {
  const o = e.target.closest('[data-jopen]');
  if (o) { api('/api/reveal', { path: o.dataset.jopen }); return; }
  const r = e.target.closest('[data-jrevert]');
  if (r) {
    const rec = state.journal.find((x) => x.id === r.dataset.jrevert);
    confirmModal({
      title: '还原这次迁移？',
      body: `<div>将把数据从目标位置移回原位置，并恢复注册表备份。</div>
        <div class="mono small" style="margin-top:10px">${esc(rec.dst)}<br>→ ${esc(rec.src)}</div>
        <div class="note warn" style="margin-top:12px">${icon('alert', 'width:15px')}<div>请确认原路径没有被新的数据占用。</div></div>`,
      confirmText: '开始还原', danger: true,
      onConfirm: async () => {
        await runJob(`/api/journal/${rec.id}/revert`, {}, `还原 ${rec.label || ''}`, {
          onDone: (j) => { if (j.status === 'done') { toast('还原完成', '', 'ok'); loadJournal(); loadOverview(); } },
        });
      },
    });
  }
});

/* =========================================================================
   SETTINGS
   ========================================================================= */
function renderDriveOptions() {
  const opts = state.targets.map((d) => `<option value="${d.letter}">${d.letter} · ${esc(d.label || '磁盘')} · 可用 ${bytes(d.free)}</option>`).join('');
  $('#largeTarget').innerHTML = opts;
  $('#largeTarget').value = state.target || '';
  $('#appTarget').innerHTML = opts;
  $('#appTarget').value = state.appTarget || state.target || '';
  $('#setTarget').innerHTML = opts;
  $('#setTarget').value = state.settings.target_drive || state.target || '';
  $('#sideTarget').textContent = state.target ? `目标盘：${state.target}` : '未设置目标盘';
}

$('#largeTarget').addEventListener('change', (e) => { state.target = e.target.value; renderDrivePick(state.target); });
$('#appTarget').addEventListener('change', (e) => { state.appTarget = e.target.value; });

function fillSettings() {
  const s = state.settings;
  $('#setGroup').value = s.group || 'Migrated';
  $('#setAppGroup').value = s.app_group || 'Apps';
  $('#setMinMb').value = s.large_file_min_mb || 200;
  $('#setLink').checked = !!s.create_junction;
  $('#setRegistry').checked = !!s.update_registry;
  $('#setHash').checked = !!s.verify_hash;
  $('#setKill').checked = !!s.kill_before_migrate;
  $('#setTarget').value = s.target_drive || state.target || '';
  renderDriveOptions();
}

$('#btnSaveSettings').onclick = (e) => {
  const btn = e.currentTarget;
  const payload = {
    target_drive: $('#setTarget').value,
    group: $('#setGroup').value.trim() || 'Migrated',
    app_group: $('#setAppGroup').value.trim() || 'Apps',
    large_file_min_mb: Number($('#setMinMb').value) || 200,
    create_junction: $('#setLink').checked,
    update_registry: $('#setRegistry').checked,
    verify_hash: $('#setHash').checked,
    kill_before_migrate: $('#setKill').checked,
  };
  return withBusy(btn, '保存中…', async () => {
    try {
      const r = await api('/api/settings', payload);
      state.settings = r.settings; state.target = r.settings.target_drive;
      $('#optLink').checked = r.settings.create_junction;
      $('#optRegistry').checked = r.settings.update_registry;
      $('#optHash').checked = r.settings.verify_hash;
      renderDriveOptions();
      toast('设置已保存', '新的默认值将在下次迁移时生效', 'ok');
    } catch (err) { toast('保存失败', err.message, 'error', 8000); }
  });
};

$('#btnRegistryBackup').onclick = (e) => {
  const btn = e.currentTarget;
  // Exporting HKCU\\Software + HKLM\\SOFTWARE can take a minute or more, so run it
  // as a background job: the progress bar reports while the button stays busy.
  return withBusy(btn, '备份中…', () =>
    runJob('/api/registry/backup', {}, '备份注册表', {
      onDone: (j) => {
        if (j && j.status === 'done') {
          toast('注册表备份完成', ((j.result || {}).folder) || '', 'ok', 9000);
        }
      },
    }));
};

['#btnElevate', '#btnElevate2'].forEach((sel) => {
  const el = $(sel); if (!el) return;
  el.onclick = async () => {
    if (el.disabled) return;
    const label = el.innerHTML;
    el.disabled = true;
    el.innerHTML = '正在请求权限…';
    try {
      const r = await api('/api/elevate', {});
      toast(r.ok ? '正在以管理员身份重新启动' : '提升失败', r.message,
        r.ok ? 'ok' : 'error', 9000);
      if (r.ok) {
        // The elevated copy takes over and this window closes itself, so keep
        // the button busy; the timer only matters if the hand-over fails.
        el.innerHTML = '正在切换…';
        setTimeout(() => { el.disabled = false; el.innerHTML = label; }, 8000);
        return;
      }
    } catch (e) {
      toast('提升失败', e.message, 'error');
    }
    el.disabled = false;
    el.innerHTML = label;
  };
});

/* =========================================================================
   ABOUT
   The GitHub repository is the only outward-facing part of the app, so it is
   always handed to the system browser - never loaded inside the desktop window.
   ========================================================================= */
const REPO_URL = 'https://github.com/soultt/CClear';

async function openExternal(url) {
  if (!/^https?:\/\//i.test(url || '')) return;
  try {
    const r = await api('/api/open-url', { url });
    toast(r.ok ? '已在浏览器中打开' : '无法自动打开',
      r.ok ? url : (r.message || url), r.ok ? 'ok' : 'warn', 6000);
    return;
  } catch (e) {
    // Older backend without the endpoint - let the browser open a new tab.
  }
  try { window.open(url, '_blank', 'noopener'); } catch (e) { /* ignore */ }
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall back to the legacy path below */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

function fillAbout() {
  const o = state.overview || {};
  const os = o.os || {};
  const paths = o.paths || {};
  const ver = o.version ? `v${o.version}` : '–';
  $('#aboutBadgeVer').textContent = ver;
  $('#aboutVer').textContent = ver;
  $('#aboutMode').textContent = document.body.classList.contains('embedded')
    ? '独立窗口（内嵌 WebView2）' : '系统浏览器（网页界面）';
  $('#aboutAdmin').textContent = o.admin
    ? '已获得（DISM 等需要提升的操作可用）' : '普通权限（部分功能受限）';
  $('#aboutOs').textContent = os.name || '–';
  $('#aboutRuntime').textContent = `Python ${os.python || '–'}${os.machine ? ` · ${os.machine}` : ''}`;
  $('#aboutSysDrive').textContent = o.system_drive || '–';
  $('#aboutDataDir').textContent = paths.data || '–';
  $('#aboutBackupDir').textContent = paths.backups || '–';
  $('#aboutLog').textContent = paths.log || '–';
}

$('#btnGithub').onclick = () => openExternal(REPO_URL);
$('#btnGithubCopy').onclick = async () => {
  const ok = await copyText(REPO_URL);
  toast(ok ? '已复制仓库地址' : '复制失败', ok ? REPO_URL : '请手动选中链接复制', ok ? 'ok' : 'warn');
};
$('#btnAboutData').onclick = () => {
  const p = (state.overview.paths || {}).data;
  if (p) api('/api/reveal', { path: p }).catch((e) => toast('打开失败', e.message, 'error'));
};
$('#btnAboutLog').onclick = () => {
  const paths = state.overview.paths || {};
  const p = paths.log || paths.data;
  if (p) api('/api/reveal', { path: p }).catch((e) => toast('打开失败', e.message, 'error'));
};

/* =========================================================================
   WINDOW CHROME
   The desktop window is frameless, so the page draws its own title bar. In a
   plain browser (--browser, or missing WebView2) the bar stays hidden and the
   browser's own controls are used instead.
   ========================================================================= */
const cclearWindowApi = () => (window.pywebview && window.pywebview.api) || null;
let cclearMaximized = false;

window.cclearSetMaximized = function (max) {
  const btn = $('#winMax');
  cclearMaximized = !!max;
  if (!btn) return;
  btn.innerHTML = `<svg><use href="#i-win-${max ? 'restore' : 'max'}"/></svg>`;
  btn.title = max ? '向下还原' : '最大化';
  btn.setAttribute('aria-label', btn.title);
  const drag = $('#tbDrag');
  // A maximized window must not be draggable, otherwise the title bar would
  // try to move a full-screen window.
  if (drag) drag.classList.toggle('pywebview-drag-region', !max);
};

function cclearMarkEmbedded() {
  document.body.classList.add('embedded');
  cclearWinCall('is_maximized').then((m) => { if (m !== null) window.cclearSetMaximized(!!m); });
}

function cclearWinCall(name, ...args) {
  const api = cclearWindowApi();
  if (!api || typeof api[name] !== 'function') return Promise.resolve(null);
  try { return Promise.resolve(api[name](...args)).catch(() => null); }
  catch (e) { return Promise.resolve(null); }
}

if (cclearWindowApi()) cclearMarkEmbedded();
window.addEventListener('pywebviewready', cclearMarkEmbedded);

$('#winMin').onclick = () => cclearWinCall('minimize');
$('#winClose').onclick = () => cclearWinCall('close');
$('#winMax').onclick = async () => {
  const max = await cclearWinCall('toggle_maximize');
  if (typeof max === 'boolean') window.cclearSetMaximized(max);
};
$('#tbDrag').addEventListener('dblclick', () => $('#winMax').click());

/* -------------------------------------------------------------------------
   Resize grips - the window has no native frame, so edge dragging is handled
   here: grab a grip, then move/resize the real window to follow the pointer.
   ------------------------------------------------------------------------- */
let grip = null;          // active drag: {edge, start, x, y, w, h, minW, minH}
let gripFrame = 0;        // pending requestAnimationFrame id
let gripNext = null;      // latest requested rect

function gripCommit() {
  gripFrame = 0;
  const r = gripNext;
  gripNext = null;
  if (r) cclearWinCall('set_bounds', r[0], r[1], r[2], r[3]);
}

function gripDrag(e) {
  const g = grip;
  if (!g) return;
  const dx = e.screenX - g.sx;
  const dy = e.screenY - g.sy;
  let { x, y, w, h } = g;
  if (g.edge.includes('w')) {
    w = g.w - dx;
    x = g.x + dx;
    if (w < g.minW) { w = g.minW; x = g.x + g.w - g.minW; }
  } else if (g.edge.includes('e')) {
    w = Math.max(g.minW, g.w + dx);
  }
  if (g.edge.includes('n')) {
    h = g.h - dy;
    y = g.y + dy;
    if (h < g.minH) { h = g.minH; y = g.y + g.h - g.minH; }
  } else if (g.edge.includes('s')) {
    h = Math.max(g.minH, g.h + dy);
  }
  gripNext = [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
  if (!gripFrame) gripFrame = requestAnimationFrame(gripCommit);
}

function gripEnd() {
  grip = null;
}

$$('.win-grips .grip').forEach((el) => {
  el.addEventListener('mousedown', async (e) => {
    if (e.button !== 0 || cclearMaximized) return;
    e.preventDefault();
    const bounds = await cclearWinCall('get_bounds');
    if (!Array.isArray(bounds) || !bounds.length) return;
    grip = {
      edge: el.dataset.edge,
      sx: e.screenX, sy: e.screenY,
      x: bounds[0], y: bounds[1], w: bounds[2], h: bounds[3],
      minW: bounds[4] || 320, minH: bounds[5] || 240,
    };
  });
});
window.addEventListener('mousemove', gripDrag);
window.addEventListener('mouseup', gripEnd);
window.addEventListener('blur', gripEnd);

/* =========================================================================
   misc bindings
   ========================================================================= */
$('#btnRefresh').onclick = () => { loadOverview(); toast('已刷新', '', 'ok', 1600); };
$('#btnRefreshDrives').onclick = () => loadOverview();
$('#btnHotspots').onclick = async () => {
  $('#hotspotList').innerHTML = '<div class="empty small">正在分析 C 盘目录占用，可能需要 1-2 分钟…</div>';
  await runJob('/api/dir/hotspots', { root: 'C:\\', depth: 2 }, '分析磁盘占用', {
    onDone: (j) => {
      if (j.status !== 'done') { $('#hotspotList').innerHTML = '<div class="empty small">分析未完成</div>'; return; }
      state.overview.hotspots = (j.result.tree || {}).children || [];
      renderHotspots(j.result.tree);
    },
  });
};
/* the job bar's own buttons are bound per row in ensureJobRow() */

/* Default selection = every "safe" project (skip admin-only ones when the app
   is not elevated). Called once on boot, so a later scan only touches what the
   user can see ticked. */
function selectSafeDefaults() {
  state.junk.sel.clear();
  state.junk.cats
    .filter((c) => c.risk === 'safe' && !(c.admin && !state.overview.admin))
    .forEach((c) => state.junk.sel.add(c.id));
  state.reg.sel.clear();
  state.reg.cats
    .filter((c) => c.risk === 'safe' && !(c.admin && !state.overview.admin))
    .forEach((c) => state.reg.sel.add(c.id));
}

/* =========================================================================
   boot
   ========================================================================= */
(async function boot() {
  const res = await api('/api/junk/categories').catch(() => null);
  if (res) state.junk.cats = res.categories;
  const reg = await api('/api/regclean/categories').catch(() => null);
  if (reg) state.reg.cats = reg.categories;
  renderDirQuick();
  await loadOverview();
  selectSafeDefaults();
  renderJunk();
  renderReg();
  updateSteps(1);
  if (!state.overview.admin) {
    toast('未获得管理员权限', '清理系统缓存、创建符号链接等功能需要管理员权限，可点击右上角按钮提升。', 'warn', 9000);
  }
})();
