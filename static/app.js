/* ghdl 前端：只做四件事 —— 拉 /api/state 画界面、把操作发给后端、维护本地输入框、目录/文件选择。
   没有框架，改样式去 style.css，改行为看下面的 data-act 分发。 */
'use strict';

const $ = (s) => document.querySelector(s);
const el = {
  url: $('#url'), assetbox: $('#assetbox'), destdir: $('#destdir'), dirpick: $('#dirpick'),
  quick: $('#quickdirs'), tasks: $('#tasks'), taskcount: $('#taskcount'),
  proxyBody: $('#proxy-body'), histBody: $('#hist-body'), histcount: $('#histcount'),
  status: $('#status'), banner: $('#banner'), probeout: $('#probeout'), gohint: $('#gohint'),
  verify: $('#chk-verify'), wantname: $('#wantname'), referer: $('#referer'),
  cookie: $('#cookie'), authorization: $('#authorization'), forcehtml: $('#forcehtml'),
  cfiles: $('#cfiles'), ctarget: $('#ctarget'), ctargetHint: $('#ctarget-hint'),
  cformatHint: $('#cformat-hint'), copts: $('#copts'), cq: $('#cq'), cqv: $('#cqv'),
  cicosize: $('#cicosize'),
  cdpi: $('#cdpi'), cpages: $('#cpages'), crotate: $('#crotate'), cpassword: $('#cpassword'),
  cdestdir: $('#cdestdir'), cdirpick: $('#cdirpick'), chint: $('#chint'),
  engines: $('#engines'),
  vpath: $('#vpath'), vurl: $('#vurl'), vpick: $('#vpick'), vhint: $('#vhint'),
  extproxy: $('#chk-extproxy'), proxyurl: $('#proxyurl'), nethint: $('#nethint'),
  sidebar: $('#sidebar'), sbMask: $('#sb-mask'), btnMenu: $('#btn-menu'),
  pageTitle: $('#page-title'), navBusy: $('#nav-busy'),
  statBusy: $('#stat-busy'), statToday: $('#stat-today'),
  statTotal: $('#stat-total'), statEngines: $('#stat-engines'),
};

let S = null;              // 最近一次 /api/state
let failCount = 0;
const openLogs = new Set(); // 哪些任务的日志是展开的，重画时保住状态和滚动位置
const prevStates = new Map();  // 任务 id → 上一轮的状态，用来认出"状态刚变了"
const pendingFx = new Map();   // 这一轮要放的一次性动画
let dirCur = null;         // 目录浏览器当前路径
let browse = { box: el.dirpick, wantFiles: false, target: el.destdir };  // 当前是哪个输入框在叫浏览器
let cTargets = [];         // 当前文件能转的目标
let cCategory = '';        // 当前文件类别（图片/文档/PDF/音视频），用来给"选文件"配过滤器
let cTimer = null;         // 识别格式的防抖
let netLoaded = false;     // 网络设置只在首次拿到配置时回填，别每 350ms 覆盖用户正在输入的内容
let topChoice = 'sec-new'; // 顶部两张并排卡（下载/转换）当前选中的是哪张，滚动监听别把它们抢来抢去

// 侧边栏顺序；下载/转换是并排的两张卡，滚动时当成"顶部区域"处理
const NAV = [
  { id: 'sec-new', title: '下载', top: true },
  { id: 'sec-convert', title: '转换', top: true },
  { id: 'sec-tasks', title: '任务' },
  { id: 'sec-verify', title: '校验' },
  { id: 'sec-proxy', title: '代理源' },
  { id: 'sec-hist', title: '历史' },
];

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

function lines(text) {
  return String(text || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
}

function baseName(p) { return String(p || '').split(/[\\/]/).pop(); }

function setActiveNav(id) {
  const item = document.querySelector('.sb-item[data-nav="' + id + '"]');
  if (!item) return;
  document.querySelectorAll('.sb-item.active').forEach((a) => a.classList.remove('active'));
  item.classList.add('active');
  if (el.pageTitle) el.pageTitle.textContent = item.dataset.title || '';
}

function scrollActive() {
  const first = document.getElementById('sec-tasks');
  if (!first) return;
  const y = window.scrollY + 110;
  const firstTop = first.getBoundingClientRect().top + window.scrollY;
  if (y < firstTop - 10) {          // 还在顶部并排的两张卡区域：保留用户点的那张
    setActiveNav(topChoice);
    return;
  }
  let cur = 'sec-tasks';
  ['sec-tasks', 'sec-verify', 'sec-proxy', 'sec-hist'].forEach((id) => {
    const s = document.getElementById(id);
    if (s && s.getBoundingClientRect().top + window.scrollY <= y) cur = id;
  });
  setActiveNav(cur);
}

function openNav(open) {
  if (!el.sidebar || !el.sbMask) return;
  el.sidebar.classList.toggle('open', open);
  el.sbMask.classList.toggle('show', open);
  document.body.classList.toggle('nav-open', open);
}

function todayStr() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
         String(d.getDate()).padStart(2, '0');
}

