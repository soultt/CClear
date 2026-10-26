'use strict';
/* ============================================================
   CClear · Electron 窗口壳
   - UI 是本目录 renderer/ 下的原生页面（loadFile 加载，不是后端托管页）；
     渲染层通过 cclear:api IPC 桥访问 FastAPI 后端，页面自身不发网络请求
   - 后端仍是 FastAPI：本进程负责拉起 / 接管 `main.py --no-window`，
     后端意外退出（如提权交接）时自动等待新副本并换绑端口
   - 窗口使用系统原生边框与标题栏，页面不再自绘窗头
   ============================================================ */

const { app, BrowserWindow, Menu, Tray, Notification, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');
const { spawn } = require('child_process');

// 开发态：项目根目录（main.py / dist\CClear.exe 在这）；
// 打包态：electron-builder 的 extraResources 会把 dist\CClear.exe 放到 resources\dist\
const ROOT = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..');
const PORT_MIN = 8765;
const PORT_SPAN = 40;
const MIN_W = 1040;
const MIN_H = 700;
const APP_TITLE = 'CClear · C 盘空间管家';

let win = null;
let backend = null;      // 本壳拉起的后端子进程
let backendPort = 0;     // 桥当前转发的后端端口
let running = false;     // 后端已就绪、窗口已接管，之后的子进程退出都要处理
let quitting = false;
let tray = null;         // 托盘图标
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------
   后端发现：探测某个端口是否是存活的 CClear
   ------------------------------------------------------------ */
function probe(port, timeout = 600) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const req = http.get({ host: '127.0.0.1', port, path: '/api/overview', timeout }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; if (body.length > 8192) req.destroy(); });
      res.on('end', () => {
        try {
          const j = JSON.parse(body);
          finish(j && typeof j.version === 'string' ? port : null);
        } catch {
          finish(null);
        }
      });
    });
    req.on('error', () => finish(null));
    req.on('timeout', () => { req.destroy(); finish(null); });
  });
}

async function findBackend() {
  const ports = [];
  for (let p = PORT_MIN; p < PORT_MIN + PORT_SPAN; p++) ports.push(p);
  const hits = await Promise.all(ports.map(probe));
  return hits.find(Boolean) || null;
}

/** 轮询直到发现一个存活的 CClear 后端（提权副本可能要几秒才起来）。 */
async function waitBackend(ms) {
  const deadline = Date.now() + ms;
  do {
    const found = await findBackend();
    if (found) return found;
    await sleep(500);
  } while (Date.now() < deadline);
  return null;
}

function portFree(port) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port, timeout: 300 });
    s.on('connect', () => { s.destroy(); resolve(false); });
    s.on('error', () => resolve(true));
    s.on('timeout', () => { s.destroy(); resolve(true); });
  });
}

async function pickPort() {
  for (let p = PORT_MIN; p < PORT_MIN + PORT_SPAN; p++) {
    if (await portFree(p)) return p;
  }
  throw new Error('找不到可用端口');
}

/* ------------------------------------------------------------
   后端子进程
   ------------------------------------------------------------ */
function backendCommand() {
  if (process.env.CCLEAR_BACKEND) return { cmd: process.env.CCLEAR_BACKEND, prefix: [] };
  const mainPy = path.join(ROOT, 'main.py');
  if (fs.existsSync(mainPy)) {
    return { cmd: process.env.CCLEAR_PYTHON || 'python', prefix: ['-u', mainPy] };
  }
  const exe = path.join(ROOT, 'dist', 'CClear-backend.exe');
  if (fs.existsSync(exe)) return { cmd: exe, prefix: [] };
  return null;
}

function startBackend(port) {
  const spec = backendCommand();
  if (!spec) return null;
  const child = spawn(spec.cmd, [...spec.prefix, '--no-window', '--port', String(port)], {
    cwd: ROOT,
    windowsHide: true,            // GUI 进程拉起控制台程序时不开新控制台窗口
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  child.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  child.on('error', (err) => console.error(`[backend] 启动失败: ${err.message}`));
  child.on('exit', () => onBackendExit(child));
  backend = child;
  return child;
}

async function waitForBackend(child, port, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) return null;   // 启动即退出（端口/权限/依赖问题）
    if (await probe(port)) return port;
    await sleep(300);
  }
  return null;                                   // 超时：交给调用方报错
}

function stopBackend() {
  if (backend && backend.exitCode === null) {
    try { backend.kill(); } catch { /* 已退出 */ }
  }
  backend = null;
}

/**
 * 请后端自行退出（附带的 /api/shutdown 需要 X-CClear-Request 头，防止
 * 任意网页通过 localhost 关掉 CClear）。接管来的后端不是本进程的子进程，
 * 只能走这个接口；自己拉起的子进程也先礼后兵，超时再 kill。
 */
