/* 运营后台「数据看板」真机截图 + 明暗主题验收
   用法：TOK=<admin token> node scripts/_snap_dashboard.js
   产物：_reports/shots/admin_dash_{dark,light}.png，日志 _reports/_snap_dash.log */
const H = require('./_e2e_lib.js');
const log = H.makeLogger('_reports/_snap_dash.log');
const SHOT = 'D:/Users/Dwm/Desktop/agaent/bank-agent/_reports/shots/';

(async () => {
  const TOK = process.env.TOK;
  if (!TOK) { log('FATAL', 'no TOK'); process.exit(1); }
  const h = await H.launch({ width: 1500, height: 1200, watchdogMs: 120000 });
  try {
    await h.setViewport(1500, 1200);
    // 1) 先落到同源页面，才能写 localStorage
    await h.goto(H.BASE + '/login');
    await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                  localStorage.setItem('xb_user','admin');
                  localStorage.removeItem('xb_admin_theme')`);
    // 2) 进后台
    await h.goto(H.BASE + '/admin');
    const okTxt = await h.waitForText('数据看板', 20000).catch(() => false);
    log('entered_admin', String(okTxt));

    // 3) 等图表渲染
    await H.sleep(2500);
    const txt = await h.bodyText();
    const must = ['数据变化趋势', '交易发生额与笔数', '对话消息数', '交易分类金额占比',
                  '结构分布', '新增用户', '风控告警', '区间合计'];
    must.forEach(m => log('has:' + m, txt.includes(m)));
    log('svg_count', await h.count('svg'));
    log('html_class_dark', await h.eval('document.documentElement.className'));

    await h.screenshot(SHOT + 'admin_dash_dark.png');
    log('shot', 'admin_dash_dark.png');

    // 4) 切亮色主题
    const clickRes = await h.clickByText('暗色', { tag: 'button' });
    log('click_theme_btn', clickRes);
    await H.sleep(1500);
    log('html_class_light', await h.eval('document.documentElement.className'));
    log('theme_ls', await h.eval(`localStorage.getItem('xb_admin_theme')`));
    log('body_bg', await h.eval(`getComputedStyle(document.body).backgroundColor`));
    log('body_color', await h.eval(`getComputedStyle(document.body).color`));
    // 卡片背景对比
    log('card_bg', await h.eval(`(()=>{const c=document.querySelector('#root .rounded-xl');return c?getComputedStyle(c).backgroundColor:'none'})()`));
    log('kpi_text_color', await h.eval(`(()=>{const e=document.querySelector('#root .text-gold-300');return e?getComputedStyle(e).color:'none'})()`));

    await h.screenshot(SHOT + 'admin_dash_light.png');
    log('shot', 'admin_dash_light.png');

    // 5) 刷新后主题保持（localStorage）
    await h.goto(H.BASE + '/admin', { reload: true });
    await h.waitForText('数据看板', 20000).catch(() => false);
    await H.sleep(2000);
    log('after_reload_class', await h.eval('document.documentElement.className'));
    await h.screenshot(SHOT + 'admin_dash_light_reload.png');
    log('shot', 'admin_dash_light_reload.png');

    // 6) 切回暗色，确认往返
    await h.clickByText('亮色', { tag: 'button' });
    await H.sleep(1200);
    log('back_to_dark_class', await h.eval('document.documentElement.className'));
    await h.screenshot(SHOT + 'admin_dash_dark_back.png');
    log('shot', 'admin_dash_dark_back.png');

    // 7) 区间切换 30 天
    await h.clickByText('30 天', { tag: 'button' });
    await H.sleep(2200);
    const txt30 = await h.bodyText();
    log('range30_ok', txt30.includes('近 30 天交易发生额与笔数'));
    await h.screenshot(SHOT + 'admin_dash_30d.png');
    log('shot', 'admin_dash_30d.png');

    log('RESULT', 'DONE');
  } catch (e) {
    log('ERROR', String(e && e.stack || e));
  } finally {
    clearTimeout(h._wd);
    try { await h.close(); } catch (_) {}
    process.exit(0);
  }
})();