function renderStats(r) {
  const tasks = r.tasks || [];
  const hist = r.history || [];
  const busy = tasks.filter((t) => BUSY.indexOf(t.state) >= 0).length;
  const today = todayStr();
  const todayN = hist.filter((h) => h.ok && (h.at || '').slice(0, 10) === today).length;
  const okN = hist.filter((h) => h.ok).length;
  const eng = r.engines || {};
  const keys = Object.keys(eng).filter((k) => k !== 'error');
  const engOk = keys.filter((k) => eng[k] && eng[k].ok).length;
  if (el.statBusy) el.statBusy.textContent = busy;
  if (el.statToday) el.statToday.textContent = todayN;
  if (el.statTotal) el.statTotal.textContent = okN;
  if (el.statEngines) el.statEngines.textContent = engOk + '/' + keys.length;
  if (el.navBusy) {
    el.navBusy.textContent = busy;
    el.navBusy.classList.toggle('hidden', !busy);
  }
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
  verify: '校验中', converting: '转换中', installing: '装引擎',
  done: '完成', failed: '失败', canceled: '已取消', need_asset: '要选文件',
};

const modeName = {
  auto: '自动', file: '按下文件', clone: 'git clone', verify: '本地校验',
  convert: '格式转换', install: '引擎安装',
};

const BUSY = ['queued', 'parsing', 'probing', 'downloading', 'verify', 'converting', 'installing'];

/* ---------------------------------------------------------------- 任务区 */