function requestShutdown() {
  if (!backendPort) return Promise.resolve();
  return new Promise((resolve) => {
    const req = http.request({
      host: '127.0.0.1',
      port: backendPort,
      path: '/api/shutdown',
      method: 'POST',
      headers: { 'X-CClear-Request': 'local' },
      timeout: 1500,
    }, (res) => {
      res.resume();
      res.on('end', resolve);
    });
    req.on('error', resolve);
    req.on('timeout', () => { req.destroy(); resolve(); });
    req.end();
  });
}

/**
 * 后端子进程意外退出：
 * 1) 大概率是「提权交接」——管理员副本已在别处启动，等它就绪后桥直接换绑；
 * 2) 否则按崩溃处理，原地重启一个后端。
 * 渲染层只经桥访问后端，端口换绑对页面透明，无需刷新。
 */
async function onBackendExit(child) {
  if (quitting || !running || child !== backend) return;
  backend = null;

  console.log('[shell] 后端进程退出，等待接管 / 重启…');
  const takeover = await waitBackend(15000);
  if (takeover) { backendPort = takeover; return; }

  try {
    const port = await pickPort();
    const fresh = startBackend(port);
    const ok = fresh && await waitForBackend(fresh, port);
    if (ok) { backendPort = ok; return; }
  } catch (err) {
    console.error(`[shell] 重启后端失败: ${err.message}`);
  }
  dialog.showErrorBox('CClear', '后台服务已退出且无法重新启动。\n'
    + '详情见 %LOCALAPPDATA%\\CClear\\CClear.log');
  app.quit();
}

/* ------------------------------------------------------------
   IPC：cclear:api —— 渲染层到后端的通用转发桥
   ------------------------------------------------------------ */
function backendRequest(path, { method = 'GET', body } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined || body === null
      ? null : Buffer.from(JSON.stringify(body), 'utf8');
    const req = http.request({
      host: '127.0.0.1',
      port: backendPort,
      path,
      method: payload && method === 'GET' ? 'POST' : method,
      headers: payload
        ? { 'Content-Type': 'application/json', 'Content-Length': payload.length }
        : {},
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        if (res.statusCode >= 400) {
          let detail = `${res.statusCode} ${res.statusMessage || ''}`;
          try { const j = JSON.parse(text); if (j && j.detail) detail = j.detail; } catch { /* 保留状态码 */ }
          reject(new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)));
          return;
        }
        try { resolve(text ? JSON.parse(text) : {}); } catch { resolve({}); }
      });
    });
    req.on('error', reject);
    // 常规接口毫秒级返回；/api/apps、/api/apps/size 这类要遍历目录/注册表的给足余量
    req.setTimeout(180000, () => req.destroy(new Error('后端响应超时，请重试')));
    if (payload) req.write(payload);
    req.end();
  });
}

ipcMain.handle('cclear:api', (_e, { path, method, body } = {}) => {
  if (typeof path !== 'string' || !path.startsWith('/api/')) {
    throw new Error('非法请求路径');
  }
  if (!backendPort) throw new Error('后台服务未就绪，请稍候重试');
  return backendRequest(path, { method, body });
});

ipcMain.handle('cclear:info', () => ({ version: app.getVersion() }));

/* ------------------------------------------------------------
   IPC：自绘标题栏的窗口控制
   ------------------------------------------------------------ */
ipcMain.handle('win:minimize', () => { win && win.minimize(); return true; });
ipcMain.handle('win:close', () => { win && win.close(); return true; });
ipcMain.handle('win:toggle-maximize', () => {
  if (!win) return false;
  if (win.isMaximized()) win.unmaximize(); else win.maximize();
  return win.isMaximized();
});
ipcMain.handle('win:is-maximized', () => (win ? win.isMaximized() : false));
ipcMain.handle('win:get-bounds', () => {
  if (!win) return null;
  const b = win.getBounds();
  return [b.x, b.y, b.width, b.height, MIN_W, MIN_H];
});
ipcMain.on('win:set-bounds', (_e, r) => {
  // 缩放拖拽按 rAF 高频调用，用单向 send 避免 invoke 排队
  if (win && r) {
    win.setBounds({
      x: Math.round(r.x) | 0, y: Math.round(r.y) | 0,
      width: Math.round(r.width) | 0, height: Math.round(r.height) | 0,
    });
  }
});

/* ------------------------------------------------------------
   窗口（系统原生边框与标题栏）
   ------------------------------------------------------------ */
