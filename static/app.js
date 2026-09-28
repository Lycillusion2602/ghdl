/* ghdl 前端：只做三件事 —— 拉 /api/state 画界面、把操作发给后端、维护本地这几个输入框。
   没有任何框架，改样式去 style.css，改行为看下面的 data-act 分发。 */
'use strict';

const $ = (s) => document.querySelector(s);
const el = {
  url: $('#url'), assetbox: $('#assetbox'), destdir: $('#destdir'), dirpick: $('#dirpick'),
  quick: $('#quickdirs'), tasks: $('#tasks'), taskcount: $('#taskcount'),
  proxyBody: $('#proxy-body'), histBody: $('#hist-body'), histcount: $('#histcount'),
  status: $('#status'), banner: $('#banner'), probeout: $('#probeout'), gohint: $('#gohint'),
  verify: $('#chk-verify'),
  vpath: $('#vpath'), vurl: $('#vurl'), vpick: $('#vpick'), vhint: $('#vhint'),
};

let S = null;              // 最近一次 /api/state
let failCount = 0;
const openLogs = new Set(); // 哪些任务的日志是展开的，重画时保住状态和滚动位置
const prevStates = new Map();  // 任务 id → 上一轮的状态，用来认出"状态刚变了"
const pendingFx = new Map();   // 这一轮要放的一次性动画
let dirCur = null;         // 目录浏览器当前路径
let browse = { box: el.dirpick, wantFiles: false };  // 当前是哪个输入框在叫浏览器（下载目录 / 校验文件）

/* ---------------------------------------------------------------- 小工具 */

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtB(n) {
  n = Number(n);
  if (!isFinite(n) || n < 0) return '-';
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  for (let i = 0; i < u.length; i++) {
    n /= 1024;
    if (n < 1024 || i === u.length - 1) return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
  }
}

function fmtS(n) { return n ? fmtB(n) + '/s' : '-'; }

function fmtDur(s) {
  s = Math.round(Number(s) || 0);
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
  return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
}

function banner(msg, kind) {
  if (!msg) { el.banner.classList.add('hidden'); return; }
  el.banner.textContent = msg;
  el.banner.classList.toggle('ok', kind === 'ok');   // 配色交给 CSS 变量，别写死
  el.banner.classList.remove('hidden');
}

async function api(path, body, loose) {
  const opt = body === undefined
    ? { headers: { 'Accept': 'application/json' } }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ ok: false, error: '返回的不是 JSON，服务可能挂了' }));
  // loose：把 ok:false 也当正常返回交给调用方判断（比如"停止服务"要先知道还有几个任务在跑）
  if (!loose && (!r.ok || j.ok === false)) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

const stateName = {
  queued: '排队中', parsing: '解析链接', probing: '选源', downloading: '下载中',
  verify: '校验中', done: '完成', failed: '失败', canceled: '已取消', need_asset: '要选文件',
};

const modeName = { auto: '自动', file: '按下文件', clone: 'git clone', verify: '本地校验' };

/* ---------------------------------------------------------------- 任务区 */