function taskCard(t) {
  const isConv = t.mode === 'convert';
  const isInst = t.mode === 'install';
  const busy = BUSY.indexOf(t.state) >= 0;
  const pct = t.total ? Math.min(100, (t.downloaded || 0) / t.total * 100) : null;
  let barCls = 'bar';
  if (t.state === 'done') barCls += ' done';
  else if (t.state === 'failed' || t.state === 'canceled') barCls += ' bad';
  if (busy && t.state !== 'verify') barCls += ' run';
  if (!t.total) barCls += ' unknown';
  const barStyle = pct === null ? 'width:100%;opacity:.4' : 'width:' + (pct || 0).toFixed(1) + '%';
  const att = (t.attempts || []);
  const used = t.proxy_used ? '源 <b>' + esc(t.proxy_used) + '</b>' : (t.proxy ? '候选 <b>' + esc(t.proxy) + '</b>' : '');
  const v = t.verify;
  const sum = v && (v.sha256 || v.note || v.archive || v.sig) ? summarize(v) : '';
  const outs = t.outputs || [];
  const badItems = (t.convert_items || []).filter((x) => !x.ok);
  const target = t.target || {};
  let meta = '';
  if (isConv) {
    meta = '<span>已完成 <b>' + (t.downloaded || 0) + '</b> / ' + (t.total || 0) + '</span>'
         + '<span>目标 <b>.' + esc(target.ext || '') + '</b></span>'
         + '<span>引擎 ' + esc(target.engine || '') + '</span>';
  } else if (isInst) {
    meta = '<span>' + fmtB(t.downloaded) + (t.total ? ' / ' + fmtB(t.total) : '') + '</span>'
         + '<span>速度 <b>' + fmtS(t.speed) + '</b></span>';
  } else {
    meta = '<span>' + fmtB(t.downloaded) + (t.total ? ' / ' + fmtB(t.total) + '（' + (pct || 0).toFixed(1) + '%）' : '') + '</span>'
         + '<span>速度 <b>' + fmtS(t.speed) + '</b></span>'
         + (t.eta ? '<span>约剩 ' + fmtDur(t.eta) + '</span>' : '')
         + '<span>' + used + '</span>';
  }
  const canRetry = (t.state === 'failed' || t.state === 'canceled') && (t.resumable || isConv || isInst);
  const destLabel = isConv ? '打开输出目录' : (isInst ? '打开安装位置' : '打开所在位置');
  return `
  <div class="task" data-tid="${esc(t.id)}">
    <div class="top">
      <span class="fn">${esc(t.filename || '（还没定文件名）')}</span>
      <span class="badge s-${esc(t.state)}">${esc(stateName[t.state] || t.state)}</span>
      ${att.length > 1 ? '<span class="muted">第 ' + (att.length) + ' 次尝试</span>' : ''}
      <span class="muted" style="margin-left:auto">${esc(modeName[t.mode] || t.mode || '自动')} · 用时 ${fmtDur(t.elapsed)}</span>
    </div>
    <div class="${barCls}"><i style="${barStyle}"></i></div>
    <div class="meta">${meta}</div>
    ${t.error ? '<div class="vres bad">' + esc(t.error) + '</div>' : ''}
    ${badItems.length ? '<div class="vres bad">' + esc(baseName(badItems[0].src)) + '：' + esc(badItems[0].error) + '</div>' : ''}
    ${sum ? '<div class="vres">' + esc(sum) + (v && v.sha256 ? ' <button class="tiny" data-act="copysha" data-sha="' + esc(v.sha256) + '">复制完整 SHA256</button>' : '') + '</div>' : ''}
    ${outs.length ? '<div class="outs">' + outs.slice(0, 10).map((p) =>
      '<button class="tiny" data-act="open" data-path="' + esc(p) + '">' + esc(baseName(p)) + '</button>').join('')
      + (outs.length > 10 ? '<span class="muted">等 ' + outs.length + ' 个</span>' : '') + '</div>' : ''}
    <div class="tbtns">
      ${t.dest ? '<button class="tiny" data-act="open" data-path="' + esc(t.dest) + '">' + destLabel + '</button>' : ''}
      ${busy ? '<button class="tiny" data-act="cancel" data-tid="' + esc(t.id) + '">取消</button>' : ''}
      ${canRetry ? '<button class="tiny" data-act="retry" data-tid="' + esc(t.id) + '">' + (isConv || isInst ? '重试' : '继续') + '</button>' : ''}
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
  const busy = list.filter((t) => BUSY.indexOf(t.state) >= 0).length;
  el.taskcount.textContent = list.length ? (busy ? '进行中的 ' + busy + ' 个' : '') : '';
  el.tasks.innerHTML = list.length
    ? list.map(taskCard).join('')
    : '<div class="muted empty">还没有任务。粘贴一个链接按开始下载，或者选文件去转换。</div>';
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
      <td data-label="序" class="mono">${i + 1}
        <button class="tiny" data-act="px-move" data-id="${esc(p.id)}" data-delta="-1" title="上移">↑</button>
        <button class="tiny" data-act="px-move" data-id="${esc(p.id)}" data-delta="1" title="下移">↓</button>
      </td>
      <td data-label="启用"><input type="checkbox" data-act="px-en" data-id="${esc(p.id)}" ${p.enabled ? 'checked' : ''}></td>
      <td data-label="名称">${esc(p.name)}</td>
      <td data-label="前缀" class="mono">${p.prefix ? esc(p.prefix) : '<span class="muted">（空 = 直连）</span>'}</td>
      <td data-label="测速">${ms}</td>
      <td data-label="成功" class="mono">${rate}</td>
      <td data-label="操作"><button class="tiny" data-act="px-del" data-id="${esc(p.id)}">删</button></td>
    </tr>`;
  }).join('');
}

function renderQuick(cfg) {
  const dirs = [];
  [cfg.last_dir].concat(cfg.recent_dirs || [], cfg.quick_dirs || [], [cfg.default_dir]).forEach((d) => {
    if (d && dirs.indexOf(d) < 0) dirs.push(d);
  });
  el.quick.innerHTML = dirs.slice(0, 10).map((d) =>
    '<span class="chip" data-act="chip" data-path="' + esc(d) + '" title="点一下填进目录框">' + esc(d) + '</span>').join('');
}

/* ---------------------------------------------------------------- 引擎区 */

const ENGINE_LABELS = [
  ['libreoffice', 'LibreOffice'], ['ffmpeg', 'FFmpeg'],
  ['pillow', '图片引擎'], ['pdfium', 'PDF 渲染'], ['pypdf', 'PDF 操作'],
  ['magick', 'ImageMagick'], ['pandoc', 'Pandoc'],
];

