'use strict';
/* 暴露给渲染层的桥：
   - window.cclear.api(path, {method, body}) → 主进程转发 FastAPI 后端
   - window.cclear.win.* → 无边框窗口的最小化 / 最大化 / 关闭 / 缩放控制 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cclear', {
  api: (path, opts = {}) => ipcRenderer.invoke('cclear:api', {
    path,
    method: opts.method || 'GET',
    body: opts.body,
  }),
  info: () => ipcRenderer.invoke('cclear:info'),
  win: {
    minimize: () => ipcRenderer.invoke('win:minimize'),
    close: () => ipcRenderer.invoke('win:close'),
    toggle_maximize: () => ipcRenderer.invoke('win:toggle-maximize'),
    is_maximized: () => ipcRenderer.invoke('win:is-maximized'),
    get_bounds: () => ipcRenderer.invoke('win:get-bounds'),
    set_bounds: (x, y, width, height) => ipcRenderer.send('win:set-bounds', { x, y, width, height }),
    on_maximized: (cb) => ipcRenderer.on('win:maximized', (_e, v) => cb(v)),
  },
});