function taskCard(t) {
  const pct = t.total ? Math.min(100, (t.downloaded || 0) / t.total * 100) : null;
  const busy = ['queued', 'parsing', 'probing', 'downloading', 'verify'].indexOf(t.state) >= 0;
  let barCls = 'bar';
  if (t.state === 'done') barCls += ' done';
  else if (t.state === 'failed' || t.state === 'canceled') barCls += ' bad';
  if (busy && t.state !== 'verify') barCls += ' run';
  if (!t.total) barCls += ' unknown';
  const barStyle = pct === null ? 'width:100%;opacity:.4' : 'width:' + (pct || 0).toFixed(1) + '%';
  const att = (t.attempts || []);
  const used = t.proxy_used ? '源 <b>' + esc(t.proxy_used) + '</b>' : (t.proxy ? '候选 <b>' + esc(t.proxy) + '</b>' : '');
  const v = t.verify;
  const vr = v && (v.sha256 || v.note || v.archive || v.sig) ? v : null;
  const sum = vr ? summarize(v) : '';
  return `
  <div class="task" data-tid="${esc(t.id)}">
    <div class="top">
      <span class="fn">${esc(t.filename || '（还没定文件名）')}</span>
      <span class="badge s-${esc(t.state)}">${esc(stateName[t.state] || t.state)}</span>
      ${att.length > 1 ? '<span class="muted">第 ' + (att.length) + ' 次尝试</span>' : ''}
      <span class="muted" style="margin-left:auto">${esc(modeName[t.mode] || t.mode || '自动')} · 用时 ${fmtDur(t.elapsed)}</span>
    </div>
    <div class="${barCls}"><i style="${barStyle}"></i></div>
    <div class="meta">
      <span>${fmtB(t.downloaded)}${t.total ? ' / ' + fmtB(t.total) + '（' + (pct || 0).toFixed(1) + '%）' : ''}</span>
      <span>速度 <b>${fmtS(t.speed)}</b></span>
      ${t.eta ? '<span>约剩 ' + fmtDur(t.eta) + '</span>' : ''}
      <span>${used}</span>
    </div>
    ${t.error ? '<div class="vres bad">' + esc(t.error) + '</div>' : ''}
    ${sum ? '<div class="vres">' + esc(sum) + (v.sha256 ? ' <button class="tiny" data-act="copysha" data-sha="' + esc(v.sha256) + '">复制完整 SHA256</button>' : '') + '</div>' : ''}
    <div class="tbtns">
      ${t.dest ? '<button class="tiny" data-act="open" data-path="' + esc(t.dest) + '">打开所在位置</button>' : ''}
      ${t.state === 'downloading' || t.state === 'probing' || t.state === 'queued' || t.state === 'parsing' || t.state === 'verify'
          ? '<button class="tiny" data-act="cancel" data-tid="' + esc(t.id) + '">取消</button>' : ''}
      ${t.state === 'need_asset' ? '<button class="tiny" data-act="assets" data-url="' + esc(t.url) + '">列出该页文件</button>' : ''}
      ${t.state === 'done' || t.state === 'failed' || t.state === 'canceled'
          ? '<button class="tiny" data-act="forget" data-tid="' + esc(t.id) + '">从列表移除</button>' : ''}
    </div>
    <div class="muted" style="margin-top:6px;word-break:break-all">${esc(t.url)}</div>
    ${(t.log && t.log.length) ? `
    <details class="log" data-tid="${esc(t.id)}"${openLogs.has(t.id) ? ' open' : ''}>
      <summary>过程日志（${t.log.length} 行）</summary>
      <pre>${esc(t.log.join('\n'))}</pre>
    </details>` : ''}
  </div>`;
}

function summarize(v) {
  // 键名要和后端 summarize_verify() 对齐：压缩包和签名各自有 detail，不能混用一个
  const b = [];
  if (v.sha256) b.push('SHA256 ' + v.sha256.slice(0, 16) + '…');
  if (v.archive === 'ok') b.push(v.archive_detail || '压缩包结构完整');
  else if (v.archive === 'fail') b.push('压缩包校验失败：' + v.archive_detail);
  if (v.sig === 'ok') b.push('签名有效（' + (v.signer || '?') + '）');
  else if (v.sig === 'bad') b.push('签名状态 ' + v.status);
  if (v.checksum && v.checksum.found) b.push(v.checksum.match ? '与官方校验值一致' : '与官方校验值对不上！');
  if (v.note) b.push(v.note);
  return b.filter(Boolean).join(' · ');
}

function detectFx(list) {
  // 状态发生跃迁才排一个一次性动画；新出现的任务排 fx-new
  const ids = new Set(list.map((t) => t.id));
  Array.from(prevStates.keys()).forEach((id) => { if (!ids.has(id)) prevStates.delete(id); });
  list.forEach((t) => {
    const was = prevStates.get(t.id);
    if (was === undefined) pendingFx.set(t.id, 'fx-new');
    else if (was !== t.state && t.state === 'done') pendingFx.set(t.id, 'fx-done');
    else if (was !== t.state && (t.state === 'failed' || t.state === 'canceled')) pendingFx.set(t.id, 'fx-fail');
    prevStates.set(t.id, t.state);
  });
}