function renderEngines(eng) {
  eng = eng || {};
  const parts = ENGINE_LABELS.map(([id, name]) => {
    const e = eng[id] || {};
    const ok = !!e.ok;
    const canInstall = !ok && (id === 'magick' || id === 'pandoc');
    const btn = canInstall
      ? ' <button class="tiny" data-act="engine-install" data-id="' + id + '">下载便携版</button>' : '';
    return '<span class="eng ' + (ok ? 'ok' : 'bad') + '" title="' + esc(e.detail || '') + '">'
         + (ok ? '✓ ' : '✗ ') + esc(name) + btn + '</span>';
  });
  if (eng.error) parts.push('<span class="eng bad">' + esc(eng.error) + '</span>');
  el.engines.innerHTML = parts.join('');
}

/* ---------------------------------------------------------------- 历史区 */

function renderHist(items) {
  el.histcount.textContent = items.length ? '共 ' + items.length + ' 条（只留最近 300 条）' : '';
  el.histBody.innerHTML = items.length ? items.map((h) => `<tr>
      <td data-label="时间" class="mono">${esc((h.at || '').slice(5))}</td>
      <td data-label="文件" title="${esc(h.url || '')}">${esc(h.name)}${h.ok ? '' : ' <span class="bad-txt">失败</span>'}</td>
      <td data-label="大小" class="mono">${fmtB(h.bytes)}</td>
      <td data-label="速度" class="mono">${fmtS(h.speed)}</td>
      <td data-label="源" class="mono">${esc(h.proxy || '-')}${h.attempts > 1 ? '<span class="muted"> ×' + h.attempts + '</span>' : ''}</td>
      <td data-label="校验" class="mono" title="${esc(h.sha256 || '')}">${esc((h.verify || '').slice(0, 70))}</td>
      <td data-label="操作">${h.path ? '<button class="tiny" data-act="open" data-path="' + esc(h.path) + '">打开</button>' : ''}</td>
    </tr>`).join('')
    : '<tr><td colspan="7" class="muted">还没下过东西。</td></tr>';
}

/* ---------------------------------------------------------------- 目录 / 文件浏览器 */

async function browseDir(path, box, wantFiles, targetInput) {
  browse = { box: box, wantFiles: !!wantFiles, target: targetInput || el.destdir };
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
    const mkBtn = wantFiles ? ''
      : '<button class="tiny" data-act="mkdir">新建文件夹</button>';
    box.innerHTML = `
      <div class="cur">当前位置：${esc(dirCur || '（我的电脑）')}
        ${r.parent ? '<button class="tiny" data-act="dirgo" data-path="' + esc(r.parent) + '">上一级</button>' : ''}
        ${useBtn}${mkBtn}
        <span class="muted">${wantFiles ? '只能选文件；目录用来走到目标那层' : '只能进目录，看不到文件 —— 这里本来就是给下载选落点的'}</span>
      </div>
      ${r.error ? '<div class="bad-txt">' + esc(r.error) + '</div>' : ''}
      <div class="dirlist">${secs.join('')}</div>`;
    box.classList.remove('hidden');
  } catch (e) { banner('列目录失败：' + e.message); }
}

async function pickDir(input, box) {
  banner('系统选择框已打开，去那边选目录…');
  try {
    const r = await api('/api/pickdir?initial=' + encodeURIComponent(input.value.trim()));
    if (r.ok && (r.paths || []).length) { input.value = r.paths[0]; banner(''); return; }
    if (!r.ok && r.fallback) {
      banner(r.error + '，改用网页目录树');
      browseDir(input.value.trim(), box, false, input);
      return;
    }
    banner(r.error || '');
    if (!r.error) banner('');
  } catch (e) {
    banner('选择框失败：' + e.message + '，改用网页目录树');
    browseDir(input.value.trim(), box, false, input);
  }
}

