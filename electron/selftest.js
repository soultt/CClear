'use strict';
/* 冒烟自检：仅在 CCLEAR_ELECTRON_TEST=1 时由 main.js 调用。
   在真实窗口里校验 API 桥与页面渲染，输出 PASS/FAIL 后退出。 */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

module.exports = async function selftest(win, app) {
  const lines = [];
  const check = (name, ok, extra = '') => {
    lines.push(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `  (${extra})` : ''}`);
  };
  let failed = false;

  try {
    // 等渲染层完成 boot（overview + categories 都经桥拉取）
    await sleep(2500);
    const r = await win.webContents.executeJavaScript(`(async () => {
      const q = (s) => document.querySelector(s);
      const res = {};
      res.api = !!(window.cclear && typeof window.cclear.api === 'function');
      res.winApi = !!(window.cclear && window.cclear.win && typeof window.cclear.win.toggle_maximize === 'function');
      res.titlebarShown = !!q('#titlebar') && getComputedStyle(q('#titlebar')).display !== 'none';
      res.noOldBar = !document.querySelector('.topbar');
      res.noBrand = !document.querySelector('.sidebar .brand');
      res.dragRegion = getComputedStyle(q('#tbDrag')).webkitAppRegion === 'drag';
      res.gripsShown = !!q('#winGrips');
      res.sidebarShown = !!q('.sidebar');
      res.navCount = document.querySelectorAll('.nav-item').length;
      res.heroTitle = (q('#heroTitle') || {}).textContent || '';
      res.driveRows = document.querySelectorAll('#driveList .drive-row').length;
      res.junkCards = document.querySelectorAll('#junkGrid .junk-card').length;
      res.regCards = document.querySelectorAll('#regGrid .reg-card').length;
      res.adminShown = (q('#sideAdmin') || {}).textContent || '';
      const overview = await window.cclear.api('/api/overview');
      res.overviewVersion = overview.version || '';
      res.junkCats = (await window.cclear.api('/api/junk/categories')).categories.length;
      res.bounds = await window.cclear.win.get_bounds();
      const m1 = await window.cclear.win.toggle_maximize();
      await new Promise((r) => setTimeout(r, 500));
      res.maxIcon = q('#winMax').innerHTML.includes('i-win-restore');
      const m2 = await window.cclear.win.toggle_maximize();
      await new Promise((r) => setTimeout(r, 400));
      res.iconBack = q('#winMax').innerHTML.includes('i-win-max');
      res.max1 = m1; res.max2 = m2;
      return res;
    })()`);

    check('window.cclear 桥可用', r.api);
    check('window.cclear.win 窗口控制可用', r.winApi);
    check('自建标题栏显示', r.titlebarShown);
    check('无旧头部横栏 .topbar', r.noOldBar);
    check('侧边栏无品牌区块 .brand', r.noBrand);
    check('标题栏拖拽区 app-region: drag', r.dragRegion);
    check('缩放 grips 存在', r.gripsShown);
    check('侧边栏渲染', r.sidebarShown);
    check('导航项 9 个', r.navCount === 9, String(r.navCount));
    check('总览已加载（hero 标题）', /盘已用|磁盘信息不可用/.test(r.heroTitle), r.heroTitle);
    check('磁盘列表已渲染', r.driveRows > 0, String(r.driveRows));
    check('垃圾清理卡片已渲染', r.junkCards > 0, String(r.junkCards));
    check('注册表卡片已渲染', r.regCards > 0, String(r.regCards));
    check('权限状态已显示', /权限/.test(r.adminShown), r.adminShown);
    check('桥调 /api/overview 返回版本', !!r.overviewVersion, r.overviewVersion);
    check('桥调 /api/junk/categories', r.junkCats > 0, String(r.junkCats));
    check('get_bounds 返回 [x,y,w,h,minW,minH]',
      Array.isArray(r.bounds) && r.bounds.length === 6 && r.bounds[2] > 0 && r.bounds[4] === 1040,
      JSON.stringify(r.bounds));
    check('toggle_maximize → 最大化', r.max1 === true, String(r.max1));
    check('最大化后标题栏图标切换', r.maxIcon);
    check('toggle_maximize → 还原', r.max2 === false, String(r.max2));
    check('还原后图标恢复', r.iconBack);
  } catch (err) {
    check('执行异常', false, String(err));
  }

  failed = lines.some((l) => l.startsWith('FAIL'));
  console.log(lines.join('\n'));
  console.log(failed ? 'SELFTEST FAILED' : 'SELFTEST OK');
  await sleep(200);
  // 走真实退出路径（before-quit → 停后端），顺带验证退出时后端会被回收
  process.exitCode = failed ? 1 : 0;
  app.quit();
};