function applyFx() {
  // 界面每 350ms 整块重画，节点是新建的：如果把动画类写进模板，每次重画都会重播一遍。
  // 所以渲染完之后，只给"这一轮刚发生状态变化"的那张卡临时挂类，动画结束就摘掉。
  if (!pendingFx.size) return;
  Array.from(pendingFx.entries()).forEach(([tid, cls]) => {
    const node = el.tasks.querySelector('.task[data-tid="' + tid + '"]');
    if (!node) { pendingFx.delete(tid); return; }
    node.classList.add(cls);
    const clear = () => { node.classList.remove(cls); pendingFx.delete(tid); };
    node.addEventListener('animationend', clear, { once: true });
    setTimeout(clear, 2500);     // 系统开了"减少动效"时 animationend 可能不来，兜个底
  });
}

function renderTasks(list) {
  detectFx(list);
  const keep = {};
  el.tasks.querySelectorAll('details.log[open]').forEach((d) => {
    keep[d.dataset.tid] = d.querySelector('pre') ? d.querySelector('pre').scrollTop : 0;
  });
  const busy = list.filter((t) => ['queued', 'parsing', 'probing', 'downloading', 'verify'].indexOf(t.state) >= 0).length;
  el.taskcount.textContent = list.length ? (busy ? '进行中的 ' + busy + ' 个' : '') : '';
  el.tasks.innerHTML = list.length
    ? list.map(taskCard).join('')
    : '<div class="muted empty">还没有任务。粘贴一个链接，按开始下载。</div>';
  Object.keys(keep).forEach((tid) => {
    const d = el.tasks.querySelector('details[data-tid="' + tid + '"]');
    if (d) {
      d.open = true;
      openLogs.add(tid);
      const p = d.querySelector('pre');
      if (p) p.scrollTop = keep[tid];
    }
  });
  applyFx();
}

/* ---------------------------------------------------------------- 代理区 */

function renderProxies(lst) {
  el.proxyBody.innerHTML = lst.map((p, i) => {
    const rate = p.tried ? (p.ok || 0) + '/' + p.tried : '没试过';
    let ms = '<span class="muted">未测</span>';
    if (p.last_ms != null) {
      ms = (p.last_ok ? '<span class="ok-txt">' : '<span class="bad-txt">') + p.last_ms + ' ms' +
           (p.last_at ? ' <span class="muted">' + esc(p.last_at) + '</span>' : '') + '</span>';
    }
    return `<tr>
      <td class="mono">${i + 1}
        <button class="tiny" data-act="px-move" data-id="${esc(p.id)}" data-delta="-1" title="上移">↑</button>
        <button class="tiny" data-act="px-move" data-id="${esc(p.id)}" data-delta="1" title="下移">↓</button>
      </td>
      <td><input type="checkbox" data-act="px-en" data-id="${esc(p.id)}" ${p.enabled ? 'checked' : ''}></td>
      <td>${esc(p.name)}</td>
      <td class="mono">${p.prefix ? esc(p.prefix) : '<span class="muted">（空 = 直连）</span>'}</td>
      <td>${ms}</td>
      <td class="mono">${rate}</td>
      <td><button class="tiny" data-act="px-del" data-id="${esc(p.id)}">删</button></td>
    </tr>`;
  }).join('');
}

function renderQuick(cfg) {
  const dirs = (cfg.quick_dirs || []).concat([cfg.default_dir]);
  const seen = new Set();
  el.quick.innerHTML = dirs.filter((d) => d && !seen.has(d) && seen.add(d)).map((d) =>
    '<span class="chip" data-act="chip" data-path="' + esc(d) + '" title="点一下填进目录框">' + esc(d) + '</span>').join('');
}

/* ---------------------------------------------------------------- 历史区 */