async function pickConvertFiles() {
  const first = lines(el.cfiles.value)[0] || el.cdestdir.value.trim();
  const kindMap = { '图片': 'image', '文档': 'doc', 'PDF': 'doc', '音视频': 'media' };
  const kind = kindMap[cCategory] || 'all';
  banner('系统选择框已打开，去那边选文件…');
  try {
    const r = await api('/api/pickfiles?initial=' + encodeURIComponent(first) + '&kind=' + kind);
    if (r.ok && (r.paths || []).length) {
      const cur = lines(el.cfiles.value);
      r.paths.forEach((p) => { if (cur.indexOf(p) < 0) cur.push(p); });
      el.cfiles.value = cur.join('\n');
      banner('已选 ' + r.paths.length + ' 个文件', 'ok');
      setTimeout(() => banner(''), 2500);
      detectFormats();
      return;
    }
    if (r.ok) { banner(''); return; }          // 用户点了取消
    banner(r.error + (r.fallback ? '。可以直接把文件路径粘进上面的框（一行一个）' : ''));
  } catch (e) {
    banner('选择框失败：' + e.message + '。可以直接把文件路径粘进上面的框。');
  }
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
    el.assetbox.innerHTML = '<div class="hd">' + esc(r.repo + '  ' + (r.tag || '')) +
      ' 共 ' + r.assets.length + ' 个文件，点一下追加到上面的链接框：</div>' +
      r.assets.map((a) => '<div class="a" data-act="asset" data-url="' + esc(a.url) + '">' +
        '<span>' + esc(a.name) + '</span><span class="sz">' + fmtB(a.size) + '</span></div>').join('');
  } catch (e) {
    el.assetbox.innerHTML = '<div class="hd">' + esc(e.message) +
      '（api 域名这会儿不通是常事；去发布页右键复制具体文件的下载链接更稳）</div>';
  }
}

/* ---------------------------------------------------------------- 转换格式识别 */

function showConvertOptions() {
  const t = cTargets[Number(el.ctarget.value)] || null;
  const opts = (t && t.options) || [];
  el.copts.querySelectorAll('.opt').forEach((node) => {
    node.classList.toggle('hidden', opts.indexOf(node.dataset.opt) < 0);
  });
  el.copts.classList.toggle('hidden', !opts.length);
  el.ctargetHint.textContent = t ? (t.engine + ' · ' + t.group) : '';
}

async function detectFormats() {
  const paths = lines(el.cfiles.value);
  if (!paths.length) {
    cTargets = [];
    cCategory = '';
    el.ctarget.innerHTML = '<option value="">先选文件…</option>';
    el.cformatHint.textContent = '选好文件后这里会列出能转的格式';
    el.ctargetHint.textContent = '';
    el.copts.classList.add('hidden');
    return;
  }
  el.cformatHint.textContent = '识别中…';
  try {
    const r = await api('/api/formats', { paths: paths });
    if (!r.ok) {
      cTargets = [];
      el.ctarget.innerHTML = '<option value="">先选文件…</option>';
      el.cformatHint.textContent = r.error || '识别失败';
      return;
    }
    cTargets = r.targets || [];
    cCategory = r.category || '';
    el.ctarget.innerHTML = cTargets.length ? cTargets.map((t, i) =>
      '<option value="' + i + '">' + esc(t.label) + '（' + esc(t.engine) + '）</option>').join('')
      : '<option value="">没有可用目标</option>';
    el.cformatHint.textContent = r.category + ' · ' + r.count + ' 个文件 · 能转 ' + cTargets.length + ' 种';
    el.ctargetHint.textContent = (r.notes && r.notes.length) ? r.notes[0] : '';
    if (cTargets.length) showConvertOptions();
    else el.copts.classList.add('hidden');
  } catch (e) {
    cTargets = [];
    el.ctarget.innerHTML = '<option value="">先选文件…</option>';
    el.cformatHint.textContent = '识别失败：' + e.message;
  }
}