function createWindow() {
  const icon = path.join(ROOT, 'CClear.ico');
  win = new BrowserWindow({
    width: MIN_W,
    height: MIN_H,
    minWidth: MIN_W,
    minHeight: MIN_H,
    title: APP_TITLE,
    frame: false,                 // 页面自绘标题栏（拖拽 / 双击走 app-region）
    thickFrame: false,            // 关掉 WS_THICKFRAME：Win10 会画浅色原生边框，
                                  // 缩放交给页面的 grips（get_bounds / set_bounds）
    backgroundColor: '#070b12',
    show: false,
    icon: fs.existsSync(icon) ? icon : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.on('page-title-updated', (e) => e.preventDefault());

  const pushMax = () => {
    if (win && !win.isDestroyed()) {
      win.webContents.send('win:maximized', win.isMaximized());
    }
  };
  win.on('maximize', pushMax);
  win.on('unmaximize', pushMax);

  // 外部链接（GitHub、文档）一律交给系统浏览器，窗口没有地址栏，跳走就回不来
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith('file://')) {
      e.preventDefault();
      if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    }
  });

  if (process.env.CCLEAR_DEBUG) win.webContents.openDevTools({ mode: 'detach' });
  if (process.env.CCLEAR_ELECTRON_TEST === '1') {
    win.webContents.once('did-finish-load', () => require('./selftest')(win, app));
  }

  win.once('ready-to-show', () => win.show());
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.on('closed', () => { win = null; });

  // 点击关闭 → 隐藏到托盘（常驻后台），真正退出走托盘菜单的"退出"
  win.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      win.hide();
    }
  });
}

/* ------------------------------------------------------------
   托盘
   ------------------------------------------------------------ */
function trayIconPath() {
  // 打包态：extraResources 把 CClear.ico 放到 resources 根目录
  const p = app.isPackaged
    ? path.join(ROOT, 'CClear.ico')
    : path.join(ROOT, 'app', 'web', 'CClear.ico');
  return fs.existsSync(p) ? p : null;
}

function showMainWindow() {
  if (!win || win.isDestroyed()) { createWindow(); return; }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createTray() {
  const icon = trayIconPath();
  if (!icon) { console.log('[tray] 未找到图标，跳过托盘'); return; }
  tray = new Tray(icon);
  tray.setToolTip(APP_TITLE);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开主窗口', click: showMainWindow },
    { type: 'separator' },
    { label: '垃圾清理', click: () => openPage('cleaner') },
    { label: '目录迁移', click: () => openPage('dir') },
    { label: '应用迁移', click: () => openPage('apps') },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
  tray.on('click', showMainWindow);
  console.log('[tray] 托盘已创建');
}

/* 托盘直达：唤起主窗口并切到指定页面 */
function openPage(page) {
  showMainWindow();
  if (win && !win.isDestroyed()) {
    win.webContents.executeJavaScript(
      `document.querySelector('.nav-item[data-page="${page}"]')?.click()`, true
    ).catch(() => {});
  }
}


/* ------------------------------------------------------------
   生命周期
   ------------------------------------------------------------ */
/* 单实例：已有壳在跑（常驻托盘时很常见）就唤起已有窗口，不再起第二个壳 */
const gotSingleLock = app.requestSingleInstanceLock();
if (!gotSingleLock) {
  app.quit();
} else {
  app.on('second-instance', showMainWindow);
}

app.whenReady().then(async () => {
  if (!gotSingleLock) return;
  Menu.setApplicationMenu(null);

  // 已有存活的 CClear（上一次提权交接留下的副本）就直接接管，
  // 不再拉新后端，避免端口和进程越堆越多。
  const existing = await findBackend();
  if (existing) {
    backendPort = existing;
  } else {
    let port;
    try {
      port = await pickPort();
    } catch (err) {
      dialog.showErrorBox('CClear', err.message);
      app.quit();
      return;
    }
    const child = startBackend(port);
    if (!child) {
      dialog.showErrorBox('CClear', '找不到后端：main.py 与 dist\\CClear-backend.exe 均不存在。');
      app.quit();
      return;
    }
    const ok = await waitForBackend(child, port);
    if (!ok) {
      stopBackend();
      dialog.showErrorBox('CClear', '后台服务启动失败（20 秒内无响应）。\n'
        + '详情见 %LOCALAPPDATA%\\CClear\\CClear.log');
      app.quit();
      return;
    }
    backendPort = port;
  }
  running = true;
  console.log(`[shell] 后端 http://127.0.0.1:${backendPort}`);
  createWindow();
  createTray();
});

let cleanupStarted = false;

app.on('before-quit', (event) => {
  quitting = true;
  if (cleanupStarted) return;                 // 清理完成后的第二次 quit
  if (!backend && !backendPort) return;
  event.preventDefault();
  cleanupStarted = true;
  // 先请求优雅退出，给自己拉起的子进程留 300ms；随后无论如何都强制清理，
  // 避免接管来的后端在窗口关闭后继续驻留。
  requestShutdown().finally(() => {
    if (backend) setTimeout(() => { stopBackend(); app.quit(); }, 300);
    else { stopBackend(); app.quit(); }
  });
});
app.on('window-all-closed', () => app.quit());