function renderHist(items) {
  el.histcount.textContent = items.length ? '共 ' + items.length + ' 条（只留最近 300 条）' : '';
  el.histBody.innerHTML = items.length ? items.map((h) => `<tr>
      <td class="mono">${esc((h.at || '').slice(5))}</td>
      <td title="${esc(h.url || '')}">${esc(h.name)}${h.ok ? '' : ' <span class="bad-txt">失败</span>'}</td>
      <td class="mono">${fmtB(h.bytes)}</td>
      <td class="mono">${fmtS(h.speed)}</td>
      <td class="mono">${esc(h.proxy || '-')}${h.attempts > 1 ? '<span class="muted"> ×' + h.attempts + '</span>' : ''}</td>
      <td class="mono" title="${esc(h.sha256 || '')}">${esc((h.verify || '').slice(0, 70))}</td>
      <td>${h.path ? '<button class="tiny" data-act="open" data-path="' + esc(h.path) + '">打开</button>' : ''}</td>
    </tr>`).join('')
    : '<tr><td colspan="7" class="muted">还没下过东西。</td></tr>';
}

/* ---------------------------------------------------------------- 目录 / 文件浏览器 */

async function browseDir(path, box, wantFiles) {
  browse = { box: box, wantFiles: !!wantFiles };
  try {
    let q = '?path=' + encodeURIComponent(path || '');
    if (wantFiles) q += '&files=1';
    const r = await api('/api/ls' + q);
    dirCur = r.path || '';
    const dirs = (r.items || []).map((d) =>
      '<div data-act="dirgo" data-path="' + esc(d.path) + '">' + esc(d.name) + '</div>').join('');
    const secs = [];
    if (wantFiles && dirs) secs.push('<div class="sec">文件夹（点进去）</div>');
    if (dirs) secs.push(dirs);
    if (wantFiles) {
      const fs = (r.files || []).map((f) =>
        '<div class="file" data-act="filepick" data-path="' + esc(f.path) + '">' +
        '<span>' + esc(f.name) + '</span><b>' + fmtB(f.size) + '</b></div>').join('');
      secs.push('<div class="sec">文件（点一下选它）</div>');
      secs.push(fs || '<div class="muted" style="padding:4px 8px">这个目录下没有文件</div>');
    }
    const useBtn = wantFiles
      ? ''                                   // 选文件时"用这个目录"没意义，不给
      : '<button class="tiny" data-act="diruse">用这个目录</button>';
    box.innerHTML = `
      <div class="cur">当前位置：${esc(dirCur || '（我的电脑）')}
        ${r.parent ? '<button class="tiny" data-act="dirgo" data-path="' + esc(r.parent) + '">上一级</button>' : ''}
        ${useBtn}
        <span class="muted">${wantFiles ? '只能选文件；目录用来走到目标那层' : '只能进目录，看不到文件 —— 这里本来就是给下载选落点的'}</span>
      </div>
      ${r.error ? '<div class="bad-txt">' + esc(r.error) + '</div>' : ''}
      <div class="dirlist">${secs.join('')}</div>`;
    box.classList.remove('hidden');
  } catch (e) { banner('列目录失败：' + e.message); }
}

/* ---------------------------------------------------------------- release 文件列表 */

async function showAssets(url) {
  if (!url) { banner('先把发布页链接贴进来'); return; }
  el.assetbox.classList.remove('hidden');
  el.assetbox.innerHTML = '<div class="hd">正在问 api.github.com 要文件清单…</div>';
  try {
    const r = await api('/api/assets?url=' + encodeURIComponent(url));
    if (!r.ok || !r.assets) throw new Error(r.error || '没拿到');
    if (!r.assets.length) { el.assetbox.innerHTML = '<div class="hd">这个 release 没有附件。</div>'; return; }
    el.assetbox.innerHTML = '<div class="hd">' + esc(r.repo + '  ' + (r.tag || '') + '，走 ' + (r.via || '')) +
      ' 共 ' + r.assets.length + ' 个文件，点一下填进上面的链接框：</div>' +
      r.assets.map((a) => '<div class="a" data-act="asset" data-url="' + esc(a.url) + '">' +
        '<span>' + esc(a.name) + '</span><span class="sz">' + fmtB(a.size) + '</span></div>').join('');
  } catch (e) {
    el.assetbox.innerHTML = '<div class="hd">' + esc(e.message) +
      '（api 域名这会儿不通是常事；去发布页右键复制具体文件的下载链接更稳）</div>';
  }
}