function convertOptions() {
  const t = cTargets[Number(el.ctarget.value)] || {};
  const need = t.options || [];
  const o = {};
  if (need.indexOf('quality') >= 0) o.quality = Number(el.cq.value) || 90;
  if (need.indexOf('ico_sizes') >= 0) o.ico_sizes = el.cicosize.value;
  if (need.indexOf('dpi') >= 0) o.dpi = Number(el.cdpi.value) || 150;
  if (need.indexOf('pages') >= 0) o.pages = el.cpages.value.trim();
  if (need.indexOf('rotate') >= 0) o.rotate = Number(el.crotate.value) || 90;
  if (need.indexOf('password') >= 0) o.password = el.cpassword.value;
  return o;
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
    else if (act === 'retry') {
      const r = await api('/api/retry', { id: b.dataset.tid });
      openLogs.add(b.dataset.tid);
      banner('已继续：' + r.task.id, 'ok');
      setTimeout(() => banner(''), 2500);
    }
    else if (act === 'engine-install') {
      const r = await api('/api/engineinstall', { id: b.dataset.id });
      openLogs.add(r.task.id);
      banner('开始下载便携引擎，进度看下面的任务卡', 'ok');
      poll(true);
    }
    else if (act === 'assets') { showAssets(b.dataset.url || lines(el.url.value)[0] || ''); }
    else if (act === 'asset') {
      const cur = el.url.value.trim();
      el.url.value = (cur ? cur + '\n' : '') + b.dataset.url;
      el.assetbox.classList.add('hidden');
    }
    else if (act === 'dirgo') { browseDir(b.dataset.path, browse.box, browse.wantFiles, browse.target); }
    else if (act === 'filepick') {
      browse.target.value = b.dataset.path;
      browse.box.classList.add('hidden');
      if (browse.target === el.vpath) el.vhint.textContent = '已选 ' + baseName(b.dataset.path);
    }
    else if (act === 'diruse') {
      if (dirCur && browse.target) browse.target.value = dirCur;
      browse.box.classList.add('hidden');
    }
    else if (act === 'mkdir') {
      const name = prompt('新文件夹名字（建在当前目录下）');
      if (!name) return;
      const r = await api('/api/mkdir', { parent: dirCur || '', name: name });
      await browseDir(r.path, browse.box, browse.wantFiles, browse.target);
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
  if (ev.target === el.ctarget) showConvertOptions();
});

document.addEventListener('input', (ev) => {
  if (ev.target === el.cq) el.cqv.textContent = el.cq.value;
  if (ev.target === el.cfiles) {
    clearTimeout(cTimer);
    cTimer = setTimeout(detectFormats, 500);
  }
});

document.addEventListener('toggle', (ev) => {
  const d = ev.target.closest && ev.target.closest('details.log');
  if (!d) return;
  if (d.open) openLogs.add(d.dataset.tid); else openLogs.delete(d.dataset.tid);
}, true);

/* ---------------------------------------------------------------- 侧边栏导航 */

document.querySelectorAll('[data-nav]').forEach((a) => {
  a.addEventListener('click', (ev) => {
    const id = a.dataset.nav;
    const sec = id && document.getElementById(id);
    if (!sec) return;
    ev.preventDefault();
    if (id === 'sec-new' || id === 'sec-convert') topChoice = id;
    setActiveNav(id);
    sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    openNav(false);
    try { history.replaceState(null, '', '#' + id); } catch (e) { /* file:// 下忽略 */ }
  });
});
$('#btn-menu').addEventListener('click', () => {
  openNav(!(el.sidebar && el.sidebar.classList.contains('open')));
});
$('#sb-mask').addEventListener('click', () => openNav(false));
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') openNav(false); });
window.addEventListener('resize', () => { if (window.innerWidth > 1024) openNav(false); });
let navTick = false;
window.addEventListener('scroll', () => {
  if (navTick) return;
  navTick = true;
  requestAnimationFrame(() => { navTick = false; scrollActive(); });
}, { passive: true });
setActiveNav(topChoice);
scrollActive();

$('#btn-go').addEventListener('click', start);
document.addEventListener('keydown', (ev) => {
  if (ev.ctrlKey && ev.key === 'Enter') start();
});
$('#btn-browse').addEventListener('click', () => pickDir(el.destdir, el.dirpick));
$('#btn-tree').addEventListener('click', () => {
  if (!el.dirpick.classList.contains('hidden')) { el.dirpick.classList.add('hidden'); return; }
  browseDir(el.destdir.value.trim() || '', el.dirpick, false, el.destdir);
});
$('#btn-cbrowse').addEventListener('click', () => pickDir(el.cdestdir, el.cdirpick));
$('#btn-cpick').addEventListener('click', pickConvertFiles);
$('#btn-cdetect').addEventListener('click', detectFormats);
$('#btn-cclear').addEventListener('click', () => {
  el.cfiles.value = '';
  cTargets = [];
  cCategory = '';
  el.ctarget.innerHTML = '<option value="">先选文件…</option>';
  el.cformatHint.textContent = '选好文件后这里会列出能转的格式';
  el.copts.classList.add('hidden');
});
$('#btn-convert').addEventListener('click', startConvert);
$('#btn-paste').addEventListener('click', async () => {
  try {
    const txt = await navigator.clipboard.readText();
    if (!txt.trim()) { banner('剪贴板是空的'); return; }
    const cur = el.url.value.trim();
    el.url.value = (cur ? cur + '\n' : '') + txt.trim();
    banner('已从剪贴板追加', 'ok');
    setTimeout(() => banner(''), 2000);
  } catch (e) { banner('浏览器不让读剪贴板，直接 Ctrl+V 吧'); }
});
$('#btn-vbrowse').addEventListener('click', () => {
  if (!el.vpick.classList.contains('hidden')) { el.vpick.classList.add('hidden'); return; }
  const p = el.vpath.value.trim();
  const here = p ? p.replace(/[\\/]+[^\\/]*$/, '') : '';
  browseDir(here || p, el.vpick, true, el.vpath);
});
$('#btn-verify').addEventListener('click', startVerify);
el.vpath.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') startVerify(); });
$('#btn-assets').addEventListener('click', () => showAssets(lines(el.url.value)[0] || ''));
$('#btn-save-net').addEventListener('click', saveNet);

async function saveNet() {
  try {
    await api('/api/config', {
      external_proxy: el.extproxy.checked,
      proxy_url: el.proxyurl.value.trim(),
    });
    el.nethint.textContent = '已保存';
    setTimeout(() => { el.nethint.textContent = ''; }, 2500);
    banner('网络设置已保存', 'ok');
    setTimeout(() => banner(''), 2500);
  } catch (e) { banner('保存失败：' + e.message); }
}

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
  const urls = lines(el.url.value);
  if (!urls.length) { banner('链接还没填'); return; }
  const mode = (document.querySelector('input[name=mode]:checked') || {}).value || 'auto';
  try {
    const r = await api('/api/download', {
      urls: urls,
      destdir: el.destdir.value.trim(),
      mode: mode,
      filename: el.wantname.value.trim(),
      force_html: el.forcehtml.checked,
      headers: {
        referer: el.referer.value.trim(),
        cookie: el.cookie.value.trim(),
        authorization: el.authorization.value.trim(),
      },
    });
    banner(r.warnings ? r.warnings.join('；') : '');
    el.gohint.textContent = r.tasks.length + ' 个任务已启动';
    r.tasks.forEach((t) => openLogs.add(t.id));
    await poll(true);
  } catch (e) { banner('启动失败：' + e.message); }
}

async function startConvert() {
  const paths = lines(el.cfiles.value);
  if (!paths.length) { banner('先选要转换的文件'); return; }
  const target = cTargets[Number(el.ctarget.value)];
  if (!target) { banner('还没选目标格式，先点「识别格式」'); return; }
  const outdir = el.cdestdir.value.trim();
  try {
    const r = await api('/api/convert', {
      paths: paths,
      target: target,
      outdir: outdir,
      options: convertOptions(),
    });
    banner('');
    el.chint.textContent = '转换任务 ' + r.task.id + ' 已启动，进度看下面的任务卡';
    openLogs.add(r.task.id);
    await poll(true);
  } catch (e) { banner('转换启动失败：' + e.message); }
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
    const cfg = r.config || {};
    if (!el.destdir.value.trim()) el.destdir.value = cfg.last_dir || cfg.default_dir || '';
    if (!el.cdestdir.value.trim()) el.cdestdir.value = cfg.last_dir || cfg.default_dir || '';
    el.verify.checked = cfg.verify !== false;
    if (!netLoaded) {
      netLoaded = true;
      el.extproxy.checked = !!cfg.external_proxy;
      el.proxyurl.value = cfg.proxy_url || '';
    }
    renderQuick(cfg);
    renderEngines(r.engines || {});
    renderStats(r);
    renderProxies(r.proxies || []);
    renderHist(r.history || []);
    renderTasks(r.tasks || []);
  } catch (e) {
    if (++failCount === 2) banner('和后台失联了：' + e.message + '（服务窗口被关了？重新双击 ghdl.bat 选 1 即可）');
    el.status.textContent = '连不上后台';
    el.status.className = 'pill off';
  }
  const busy = S && (S.tasks || []).some((t) => BUSY.indexOf(t.state) >= 0);
  setTimeout(poll, busy ? 350 : 1200);
}

poll(true);