/* ---------------------------------------------------------------- 事件 */

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-act]');
  if (!b) return;
  const act = b.dataset.act;
  try {
    if (act === 'chip') { el.destdir.value = b.dataset.path; }
    else if (act === 'open') { await api('/api/open?path=' + encodeURIComponent(b.dataset.path)); banner('已在资源管理器里打开', 'ok'); setTimeout(() => banner(''), 2500); }
    else if (act === 'cancel') { await api('/api/cancel', { id: b.dataset.tid }); }
    else if (act === 'forget') { openLogs.delete(b.dataset.tid); await api('/api/forget', { id: b.dataset.tid }); }
    else if (act === 'assets') { showAssets(b.dataset.url || el.url.value.trim()); }
    else if (act === 'asset') { el.url.value = b.dataset.url; el.assetbox.classList.add('hidden'); }
    else if (act === 'dirgo') { browseDir(b.dataset.path, browse.box, browse.wantFiles); }
    else if (act === 'filepick') {
      el.vpath.value = b.dataset.path;
      browse.box.classList.add('hidden');
      el.vhint.textContent = '已选 ' + b.dataset.path.split(/[\\/]/).pop();
    }
    else if (act === 'diruse') {
      if (dirCur) { (browse.wantFiles ? el.vpath : el.destdir).value = dirCur; }
      browse.box.classList.add('hidden');
    }
    else if (act === 'px-del') { await api('/api/proxies', { action: 'del', id: b.dataset.id }); }
    else if (act === 'px-move') { await api('/api/proxies', { action: 'move', id: b.dataset.id, delta: Number(b.dataset.delta) }); }
    else if (act === 'copysha') {
      await navigator.clipboard.writeText(b.dataset.sha);
      b.textContent = '已复制 ✓'; setTimeout(() => { b.textContent = '复制完整 SHA256'; }, 1600);
    }
    await poll(true);
  } catch (e) { banner('操作失败：' + e.message); }
});

document.addEventListener('change', async (ev) => {
  const b = ev.target.closest('[data-act="px-en"]');
  if (b) { await api('/api/proxies', { action: 'enable', id: b.dataset.id, enabled: b.checked }).catch((e) => banner(e.message)); poll(true); return; }
  if (ev.target === el.verify) {
    api('/api/config', { verify: el.verify.checked }).catch((e) => banner(e.message));
  }
});

document.addEventListener('toggle', (ev) => {
  const d = ev.target.closest && ev.target.closest('details.log');
  if (!d) return;
  if (d.open) openLogs.add(d.dataset.tid); else openLogs.delete(d.dataset.tid);
}, true);

$('#btn-go').addEventListener('click', start);
el.url.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') start(); });
document.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey && ev.key === 'Enter') start();
});
$('#btn-browse').addEventListener('click', () => {
  if (!el.dirpick.classList.contains('hidden')) { el.dirpick.classList.add('hidden'); return; }
  browseDir(el.destdir.value.trim() || '', el.dirpick, false);
});
$('#btn-vbrowse').addEventListener('click', () => {
  if (!el.vpick.classList.contains('hidden')) { el.vpick.classList.add('hidden'); return; }
  const p = el.vpath.value.trim();
  // 有路径就从它所在的那层目录开始，没有就从盘符开始
  const here = p ? p.replace(/[\\/]+[^\\/]*$/, '') : '';
  browseDir(here || p, el.vpick, true);
});
$('#btn-verify').addEventListener('click', startVerify);
el.vpath.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') startVerify(); });

async function startVerify() {
  const p = el.vpath.value.trim();
  if (!p) { banner('先填或先选一个文件'); return; }
  try {
    const r = await api('/api/verify', { path: p, url: el.vurl.value.trim() });
    banner('');
    el.vhint.textContent = '校验任务 ' + r.task.id + ' 已启动，看上面的任务列表';
    openLogs.add(r.task.id);
    await poll(true);
  } catch (e) { banner('校验启动失败：' + e.message); }
}

/* ---------------------------------------------------------------- 主题
   界面右上角不做切换器了（他说不要），主题固定用可爱，定义在 index.html 的内联脚本里：
   document.documentElement.dataset.theme = 'cute'。想换一套改那一行即可，四套变量袋都在 style.css。 */

// 打开页面必须停在最顶上：浏览器有时会恢复上次的滚动位置，看起来就像"一打开在中间"
window.scrollTo(0, 0);
window.addEventListener('load', () => window.scrollTo(0, 0));
$('#btn-assets').addEventListener('click', () => showAssets(el.url.value.trim()));
$('#btn-probe').addEventListener('click', async () => {
  $('#btn-probe').disabled = true;
  el.probeout.textContent = '测速中…';
  try {
    const r = await api('/api/probe');
    const bad = (r.results || []).filter((x) => !x.ok).length;
    el.probeout.textContent = (r.results || []).length + ' 个源：' + (r.results || [])
      .map((x) => x.name + ' ' + (x.ok ? x.ms + 'ms' : '不通')).join('，');
    banner(bad ? bad + ' 个源不通，已自动排到后面' : '全部可用', bad ? '' : 'ok');
    if (bad) setTimeout(() => banner(''), 5000);
  } catch (e) { el.probeout.textContent = ''; banner('测速失败：' + e.message); }
  $('#btn-probe').disabled = false;
  poll(true);
});
$('#btn-px-add').addEventListener('click', addProxy);
$('#px-prefix').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') addProxy(); });

/* 停止服务：打包成 exe 后没有控制台窗口可关，这个按钮就是唯一出口 */
let stopArmed = false;
$('#btn-stop').addEventListener('click', async () => {
  const b = $('#btn-stop');
  const r = await api('/api/shutdown', { force: stopArmed }, true);
  if (r.ok) {
    b.textContent = '已停止';
    b.disabled = true;
    banner('服务已停止，可以关掉这个标签页了', 'ok');
    return;
  }
  if (r.busy) {                      // 有任务在跑，后端拦着不让停，第二次点击才带 force
    stopArmed = true;
    b.textContent = '强制停止';
    banner(r.error);
    setTimeout(() => { stopArmed = false; b.textContent = '停止服务'; }, 8000);
    return;
  }
  banner('停止失败：' + (r.error || '未知原因'));
});

async function addProxy() {
  const prefix = $('#px-prefix').value.trim();
  if (!prefix) { banner('先填代理前缀，比如 https://my-proxy.example/'); return; }
  try {
    await api('/api/proxies', { action: 'add', prefix: prefix, name: $('#px-name').value.trim() });
    $('#px-prefix').value = ''; $('#px-name').value = '';
    banner('已加进列表，建议顺手点一次「全部测速」看看它通不通', 'ok');
    setTimeout(() => banner(''), 4000);
    await poll(true);
  } catch (e) { banner('添加失败：' + e.message); }
}

async function start() {
  const url = el.url.value.trim();
  if (!url) { banner('链接还没填'); return; }
  const mode = (document.querySelector('input[name=mode]:checked') || {}).value || 'auto';
  try {
    const r = await api('/api/download', { url: url, destdir: el.destdir.value.trim(), mode: mode });
    banner('');
    el.gohint.textContent = '任务 ' + r.task.id + ' 已启动';
    openLogs.add(r.task.id);
    await poll(true);
  } catch (e) { banner('启动失败：' + e.message); }
}

/* ---------------------------------------------------------------- 轮询 */

async function poll(now) {
  try {
    const r = await api('/api/state');
    S = r;
    failCount = 0;
    el.status.textContent = '服务在线 · 已运行 ' + fmtDur(r.up);
    el.status.className = 'pill on';
    const ver = $('#ver');
    if (ver) ver.textContent = r.version ? 'v' + r.version : '';   // 版本号只由后端提供，前端不写死
    if (!el.destdir.value.trim() && r.config) el.destdir.value = r.config.default_dir || '';
    el.verify.checked = r.config.verify !== false;
    renderQuick(r.config || {});
    renderProxies(r.proxies || []);
    renderHist(r.history || []);
    renderTasks(r.tasks || []);
  } catch (e) {
    if (++failCount === 2) banner('和后台失联了：' + e.message + '（服务窗口被关了？重新双击 start.bat 即可）');
    el.status.textContent = '连不上后台';
    el.status.className = 'pill off';
  }
  setTimeout(poll, S && (S.tasks || []).some((t) => ['queued', 'parsing', 'probing', 'downloading', 'verify'].indexOf(t.state) >= 0) ? 350 : 1200);
}

poll(true);
