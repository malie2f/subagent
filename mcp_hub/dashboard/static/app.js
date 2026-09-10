// mcp-hub dashboard 前端逻辑

const POLL_MS = 8000; // 仅当 SSE 不可用时的后备
let autoRefresh = true;
let pollTimer = null;
let dashEvents = null; // 看板状态 SSE（有变化才刷）
let sseFail = 0;
let currentTab = 'connect';
let currentSubagent = null;
let liveEventSource = null;  // 当前正在实时 tail 的 SSE
let liveStreamTaskId = null; // 当前正在实时看的 task_id
// SSE 日志跟随滚动（用户上翻时自动暂停），开关状态 localStorage 记忆，默认开
let liveAutoScroll = localStorage.getItem('mchub.liveAutoScroll') !== '0';
// 当前日志面板所在根节点：子 Agent / 任务两个详情面板的元素 id 相同，
// 靠 root 限定 querySelector 范围，避免跨 tab 拿到隐藏面板的同名元素
let liveRoot = null;
let usageLastFetch = 0;      // 用量 tab 30s 节流（/api/usage 聚合有开销）

// 封存会话显示开关
let showArchivedSubagents = false;
let showArchivedTasks = false;

// dispatch tab 状态：本会话派过的所有 task_id
const dispatchedTasks = [];  // [{task_id, topic, payload, submitted_at}]

// ---------- 工具 ----------

async function fetchJson(url) {
  const r = await fetch(url);
  return await r.json();
}

/**
 * 轮询刷新用 Idiomorph（HTMX / Turbo 同款）就地 morph，而不是 innerHTML 整段替换。
 * innerHTML 会拆掉节点 → 输入框清空、光标丢失、滚动回顶、监听器重复绑定。
 * ignoreActiveValue + 不改用户正在改的 value，保证打字不被 2s 轮询盖掉。
 */
const MORPH_OPTS = {
  morphStyle: 'innerHTML',
  ignoreActiveValue: true,
  restoreFocus: true,
  callbacks: {
    beforeAttributeUpdated(attr, node) {
      if (attr !== 'value' && attr !== 'checked') return;
      if (node.dataset && node.dataset.live === '1') return;
      const tag = node.tagName;
      if (tag === 'TEXTAREA') return false;
      if (tag === 'INPUT') {
        const t = (node.type || 'text').toLowerCase();
        if (t === 'checkbox' || t === 'radio' || t === 'hidden' || t === 'submit' || t === 'button') return;
        return false;
      }
    },
  },
};

function setHtml(el, html) {
  if (!el) return;
  const htmlStr = html == null ? '' : String(html);
  const scroll = el.scrollTop;
  const opts = Object.assign({}, MORPH_OPTS, {
    callbacks: Object.assign({}, MORPH_OPTS.callbacks, {
      beforeNodeMorphed(oldNode, newNode) {
        if (oldNode !== el && oldNode.classList && oldNode.classList.contains('js-keep')) return false;
        if (MORPH_OPTS.callbacks.beforeNodeMorphed) {
          return MORPH_OPTS.callbacks.beforeNodeMorphed(oldNode, newNode);
        }
      },
    }),
  });
  if (typeof Idiomorph !== 'undefined' && typeof Idiomorph.morph === 'function') {
    Idiomorph.morph(el, htmlStr, opts);
  } else {
    el.innerHTML = htmlStr;
  }
  if (el.scrollTop !== scroll) el.scrollTop = scroll;
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * 极简 markdown 渲染（会话查看用）：代码块/行内代码/粗斜体/链接/标题/列表。
 * 先 escape 再套规则，不会引入 XSS。代码块先抽出来占位，最后换回，防止内部被行内规则污染。
 */
function renderMarkdown(src) {
  if (!src) return '';
  const blocks = [];
  let text = String(src).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre class="md-pre"><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
    return `@@MD@@${blocks.length - 1}@@MD@@`;
  });
  text = escapeHtml(text);
  text = text.replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  text = text.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = text.split('\n');
  const out = [];
  let inList = null;
  const closeList = () => {
    if (inList) { out.push(inList === 'ul' ? '</ul>' : '</ol>'); inList = null; }
  };
  for (const line of lines) {
    let m;
    if (/^@@MD@@\d+@@MD@@$/.test(line.trim())) {
      // 代码块占位符独占一行，不要包 <p>
      closeList();
      out.push(line.trim());
    } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList();
      const lvl = Math.min(m[1].length + 2, 6); // h3-h6，避免喧宾夺主
      out.push(`<h${lvl} class="md-h">${m[2]}</h${lvl}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (inList !== 'ul') { closeList(); out.push('<ul class="md-list">'); inList = 'ul'; }
      out.push(`<li>${m[1]}</li>`);
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      if (inList !== 'ol') { closeList(); out.push('<ol class="md-list">'); inList = 'ol'; }
      out.push(`<li>${m[1]}</li>`);
    } else if (line.trim() === '') {
      closeList();
    } else {
      closeList();
      out.push(`<p class="md-p">${line}</p>`);
    }
  }
  closeList();
  return out.join('').replace(/@@MD@@(\d+)@@MD@@/g, (_, i) => blocks[+i]);
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtTimeInline(ts) {
  if (!ts) return '';
  return `<span class="ts">${fmtTime(ts)}</span>`;
}

function fmtDuration(sec) {
  if (!sec || sec < 0) return '-';
  if (sec < 60) return sec.toFixed(1) + 's';
  if (sec < 3600) return (sec / 60).toFixed(1) + 'm';
  return (sec / 3600).toFixed(1) + 'h';
}

// 运行时长徽章用：mm:ss，超过 1 小时显示 "2h 05m"
function fmtElapsed(sec) {
  if (!sec || sec < 0) sec = 0;
  const s = Math.floor(sec);
  if (s < 3600) {
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

function fmtBytes(b) {
  if (!b) return '0 B';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1024 / 1024).toFixed(1) + ' MB';
}

function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function fmtCost(c) {
  if (!c) return '$0';
  return '$' + (c >= 1 ? c.toFixed(2) : c.toFixed(4));
}

function makeTaskTitle(fromModel, createdAt, payload) {
  // 生成用户可读的会话标题：使用者 + 时间 + 请求摘要
  const who = fromModel && fromModel !== 'unknown' ? fromModel : '用户';
  const time = fmtTime(createdAt);
  const summary = (payload || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 45)
    + ((payload || '').length > 45 ? '…' : '');
  return `${who} ${time} · ${summary}`;
}

// ---------- Tabs ----------

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    stopLiveStream();  // 切 tab 时关掉实时流
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
    btn.classList.add('active');
    currentTab = btn.dataset.tab;
    document.getElementById('tab-' + currentTab).classList.remove('hidden');
    refresh(true);  // 切 tab 立即刷一次（强制，绕过 usage 30s 节流）
  });
});

// ---------- Auto refresh ----------

document.getElementById('auto-refresh').addEventListener('change', e => {
  autoRefresh = e.target.checked;
  if (autoRefresh) startLive();
  else {
    stopLive();
    document.getElementById('auto-refresh-status').textContent = '⏸ 已暂停';
  }
});

document.getElementById('refresh-btn').addEventListener('click', () => refresh(true));

// ---------- 主题切换：月之暗面（默认）/ 月之亮面 ----------
const themeToggleBtn = document.getElementById('theme-toggle');
function applyTheme(t) {
  document.documentElement.classList.toggle('moon-light', t === 'moon-light');
  themeToggleBtn.textContent = t === 'moon-light' ? '☀ 月之亮面' : '☾ 月之暗面';
  themeToggleBtn.title = '当前：' + (t === 'moon-light' ? '月之亮面（日间）' : '月之暗面（夜间）') + '，点击切换';
  localStorage.setItem('mcp-hub-theme', t);
}
themeToggleBtn.addEventListener('click', () => {
  applyTheme(document.documentElement.classList.contains('moon-light') ? 'moon-dark' : 'moon-light');
});
applyTheme(localStorage.getItem('mcp-hub-theme') || 'moon-dark');

// 封存会话开关
document.getElementById('subagent-show-archived').addEventListener('change', e => {
  showArchivedSubagents = e.target.checked;
  renderSubagents();
});
document.getElementById('task-show-archived').addEventListener('change', e => {
  showArchivedTasks = e.target.checked;
  renderTasks();
});

let pollInFlight = false;
let pendingRefresh = null; // null=无排队；boolean=排队的 force

function tabNeeds(tab, keys) {
  if (!keys || !keys.length) return true;
  const map = {
    connect: ['connections'],
    overview: ['connections', 'tasks', 'subagents'],
    dispatch: ['tasks'],
    runtimes: ['connections'],
    subagents: ['subagents', 'tasks'],
    cluster: ['crews', 'tasks', 'subagents'],
    crew: ['crews', 'subagents'],
    tasks: ['tasks', 'subagents'],
    usage: [],
  };
  const need = map[tab];
  if (!need) return true;
  if (!need.length) return false;
  return keys.some(k => need.indexOf(k) !== -1);
}

function startPoll() {
  stopPoll();
  pollTimer = setInterval(() => { refresh(false); }, POLL_MS);
  const st = document.getElementById('auto-refresh-status');
  if (st && autoRefresh) st.textContent = '▶ 轮询后备';
}

function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function stopLive() {
  if (dashEvents) {
    dashEvents.close();
    dashEvents = null;
  }
  stopPoll();
}

function startLive() {
  stopLive();
  if (!autoRefresh) return;
  if (typeof EventSource === 'undefined') {
    startPoll();
    return;
  }
  const es = new EventSource('/api/events');
  dashEvents = es;
  es.addEventListener('hello', () => {
    sseFail = 0;
    stopPoll();
    const st = document.getElementById('auto-refresh-status');
    if (st) st.textContent = '▶ 推送中';
    refresh(true);
  });
  es.addEventListener('dirty', (ev) => {
    if (document.hidden) return;
    let keys = [];
    try { keys = (JSON.parse(ev.data) || {}).keys || []; } catch (e) { keys = []; }
    if (tabNeeds(currentTab, keys)) refresh(true);
  });
  es.onerror = () => {
    sseFail += 1;
    if (sseFail >= 3 && autoRefresh && !pollTimer) startPoll();
  };
}

async function refresh(force) {
  const wantForce = force === true;
  if (pollInFlight) {
    pendingRefresh = wantForce || pendingRefresh === true;
    return;
  }
  pollInFlight = true;
  try {
    let runForce = wantForce;
    for (;;) {
      const tab = currentTab;
      switch (tab) {
        case 'overview':  await renderOverview(); break;
        case 'dispatch':  await renderDispatch(); break;
        case 'runtimes':  await renderRuntimes(); break;
        case 'subagents': await renderSubagents(); break;
        case 'cluster':   await renderCluster(); break;
        case 'tasks':     await renderTasks(); break;
        case 'usage':     await renderUsage(runForce); break;
        case 'connect':   await renderConnect(runForce); break;
        case 'crew':      await renderCrew(); break;
      }
      document.getElementById('last-update').textContent = '更新于 ' + fmtTime(Date.now() / 1000);
      if (pendingRefresh === null) break;
      runForce = pendingRefresh === true;
      pendingRefresh = null;
    }
  } finally {
    pollInFlight = false;
  }
}

// 全局缓存 models（派活 tab 用）
let modelsCache = null;

// dispatch tab form 渲染状态：避免 2s 轮询 innerHTML 重写把用户正在编辑的内容清掉
let dispatchFormRendered = false;

// 暴露给 inline onclick
window.toggleTaskDetail = toggleTaskDetail;
window.submitVerify = submitVerify;
window.selectCrew = selectCrew;
window.selectCrewMember = selectCrewMember;
window.crewSupervise = crewSupervise;
window.openSubagentTranscript = openSubagentTranscript;

// ---------- 实时日志流（SSE） ----------

function stopLiveStream() {
  if (liveEventSource) {
    liveEventSource.close();
    liveEventSource = null;
  }
  liveStreamTaskId = null;
  liveRoot = null;
}

// 在 liveRoot 范围内查元素（没有 root 时退回全文档）
function liveEl(id) {
  return liveRoot ? liveRoot.querySelector('#' + id) : document.getElementById(id);
}

function saveLiveAutoScroll() {
  try { localStorage.setItem('mchub.liveAutoScroll', liveAutoScroll ? '1' : '0'); } catch (e) { /* 隐私模式等场景忽略 */ }
}

// 把 liveAutoScroll 状态同步到 checkbox 和"回到底部"提示按钮
function syncLiveScrollUI() {
  const cb = liveEl('live-autoscroll');
  if (cb) cb.checked = liveAutoScroll;
  const jump = liveEl('live-jump-latest');
  if (jump) jump.classList.toggle('hidden', liveAutoScroll);
}

function setLiveAutoScroll(on, pre) {
  liveAutoScroll = on;
  saveLiveAutoScroll();
  syncLiveScrollUI();
  if (on && pre) pre.scrollTop = pre.scrollHeight;
}

/**
 * 绑定日志面板的控制条（跟随滚动开关 / 清空 / 回到底部提示 / 状态点击重连）。
 * 每次详情面板 innerHTML 重渲染后都要重绑一次。
 */
function bindLiveLogControls(root) {
  liveRoot = root || liveRoot;
  const container = liveEl('live-log-container');
  if (!container) return;
  const pre = container.querySelector('pre');
  const cb = liveEl('live-autoscroll');
  const clearBtn = liveEl('live-clear');
  const jumpBtn = liveEl('live-jump-latest');
  const statusEl = liveEl('live-status');
  if (cb) {
    cb.addEventListener('change', () => setLiveAutoScroll(cb.checked, pre));
  }
  if (clearBtn && pre) {
    clearBtn.addEventListener('click', () => { pre.textContent = ''; });
  }
  if (jumpBtn && pre) {
    jumpBtn.addEventListener('click', () => setLiveAutoScroll(true, pre));
  }
  if (statusEl) {
    // 状态处于"已断开"时可点击手动重连（EventSource 自动重连之外的兜底）
    statusEl.addEventListener('click', () => {
      if (statusEl.classList.contains('reconnect') && statusEl.dataset.tid) {
        const tid = statusEl.dataset.tid;
        stopLiveStream();
        liveRoot = root || liveRoot;  // stopLiveStream 清了 liveRoot，恢复
        startLiveStream(tid, true);
      }
    });
  }
  if (pre) {
    // 用户上翻 = 暂停跟随并提示；滚回底部 = 恢复跟随
    pre.addEventListener('scroll', () => {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
      if (atBottom !== liveAutoScroll) {
        liveAutoScroll = atBottom;
        saveLiveAutoScroll();
        syncLiveScrollUI();
      }
    });
  }
  // 初始状态：记住的开关同步到 UI；跟随中则先滚到底
  syncLiveScrollUI();
  if (liveAutoScroll && pre) pre.scrollTop = pre.scrollHeight;
}

/**
 * 实时日志 SSE。只有 running 任务才开连接：
 * 已结束的任务静态日志已经在面板里，开着 SSE 只会白占一个服务端线程
 * （后端 stream 是无限 tail + 心跳，永远不会自己结束）。
 */
function startLiveStream(tid, isRunning = true) {
  // 避免重复连接同一个 task
  if (liveStreamTaskId === tid && liveEventSource) return;
  const root = liveRoot;  // stopLiveStream 会清 liveRoot，先存后恢复（面板 root 由 bindLiveLogControls 设定）
  stopLiveStream();
  liveRoot = root;

  const statusEl = liveEl('live-status');
  const container = liveEl('live-log-container');
  if (!statusEl || !container) return;

  if (!isRunning) {
    statusEl.textContent = '■ 任务已结束（静态日志）';
    statusEl.style.color = 'var(--text-dim)';
    statusEl.classList.remove('reconnect');
    statusEl.title = '';
    return;
  }
  liveStreamTaskId = tid;

  statusEl.dataset.tid = tid;
  statusEl.classList.remove('reconnect');
  statusEl.textContent = '○ 连接中...';
  statusEl.style.color = 'var(--warn)';

  const es = new EventSource(`/api/subagents/${encodeURIComponent(tid)}/stream`);
  liveEventSource = es;

  // 详情面板可能整体重渲染，statusEl/pre 都会换成新元素，
  // 所以回调里每次现查 DOM，别闭包引用旧元素（否则会往 detached 节点里 append）
  es.onopen = () => {
    const el = liveEl('live-status');
    if (el && liveStreamTaskId === tid) {
      el.textContent = '● 已连接';
      el.style.color = 'var(--ok)';
      el.classList.remove('reconnect');
      el.title = 'SSE 实时日志流已连接';
    }
  };
  es.onerror = () => {
    const el = liveEl('live-status');
    // EventSource 默认会自动重连，不用手动 close；同时允许点击立即重连
    if (el && liveStreamTaskId === tid) {
      el.dataset.tid = tid;
      el.textContent = '× 已断开（点击重连）';
      el.style.color = 'var(--err)';
      el.classList.add('reconnect');
      el.title = '连接断开，EventSource 自动重连中；点击可立即重连';
    }
  };
  es.onmessage = (e) => {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    if (data.type === 'log') {
      const c = liveEl('live-log-container');
      const pre = c && c.querySelector('pre');
      if (pre) {
        pre.textContent += data.content;
        if (liveAutoScroll) pre.scrollTop = pre.scrollHeight;
      }
    } else if (data.type === 'meta') {
      const el = liveEl('live-status');
      if (el) el.title = data.path || '';
    } else if (data.type === 'error') {
      const el = liveEl('live-status');
      if (el) {
        el.dataset.tid = tid;
        el.textContent = '错误: ' + (data.message || '') + '（点击重连）';
        el.style.color = 'var(--err)';
        el.classList.add('reconnect');
      }
      es.close();
      liveEventSource = null;
      liveStreamTaskId = null;
    } else if (data.type === 'heartbeat') {
      // 心跳，什么都不做
    }
  };
}

// ---------- Pinned models 编辑 ----------

async function loadPinnedModels() {
  const r = await fetchJson('/api/dashboard/pinned-models');
  const fromEnv = r.from_env || [];
  const fromDash = r.from_dashboard || [];
  // 显示：只显示 dashboard 存的（这个是用户能编辑的）
  // .env 里的作为只读背景
  document.getElementById('pinned-models').value = fromDash.join('\n');
  // 来源说明
  const sources = [];
  if (fromEnv.length) sources.push(`.env (${fromEnv.length})`);
  if (fromDash.length) sources.push(`dashboard (${fromDash.length})`);
  document.getElementById('pinned-source').textContent = sources.length ? sources.join(' + ') : '（无，显示全部）';
  return r.pinned_models;
}

async function savePinnedModels() {
  const text = document.getElementById('pinned-models').value;
  const models = text.split('\n').map(s => s.trim()).filter(s => s);
  const statusEl = document.getElementById('pinned-status');
  statusEl.textContent = '保存中...';
  statusEl.style.color = 'var(--text-dim)';
  try {
    const r = await fetch('/api/dashboard/pinned-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinned_models: models }),
    });
    const data = await r.json();
    if (data.ok) {
      statusEl.textContent = `已保存 (${data.pinned_models.length} 个)`;
      statusEl.style.color = 'var(--ok)';
      // 重新拉 models 让卡片墙刷新
      modelsCache = null;
      await renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = 'var(--err)';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = 'var(--err)';
  }
}

async function clearPinnedModels() {
  document.getElementById('pinned-models').value = '';
  await savePinnedModels();
}

document.getElementById('pinned-save').addEventListener('click', savePinnedModels);
document.getElementById('pinned-clear').addEventListener('click', clearPinnedModels);
document.getElementById('pinned-reload').addEventListener('click', loadPinnedModels);

// 写完即保存：白名单 textarea 失焦自动保存（不用每次点按钮）
document.getElementById('pinned-models').addEventListener('blur', () => {
  // 只在有内容时保存（避免误触）
  const text = document.getElementById('pinned-models').value;
  if (text.trim()) {
    savePinnedModels();
  }
});

// "重置表单" 按钮：清掉渲染标志，强制重新拉 models / 白名单
const resetFormBtn = document.getElementById('dispatch-refresh-form');
if (resetFormBtn) {
  resetFormBtn.addEventListener('click', resetDispatchForm);
}

// ---------- Overview ----------

async function renderOverview() {
  const o = await fetchJson('/api/overview');

  document.getElementById('runtime-count').textContent = o.runtimes.count;
  const rl = document.getElementById('runtime-list');
  setHtml(rl, o.runtimes.items.map(r => `
    <div class="runtime-item" id="ov-rt-${escapeHtml(r.name)}">
      <span class="dot ${r.available ? 'green' : 'red'}"></span>
      <span class="runtime-name">${escapeHtml(r.name)}</span>
      <span class="runtime-models">${r.models.length ? r.models.slice(0, 3).join(', ') + (r.models.length > 3 ? '…' : '') : '(stub)'}</span>
    </div>
  `).join('') || '<div class="muted">无运行时</div>');

  document.getElementById('tool-count').textContent = o.tools.count;
  const tl = document.getElementById('tool-list');
  setHtml(tl, o.tools.items.map(t => `
    <div class="tool-item" id="ov-tool-${escapeHtml(t.name)}">
      <span class="dot ${t.available ? 'green' : 'red'}"></span>
      <span class="tool-name">${escapeHtml(t.name)}</span>
      <span class="tool-ops">${(t.operations || []).slice(0, 4).join(', ')}</span>
    </div>
  `).join('') || '<div class="muted">无工具</div>');

  const cfg = o.config;
  setHtml(document.getElementById('cluster-summary'), `
    <table class="detail-table">
      <tr><td>状态</td><td>${cfg.cluster_enabled ? `<span class="dot green"></span> 已开启` : `<span class="dot gray"></span> 已关闭（无 worker，派活不会被认领）`}</td></tr>
      <tr><td>数量</td><td>${cfg.cluster_size || 0}</td></tr>
      <tr><td>模型</td><td>${escapeHtml(cfg.cluster_model)}</td></tr>
      <tr><td>主题</td><td>${escapeHtml(cfg.cluster_topic)}</td></tr>
      <tr><td>队列</td><td>${escapeHtml(cfg.queue_path)}</td></tr>
    </table>
  `);

  const q = o.queue;
  const s = q.stats || {};
  setHtml(document.getElementById('queue-summary'), `
    <table class="detail-table">
      <tr><td>总数</td><td>${s.total || 0}</td></tr>
      ${Object.entries(s.by_status || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}
      <tr><td>主题数</td><td>${(q.topics || []).length}</td></tr>
    </table>
  `);
}

// ---------- Runtimes ----------

async function renderRuntimes() {
  const r = await fetchJson('/api/runtimes');
  const el = document.getElementById('runtime-detail');
  setHtml(el, r.items.map(rt => `
    <div class="card" id="rt-${escapeHtml(rt.name)}" style="margin-bottom: 12px">
      <h2>
        <span class="dot ${rt.available ? 'green' : 'red'}"></span>
        ${escapeHtml(rt.name)}
        <span class="muted" style="font-weight: 400">二进制=${escapeHtml(rt.binary)}</span>
      </h2>
      ${rt.status ? `<div class="muted" style="margin-bottom: 8px">状态: ${escapeHtml(rt.status)} · ${escapeHtml(rt.note || '')}</div>` : ''}
      <div class="muted" style="font-size: 12px">${rt.models.length ? rt.models.map(m => `<span style="background: var(--border-dim); padding: 1px 6px; margin-right: 4px; display: inline-block; margin-bottom: 4px">${escapeHtml(m)}</span>`).join('') : '（占位，无可用模型）'}</div>
    </div>
  `).join('') || '<div class="muted">无运行时</div>');
}

// ---------- Transcript 渲染 ----------

/**
 * 把 transcript events 渲染成 chat-like 视图。
 * events 是从 /api/subagents/<id>/transcript 拿到的 list of dict。
 * 第一个 event 通常是 type:"prompt"，后续是 turn/tool_call/tool_result/file_change/final/error。
 */
function renderTranscriptView(events, isRunning = false, durationSec = 0) {
  if (!events || !events.length) {
    return '<div class="muted">（无 transcript 内容）</div>';
  }
  const parts = [];
  for (const ev of events) {
    const t = ev.type;
    const ts = fmtTimeInline(ev.ts);
    if (t === 'prompt') {
      parts.push(`
        <div class="msg msg-prompt">
          <div class="msg-role">📝 Prompt ${ts}</div>
          <div class="msg-content">${escapeHtml(ev.content || '')}</div>
        </div>
      `);
    } else if (t === 'turn') {
      const role = ev.role || 'assistant';
      const cls = role === 'assistant' ? 'msg-assistant' : (role === 'reasoning' ? 'msg-reasoning' : 'msg-user');
      const icon = role === 'assistant' ? '🤖' : (role === 'reasoning' ? '💭' : '👤');
      let body;
      if (role === 'reasoning') {
        // reasoning 往往很长，默认折叠，点开看全文
        const content = ev.content || '';
        body = `
          <details class="reasoning-fold">
            <summary>推理过程（${content.length} 字符，点击展开）</summary>
            <div class="msg-content">${escapeHtml(content)}</div>
          </details>
        `;
      } else if (role === 'assistant') {
        body = `<div class="msg-content md">${renderMarkdown(ev.content || '')}</div>`;
      } else {
        body = `<div class="msg-content">${escapeHtml(ev.content || '')}</div>`;
      }
      parts.push(`
        <div class="msg ${cls}">
          <div class="msg-role">${icon} ${escapeHtml(role)} ${ts}</div>
          ${body}
        </div>
      `);
    } else if (t === 'tool_call') {
      const name = ev.name || '?';
      const args = ev.args || {};
      const argsStr = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
      // 长参数默认折叠成一行摘要，点开看完整 JSON
      const ARGS_INLINE_MAX = 300;
      const argsHtml = argsStr.length > ARGS_INLINE_MAX
        ? `
          <details class="args-fold">
            <summary>参数（${argsStr.length} 字符）: ${escapeHtml(argsStr.slice(0, 80).replace(/\s+/g, ' '))}…</summary>
            <pre class="msg-args">${escapeHtml(argsStr)}</pre>
          </details>
        `
        : `<pre class="msg-args">${escapeHtml(argsStr)}</pre>`;
      const resultPreview = ev.result_preview ? `
        <details class="tool-result-preview">
          <summary>结果预览</summary>
          <pre>${escapeHtml(ev.result_preview)}</pre>
        </details>
      ` : '';
      const status = ev.status ? `<span class="tool-status">${escapeHtml(ev.status)}</span>` : '';
      parts.push(`
        <div class="msg msg-tool">
          <div class="msg-role">🔧 工具调用 · ${escapeHtml(name)} ${status} ${ts}</div>
          ${argsHtml}
          ${resultPreview}
        </div>
      `);
    } else if (t === 'usage') {
      // usage 聚合成一行小字，别糊一大坨 JSON
      const tk = ev.tokens || {};
      const cache = tk.cache || {};
      const cachePart = cache.read ? ` · cache ${fmtTokens(cache.read)}` : '';
      const costPart = ev.cost ? ` · cost ${fmtCost(ev.cost)}` : '';
      parts.push(`
        <div class="usage-line">📊 tokens in ${fmtTokens(tk.input)} / out ${fmtTokens(tk.output)}${tk.reasoning ? ' / reasoning ' + fmtTokens(tk.reasoning) : ''} / 共 ${fmtTokens(tk.total)}${cachePart}${costPart} ${ts}</div>
      `);
    } else if (t === 'tool_result') {
      const ok = ev.is_error ? 'msg-tool-result-err' : 'msg-tool-result';
      const content = typeof ev.content === 'string' ? ev.content : JSON.stringify(ev.content, null, 2);
      const truncated = content.length > 2000 ? content.slice(0, 2000) + '\n...(truncated)' : content;
      parts.push(`
        <div class="msg ${ok}">
          <div class="msg-role">${ev.is_error ? '❌ 工具返回（错误）' : '✅ 工具返回'} ${ts}</div>
          <pre class="msg-args">${escapeHtml(truncated)}</pre>
        </div>
      `);
    } else if (t === 'file_change') {
      const icon = ev.action === 'create' ? '✨' : (ev.action === 'delete' ? '🗑' : '✏️');
      parts.push(`
        <div class="msg msg-file">
          <div class="msg-role">${icon} 文件${escapeHtml(ev.action || 'modify')} ${ts}</div>
          <div class="msg-content"><code>${escapeHtml(ev.path || '?')}</code></div>
        </div>
      `);
    } else if (t === 'final') {
      parts.push(`
        <div class="msg msg-final">
          <div class="msg-role">🎯 最终回答 <span class="muted">(${escapeHtml(ev.stop_reason || 'end_turn')})</span> ${ts}</div>
          <div class="msg-content md">${renderMarkdown(ev.content || '')}</div>
        </div>
      `);
    } else if (t === 'error') {
      parts.push(`
        <div class="msg msg-error">
          <div class="msg-role">❌ 错误 ${ts}</div>
          <div class="msg-content">${escapeHtml(ev.message || '')}</div>
        </div>
      `);
    }
    // 忽略其它未知 type
  }
  if (isRunning) {
    const elapsed = durationSec ? ` · 已运行 ${fmtDuration(durationSec)}` : '';
    parts.push(`
      <div class="msg msg-running">
        <div class="msg-role">⏳ 模型工作中<span class="working-dots"><span>.</span><span>.</span><span>.</span></span>${elapsed}</div>
        <div class="msg-content muted">模型仍在处理，最新事件见上方；长时间无新事件可判断为卡死。</div>
      </div>
    `);
  }
  return parts.join('');
}

/**
 * 给"子 Agent"详情用的：拉 transcript 并展示成折叠面板。
 */
async function openSubagentTranscript(tid, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '<div class="muted">加载 transcript...</div>';
  try {
    const r = await fetchJson(`/api/subagents/${tid}/transcript`);
    if (!r.ok) {
      el.innerHTML = `<div class="muted">无 transcript: ${escapeHtml(r.error || '未知')}<br>路径（应存在）: <code>${escapeHtml(r.transcript_path_expected || '')}</code></div>`;
      return;
    }
    const isRunning = r.status === 'running' || r.status === 'claimed';
    el.innerHTML = `
      <div class="transcript-header">
        <span class="muted">${r.event_count} 个事件</span>
        <span class="muted">${fmtBytes(r.size)} · ${escapeHtml(r.path)}</span>
        ${r.live ? '<span style="color:var(--ok-bright);font-size:11px">● 实时预览</span>' : ''}
      </div>
      <div class="transcript-body">${renderTranscriptView(r.events, isRunning)}</div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="muted" style="color: var(--err)">异常: ${escapeHtml(e.message)}</div>`;
  }
}

// ---------- Subagents ----------

// 思考档位徽章：真值（spawn 时调用方指定的 reasoning_effort）优先，
// 没有则从模型变体名推断（推断的标注来源，避免误导）
function effortBadge(model, real) {
  if (real) {
    return `<span class="effort-badge" title="思考档位（调用时指定）">⚡${escapeHtml(real)}</span>`;
  }
  if (!model) return '';
  const m = model.toLowerCase();
  let effort = null;
  const suffix = m.match(/-(none|minimal|low|medium|high|xhigh|thinking)$/);
  if (suffix) {
    effort = suffix[1];
  } else if (/-max$/.test(m) && /gpt|deepseek|sol|terra|luna/.test(m)) {
    // qwen3.7-max 这类 "-max" 是型号大小不是思考档，只在 gpt/deepseek 家族认
    effort = 'max';
  }
  if (!effort) return '';
  return `<span class="effort-badge" title="思考档位（由模型名推断）">⚡${effort}?</span>`;
}

async function renderSubagents() {
  const url = '/api/subagents' + (showArchivedSubagents ? '?include_archived=true' : '');
  const r = await fetchJson(url);
  document.getElementById('subagent-count').textContent = r.count;
  document.getElementById('subagent-archived-count').textContent = r.archived_count || 0;
  const list = document.getElementById('subagent-list');

  if (!r.subagents.length) {
    setHtml(list, `<div class="muted">${showArchivedSubagents ? '暂无子 Agent' : '暂无活跃子 Agent（已封存 ' + (r.archived_count || 0) + ' 个）'}</div>`);
    if (currentSubagent) {
      currentSubagent = null;
      document.getElementById('subagent-detail').innerHTML = '<p class="muted">选左侧一项查看详情</p>';
    }
    return;
  }

  setHtml(list, r.subagents.map(s => {
    const isRunning = ['running', 'claimed', 'pending'].includes(s.status);
    return `
    <div class="subagent-item ${currentSubagent === s.task_id ? 'active' : ''} ${s.status === 'verifying' ? 'task-verify' : ''}" id="sa-${escapeHtml(s.task_id)}" data-tid="${escapeHtml(s.task_id)}">
      <div class="row1">
        <span class="title" title="${escapeHtml(s.payload || '')}">${escapeHtml(makeTaskTitle(s.from_model, s.created_at, s.payload))}</span>
        <span class="task-status ${escapeHtml(s.status || 'unknown')}">${escapeHtml(s.status || '-')}</span>
        ${s.status === 'verifying' ? '<span class="verify-badge">⚠ 待验收</span>' : ''}
      </div>
      <div class="row2">
        <span class="tid">${escapeHtml(s.task_id)}</span>
        <span class="muted">${escapeHtml(s.claimed_by || s.topic || '-')}</span>
        ${s.for_model ? `<span class="model-badge" title="调用的模型">🧠 ${escapeHtml(s.for_model)}</span>${effortBadge(s.for_model, s.reasoning_effort)}` : ''}
        ${isRunning
          ? `<span class="elapsed-live" data-start="${s.claimed_at || s.created_at || 0}" title="已运行时长（每秒刷新）">⏱ ${fmtElapsed(s.duration_sec)}</span>`
          : `<span class="muted">${fmtDuration(s.duration_sec)}</span>`}
        <span class="muted">${fmtTime(s.claimed_at)}</span>
        ${s.last_activity ? `<span class="muted" title="最后活动时间">⏱ ${fmtTime(s.last_activity)}</span>` : ''}
        ${s.possibly_stuck ? '<span class="stuck-badge">⚠ 疑似卡死</span>' : ''}
      </div>
      ${s.result_preview ? `<div class="payload">${escapeHtml(s.result_preview.slice(0, 120))}</div>` : ''}
    </div>
  `;
  }).join(''));

  // 如果当前有打开详情的 task，只轻量更新列表里的状态高亮，
  // 不要重刷详情面板：实时日志面板靠 SSE 自己推，重刷会打断滚动。
  if (currentSubagent) {
    const stillThere = r.subagents.find(s => s.task_id === currentSubagent);
    if (!stillThere) {
      currentSubagent = null;
      stopLiveStream();
    } else if (
      liveStreamTaskId === currentSubagent &&
      !['running', 'claimed', 'pending'].includes(stillThere.status)
    ) {
      // 盯着的任务刚结束：关掉 SSE（后端流是无限 tail，不自己结束），状态置为已结束
      // 注意先拿元素再 stopLiveStream（stop 会清 liveRoot）
      const statusEl = liveEl('live-status');
      stopLiveStream();
      if (statusEl) {
        statusEl.textContent = '■ 任务已结束';
        statusEl.style.color = 'var(--text-dim)';
        statusEl.classList.remove('reconnect');
      }
    }
  }
}

function renderSubTaskList(subTasks) {
  if (!subTasks || !subTasks.length) return '';
  return `
    <h3 style="font-size: 13px; margin: 16px 0 4px">🌿 Sub-tasks (${subTasks.length})</h3>
    <p class="muted" style="font-size: 12px; margin-bottom: 8px">主 agent 拆出来的子任务，点击可查看各自日志 / 会话。</p>
    <div class="subtask-list">
      ${subTasks.map(st => `
        <div class="subtask-item" data-tid="${escapeHtml(st.task_id)}">
          <div class="row1">
            <span class="tid">${escapeHtml(st.task_id)}</span>
            <span class="task-status ${escapeHtml(st.status)}">${escapeHtml(st.status)}</span>
            <span class="muted">${st.claimed_by || '-'}</span>
          </div>
          <div class="row2 muted">${escapeHtml((st.result_preview || '').slice(0, 120))}</div>
          <div class="row3">
            ${st.has_log ? '<span class="badge-small">log</span>' : ''}
            ${st.has_transcript ? '<span class="badge-small">transcript</span>' : ''}
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

async function showSubagentDetail(tid) {
  // 注意必须带 include_archived=true：列表勾了"显示已封存"后点的是封存任务，
  // 默认接口不含封存会找不到，详情直接显示"已消失"
  const s = (await fetchJson('/api/subagents?include_archived=true')).subagents.find(x => x.task_id === tid);
  if (!s) {
    stopLiveStream();  // 任务消失，别再挂着 SSE
    document.getElementById('subagent-detail').innerHTML = '<p class="muted">已消失</p>';
    return;
  }
  // 用 allSettled 容错：任何一个接口挂了（404/超时/解析失败）都不拖垮整个详情面板
  const settled = await Promise.allSettled([
    s.has_log ? fetchJson(`/api/subagents/${tid}/log?tail_kb=64`) : Promise.resolve(null),
    fetchJson(`/api/subagents/${tid}/history?limit=5`),
    fetchJson(`/api/subagents/${tid}/transcript`),
    fetchJson(`/api/tasks/${tid}/details`),
  ]);
  const val = i => (settled[i].status === 'fulfilled' ? settled[i].value : null);
  const log = val(0);
  const hist = val(1) || { ok: false };
  const transcript = val(2) || { ok: false, error: 'transcript 接口请求失败' };
  const taskDetails = val(3) || { ok: false };

  // 历史渲染辅助
  const renderHistSection = (title, items) => {
    if (!items || !items.length) return '';
    return `
      <h3 style="font-size: 13px; margin: 12px 0 4px">${title} (${items.length})</h3>
      <div class="hist-list">
        ${items.map(t => `
          <div class="hist-item" data-tid="${escapeHtml(t.task_id)}">
            <span class="task-status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
            <span class="tid">${escapeHtml(t.task_id)}</span>
            <span class="muted">${fmtTime(t.created_at)}</span>
            <span class="hist-payload">${escapeHtml((t.payload || '').slice(0, 80))}</span>
          </div>
        `).join('')}
      </div>
    `;
  };

  const isRunning = ['running', 'claimed', 'pending'].includes(s.status);

  // transcript summary（顶部提示有几条事件）
  const transcriptSummary = transcript.ok
    ? `<span class="muted">${transcript.event_count} 个事件</span>`
    : `<span class="muted" title="${escapeHtml(transcript.error || '')}">无 transcript</span>`;

  document.getElementById('subagent-detail').innerHTML = `
    <table class="detail-table">
      <tr><td>任务 ID</td><td>${escapeHtml(s.task_id)}</td></tr>
      <tr><td>状态</td><td>${escapeHtml(s.status)}</td></tr>
      <tr><td>主题</td><td>${escapeHtml(s.topic)}</td></tr>
      <tr><td>认领者</td><td>${escapeHtml(s.claimed_by || '-')}</td></tr>
      <tr><td>发起方</td><td>${escapeHtml(s.from_model || '-')}</td></tr>
      <tr><td>接收方</td><td>${escapeHtml(s.for_model || '-')} ${effortBadge(s.for_model, s.reasoning_effort)}</td></tr>
      <tr><td>创建时间</td><td>${fmtTime(s.created_at)}</td></tr>
      <tr><td>认领时间</td><td>${fmtTime(s.claimed_at)}</td></tr>
      <tr><td>完成时间</td><td>${fmtTime(s.completed_at)}</td></tr>
      <tr><td>耗时</td><td>${fmtDuration(s.duration_sec)}</td></tr>
      <tr><td>日志</td><td>${log ? `${fmtBytes(log.size)} (${escapeHtml(log.path)})` : '无'}</td></tr>
      <tr><td>会话</td><td>${transcriptSummary}</td></tr>
      ${s.error ? `<tr><td>错误</td><td style="color: var(--err)">${escapeHtml(s.error)}</td></tr>` : ''}
    </table>
    ${s.result_preview ? `<h3 style="font-size: 13px; margin: 8px 0 4px">结果（预览）</h3><pre class="log">${escapeHtml(s.result_preview)}</pre>` : ''}

    ${taskDetails.ok ? renderSubTaskList(taskDetails.sub_tasks) : ''}

    <div class="transcript-section" style="margin-top: 12px">
      <h3 style="font-size: 13px; margin: 12px 0 4px; display: flex; align-items: center; gap: 8px">
        📡 实时日志
        <span id="live-status" class="muted" style="font-size: 11px">--</span>
      </h3>
      <div class="live-controls">
        <label><input type="checkbox" id="live-autoscroll"> 跟随滚动</label>
        <button id="live-clear">清空</button>
        <span class="muted">上翻暂停跟随，滚回底部恢复</span>
      </div>
      ${isRunning ? `
        <div class="msg msg-running" style="margin-bottom: 8px">
          <div class="msg-role">⏳ 模型工作中<span class="working-dots"><span>.</span><span>.</span><span>.</span></span> <span class="ts">已运行 ${fmtDuration(s.duration_sec)}</span></div>
          <div class="msg-content muted">日志持续滚动说明还在工作；长时间无新内容可判断为卡死。</div>
        </div>
      ` : ''}
      <div id="live-log-container">
        <pre class="log" style="max-height: 300px; background: var(--inset); margin: 0">${log && log.content ? escapeHtml(log.content) : ''}</pre>
        <button id="live-jump-latest" class="live-jump-latest hidden">⤓ 已暂停跟随，点击回到底部</button>
      </div>
    </div>

    <div class="transcript-section">
      <h3 style="font-size: 13px; margin: 16px 0 4px; display: flex; align-items: center; gap: 8px">
        💬 会话内容（transcript）
        <button onclick="openSubagentTranscript('${escapeHtml(tid)}', 'transcript-container')" style="background: var(--accent-btn); color: #fff; border: none; padding: 2px 10px; cursor: pointer; font-size: 11px;">${transcript.ok ? '↻ 重新加载' : '加载'}</button>
      </h3>
      <div id="transcript-container">
        ${transcript.ok ? `
          <div class="transcript-header">
            <span class="muted">${transcript.event_count} 个事件</span>
            <span class="muted">${fmtBytes(transcript.size)} · ${escapeHtml(transcript.path)}</span>
            ${transcript.live ? '<span style="color:var(--ok-bright);font-size:11px">● 实时预览（任务进行中）</span>' : ''}
          </div>
          <div class="transcript-body">${renderTranscriptView(transcript.events, isRunning, s.duration_sec)}</div>
        ` : `<div class="muted">${escapeHtml(transcript.error || '无 transcript（这个 run 早于 v3 引入 transcript）')}</div>`}
      </div>
    </div>

    <div class="transcript-section">
      <h3 style="font-size: 13px; margin: 16px 0 4px">💬 手动干预</h3>
      <p class="muted" style="font-size: 12px; margin-bottom: 8px">
        给这个子 agent 发消息（保存到任务旁，续跑时带上）。任务卡住或中断后可用"继续"基于原任务重新 spawn。
      </p>
      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px">
        <input type="text" id="user-msg-input" placeholder="例如：继续 / 上一步错了，重新解析 ..." style="flex: 1; background: var(--inset); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; font-size: 13px;">
        <button id="user-msg-save" style="background: var(--border-dim); color: var(--text); border: 1px solid var(--border); padding: 6px 12px; cursor: pointer; font-size: 12px;">仅保存</button>
        <button id="user-msg-continue" style="background: var(--accent-btn); color: #fff; border: none; padding: 6px 12px; cursor: pointer; font-size: 12px;">继续任务</button>
      </div>
      <div id="user-msg-status" class="muted" style="font-size: 12px; margin-bottom: 4px"></div>
      <div id="user-msg-list" style="font-size: 12px"></div>
      ${isRunning ? `
        <div style="display: flex; gap: 8px; align-items: center; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border)">
          <button id="cancel-task-btn" class="danger-btn">⛔ 停止任务</button>
          <span id="cancel-status" class="muted" style="font-size: 12px">强制杀掉子进程（不可恢复）</span>
        </div>
      ` : ''}
    </div>

    ${log && log.content ? `
      <h3 style="font-size: 13px; margin: 16px 0 4px">📋 完整 stdout/stderr 日志（末尾 ${log.tail_kb}KB）</h3>
      <pre class="log">${escapeHtml(log.content)}</pre>
    ` : ''}

    <h3 style="font-size: 13px; margin: 16px 0 4px; border-top: 1px solid var(--border); padding-top: 12px">会话历史</h3>
    <p class="muted" style="font-size: 12px; margin-bottom: 8px">同一 worker / 同一 model / 同一发起方 派过的其他任务（点开看详情）</p>
    ${renderHistSection('同 worker（' + escapeHtml(s.claimed_by || '-') + '）跑过', (hist.history || {}).by_worker)}
    ${renderHistSection('同 model（' + escapeHtml(s.for_model || '-') + '）接的任务', (hist.history || {}).by_for_model)}
    ${renderHistSection('同发起方（' + escapeHtml(s.from_model || '-') + '）派过', (hist.history || {}).by_from_model)}
    ${renderHistSection('同主题（' + escapeHtml(s.topic || '-') + '）的任务', (hist.history || {}).by_topic)}
  `;

  // sub-task 项可点 → 切到那个 sub-task 详情
  document.getElementById('subagent-detail').querySelectorAll('.subtask-item').forEach(el => {
    el.addEventListener('click', () => {
      const newTid = el.dataset.tid;
      currentSubagent = newTid;
      document.querySelectorAll('.subagent-item').forEach(e => e.classList.remove('active'));
      const newLi = document.querySelector(`.subagent-item[data-tid="${CSS.escape(newTid)}"]`);
      if (newLi) {
        newLi.classList.add('active');
        newLi.scrollIntoView({ block: 'nearest' });
      }
      showSubagentDetail(newTid);
    });
  });

  // 历史项可点 → 切到那个 task 详情
  document.getElementById('subagent-detail').querySelectorAll('.hist-item').forEach(el => {
    el.addEventListener('click', () => {
      const newTid = el.dataset.tid;
      currentSubagent = newTid;
      // 高亮新选中的
      document.querySelectorAll('.subagent-item').forEach(e => e.classList.remove('active'));
      const newLi = document.querySelector(`.subagent-item[data-tid="${CSS.escape(newTid)}"]`);
      if (newLi) {
        newLi.classList.add('active');
        newLi.scrollIntoView({ block: 'nearest' });
      }
      showSubagentDetail(newTid);
    });
  });

  // 实时日志：绑定控制条（root 限定到这个详情面板，避免和任务 tab 的同名 id 冲突）；
  // running 任务自动接 SSE 跟随，已结束的只留静态日志
  bindLiveLogControls(document.getElementById('subagent-detail'));
  startLiveStream(tid, isRunning);

  // 用户手动干预事件绑定
  const msgInput = document.getElementById('user-msg-input');
  const msgSaveBtn = document.getElementById('user-msg-save');
  const msgContinueBtn = document.getElementById('user-msg-continue');
  const msgStatus = document.getElementById('user-msg-status');
  const msgList = document.getElementById('user-msg-list');

  async function loadUserMessages() {
    try {
      const r = await fetchJson(`/api/subagents/${tid}/messages`);
      if (!r.ok || !r.messages.length) {
        msgList.innerHTML = '<div class="muted">暂无干预消息</div>';
        return;
      }
      msgList.innerHTML = r.messages.map(m => `
        <div class="user-msg-item">
          <span class="muted">${fmtTime(m.ts)} · ${escapeHtml(m.from || '用户')}</span>
          <div>${escapeHtml(m.message || '')}</div>
        </div>
      `).join('');
    } catch (e) {
      msgList.innerHTML = `<div class="muted" style="color:var(--err)">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function saveUserMessage() {
    const msg = msgInput.value.trim();
    if (!msg) {
      msgStatus.textContent = '消息不能为空';
      msgStatus.style.color = 'var(--err)';
      return;
    }
    if (msgSaveBtn.disabled) return;  // 发送中，防重复点
    msgSaveBtn.disabled = true;
    msgStatus.textContent = '发送中...';
    msgStatus.style.color = 'var(--text-dim)';
    try {
      const r = await fetch(`/api/subagents/${tid}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      const data = await r.json();
      if (data.ok) {
        msgStatus.textContent = '✓ 已发送并保存（续跑时会带上）';
        msgStatus.style.color = 'var(--ok)';
        msgInput.value = '';
        await loadUserMessages();
      } else {
        msgStatus.textContent = '发送失败: ' + (data.error || '未知');
        msgStatus.style.color = 'var(--err)';
      }
    } catch (e) {
      msgStatus.textContent = '异常: ' + e.message;
      msgStatus.style.color = 'var(--err)';
    } finally {
      msgSaveBtn.disabled = false;
    }
  }

  async function continueTask() {
    const msg = msgInput.value.trim();
    if (!confirm(`将基于原任务 spawn 一个新的续跑子 agent${msg ? '，并带上消息：" ' + msg.slice(0, 50) + '"' : ''}。继续？`)) {
      return;
    }
    msgContinueBtn.disabled = true;
    msgStatus.textContent = '续跑派发中（spawn 新子 agent）...';
    msgStatus.style.color = 'var(--text-dim)';
    try {
      const r = await fetch(`/api/subagents/${tid}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      const data = await r.json();
      if (data.ok) {
        const note = data.note ? `（${data.note}）` : '';
        msgStatus.textContent = `✓ 已派发续跑任务 ${data.new_task_id}，等待 agent 启动… ${note}（即将自动跳转）`;
        msgStatus.style.color = 'var(--ok)';
        msgInput.value = '';
        // 跳转到新任务
        currentSubagent = data.new_task_id;
        await renderSubagents();
        setTimeout(() => {
          document.querySelectorAll('.subagent-item').forEach(e => e.classList.remove('active'));
          const li = document.querySelector(`.subagent-item[data-tid="${CSS.escape(data.new_task_id)}"]`);
          if (li) {
            li.classList.add('active');
            li.scrollIntoView({ block: 'nearest' });
          }
          showSubagentDetail(data.new_task_id);
        }, 500);
      } else {
        msgStatus.textContent = '续跑失败: ' + (data.error || '未知');
        msgStatus.style.color = 'var(--err)';
      }
    } catch (e) {
      msgStatus.textContent = '异常: ' + e.message;
      msgStatus.style.color = 'var(--err)';
    } finally {
      msgContinueBtn.disabled = false;
    }
  }

  // 停止按钮（仅 running 任务渲染了这个按钮）：confirm + 立即刷列表
  const cancelBtn = document.getElementById('cancel-task-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      if (!confirm(`确定要停止任务 ${tid}？\n子进程会被强制杀掉，未完成的进度会丢，此操作不可恢复。`)) {
        return;
      }
      const cStatus = document.getElementById('cancel-status');
      cancelBtn.disabled = true;
      cStatus.textContent = '停止中...';
      cStatus.style.color = 'var(--text-dim)';
      try {
        const r = await fetch(`/api/subagents/${tid}/cancel`, { method: 'POST' });
        const data = await r.json();
        if (data.ok) {
          cStatus.textContent = '✓ 已停止（子进程已杀）';
          cStatus.style.color = 'var(--ok)';
          const lStatus = liveEl('live-status');  // 先拿元素再 stop（stop 会清 liveRoot）
          stopLiveStream();
          if (lStatus) {
            lStatus.textContent = '■ 已停止';
            lStatus.style.color = 'var(--text-dim)';
            lStatus.classList.remove('reconnect');
          }
          // 列表 2s 轮询也会更新，这里主动刷一次让状态立即反映
          await renderSubagents();
        } else {
          cStatus.textContent = '停止失败: ' + (data.error || '未知');
          cStatus.style.color = 'var(--err)';
          cancelBtn.disabled = false;
        }
      } catch (e) {
        cStatus.textContent = '异常: ' + e.message;
        cStatus.style.color = 'var(--err)';
        cancelBtn.disabled = false;
      }
    });
  }

  if (msgSaveBtn) msgSaveBtn.addEventListener('click', saveUserMessage);
  if (msgContinueBtn) msgContinueBtn.addEventListener('click', continueTask);
  if (msgInput) {
    msgInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') saveUserMessage();
    });
  }
  await loadUserMessages();
}

// ---------- Cluster ----------

async function renderCluster() {
  const [c, crewsResp] = await Promise.all([
    fetchJson('/api/cluster'),
    fetchJson('/api/crews'),
  ]);
  const el = document.getElementById('cluster-detail');
  const crews = (crewsResp && crewsResp.crews) || [];

  if (!c.enabled) {
    setHtml(el, `
      <div class="card">
        <h2>不预挂工人</h2>
        <p class="cluster-banner">${escapeHtml(c.note || '模型由调用方指定，不写死任何厂商。')}</p>
        <p class="muted">任务组/派活时自己选已连接的 runtime 和 model。mimo 只是一种可接入的模型，不是集群本体。交流和监督在「监督」或下面的关系图。</p>
      </div>
      ${await renderClusterCrewCard(crews)}
    `);
    bindCrewStage(el);
    return;
  }

  const pools = c.pools || [];
  const totalSize = pools.reduce((sum, p) => sum + (p.size || 0), 0);
  const totalPending = (c.totals && c.totals.pending) || 0;
  const totalClaimed = (c.totals && c.totals.claimed) || 0;
  const totalDone = (c.totals && c.totals.done) || 0;
  const totalFailed = (c.totals && c.totals.failed) || 0;

  const poolsHtml = pools.map(p => {
    const slots = [];
    const n = Math.max(1, p.size || 1);
    for (let i = 0; i < n; i++) {
      slots.push(`<div class="cluster-slot"><div class="nm">工人 ${i + 1}</div><div class="muted">${escapeHtml(p.runtime)} / ${escapeHtml(p.model)}</div></div>`);
    }
    return `
    <div class="card" style="margin-bottom: 12px">
      <h2>
        <span class="pool-name">${escapeHtml(p.name)}</span>
        <span class="pool-tag" style="font-size: 12px; color: var(--gray2); margin-left: 8px">
          ${p.enabled ? '✓ 启用' : '× 禁用'} · ${n} 工人 · ${escapeHtml(p.topic)}
        </span>
      </h2>
      <div class="cluster-flow">
        <div class="cluster-col">
          <h3>排队 ${p.queue.pending}</h3>
          <p class="muted">topic 里还没人领的活</p>
        </div>
        <div class="cluster-col">
          <h3>工人认领 ${p.queue.claimed}</h3>
          ${slots.join('')}
        </div>
        <div class="cluster-col">
          <h3>出活</h3>
          <div class="cluster-slot"><div class="nm" style="color:var(--ok)">完成 ${p.queue.done}</div></div>
          <div class="cluster-slot"><div class="nm" style="color:var(--err)">失败 ${p.queue.failed}</div></div>
        </div>
      </div>
      <table class="detail-table">
        <tr><td>工作目录</td><td>${escapeHtml(p.workdir)}</td></tr>
        <tr><td>超时</td><td>${p.task_timeout_sec}秒 · 每工人并发 ${p.concurrency_per_worker}</td></tr>
      </table>
    </div>`;
  }).join('');

  setHtml(el, `
    <div class="card">
      <h2>工人池（${pools.length} 组，共 ${totalSize} 个工人）</h2>
      <p class="cluster-banner">可选的本机队列。每组工人的 runtime/model 是你配的，不是产品内置。各领各的活，<b>互相不说话</b>。交流在任务组黑板。</p>
      <div class="cluster-stat">
        <div class="stat"><div class="num" style="color: var(--warn)">${totalPending}</div><div class="label">待处理</div></div>
        <div class="stat"><div class="num" style="color: var(--accent-btn)">${totalClaimed}</div><div class="label">认领中</div></div>
        <div class="stat"><div class="num" style="color: var(--ok)">${totalDone}</div><div class="label">已完成</div></div>
        <div class="stat"><div class="num" style="color: var(--err)">${totalFailed}</div><div class="label">失败</div></div>
      </div>
    </div>
    ${poolsHtml}
    ${await renderClusterCrewCard(crews)}
    <div class="card" style="margin-top: 12px">
      <h2>说明</h2>
      <p class="muted">${c.note || ''}</p>
    </div>
  `);
  bindCrewStage(el);
}

async function renderClusterCrewCard(crews) {
  if (!crews.length) {
    return `<div class="card" style="margin-top:12px">
      <h2>任务组（交流 + 监督）</h2>
      <p class="muted">还没有任务组。到「监督」建一个，成员会经黑板互相留言，监督者解卡/纠偏会出现在关系图上。</p>
    </div>`;
  }
  if (!currentCrewId || !crews.some(c => c.crew_id === currentCrewId)) {
    currentCrewId = crews[0].crew_id;
  }
  const d = await fetchJson('/api/crews/' + encodeURIComponent(currentCrewId));
  if (!d.ok) {
    return `<div class="card" style="margin-top:12px"><h2>任务组</h2><p class="muted">${escapeHtml(d.error || '')}</p></div>`;
  }
  const picker = crews.map(c => {
    const sel = c.crew_id === currentCrewId ? ' selected' : '';
    return `<option value="${escapeHtml(c.crew_id)}"${sel}>${escapeHtml((c.goal || '').slice(0, 60))}</option>`;
  }).join('');
  return `<div class="card" style="margin-top:12px">
    <h2>任务组（交流 + 监督）</h2>
    <label class="form-label">当前任务组</label>
    <select id="cluster-crew-pick" class="form-input cluster-crew-pick" style="width:100%;margin:6px 0 10px">${picker}</select>
    ${crewRelationHtml(d.crew, { includeForm: false, pfx: 'cl' })}
  </div>`;
}

function bindCrewStage(root) {
  const stage = root.querySelector('.crew-stage');
  if (stage) bindCrewRelation(stage, currentCrewId);
}

// ---------- Usage ----------

async function renderUsage(force = false) {
  // 30s 节流：/api/usage 要聚合全部 transcript，2s 轮询每次都拉太贵
  const now = Date.now();
  if (!force && usageLastFetch && now - usageLastFetch < 30000) return;
  usageLastFetch = now;

  const u = await fetchJson('/api/usage');
  const total = u.total || { tokens: {}, cost: 0, tasks: 0 };
  // by_day 的 key 是服务端本地日期；dashboard 跟 hub 同机，直接用浏览器本地日期对齐
  const d = new Date();
  const todayKey = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const today = (u.by_day || {})[todayKey] || { tokens: {}, cost: 0, tasks: 0 };

  setHtml(document.getElementById('usage-stats'), `
    <div class="cluster-stat" style="margin-bottom: 8px">
      <div class="stat"><div class="num">${fmtTokens((today.tokens || {}).total)}</div><div class="label">今日 token（${today.tasks || 0} 任务）</div></div>
      <div class="stat"><div class="num" style="color: var(--warn)">${fmtCost(today.cost)}</div><div class="label">今日 cost</div></div>
      <div class="stat"><div class="num">${fmtTokens((total.tokens || {}).total)}</div><div class="label">累计 token（${total.tasks || 0} 任务）</div></div>
      <div class="stat"><div class="num" style="color: var(--warn)">${fmtCost(total.cost)}</div><div class="label">累计 cost</div></div>
    </div>
    <p class="muted" style="font-size: 11px">
      每 30s 自动刷新 · 更新于 ${fmtTime(now / 1000)} ·
      有 usage 数据的任务 ${u.tasks_with_usage || 0} / ${u.tasks_total || 0}
      （${u.tasks_without_usage || 0} 个任务的 runtime 不上报 token，不计入）
    </p>
  `);

  // 按模型排行：横向条形（纯 CSS）
  const rows = u.by_model || [];
  const maxTok = Math.max(1, ...rows.map(r => (r.tokens || {}).total || 0));
  setHtml(document.getElementById('usage-by-model'), rows.map(r => {
    const tk = r.tokens || {};
    const cache = tk.cache || {};
    const tok = tk.total || 0;
    const pct = Math.max(tok > 0 ? 1 : 0, (tok / maxTok) * 100);
    const tip = `input=${fmtTokens(tk.input)} output=${fmtTokens(tk.output)} reasoning=${fmtTokens(tk.reasoning)} cache_read=${fmtTokens(cache.read)}`;
    return `
      <div class="usage-bar-row" title="${escapeHtml(tip)}">
        <span class="usage-name" title="${escapeHtml(r.model)}">${escapeHtml(r.model)}</span>
        <div class="usage-bar-track"><div class="usage-bar-fill" style="width: ${pct.toFixed(1)}%"></div></div>
        <span class="usage-val">${fmtTokens(tok)}</span>
        <span class="usage-cost">${fmtCost(r.cost)}</span>
        <span class="muted">${r.tasks} 任务</span>
      </div>
    `;
  }).join('') || '<div class="muted">无数据</div>');

  // 按天：最近 14 天，绿色小条形
  const days = Object.entries(u.by_day || {})
    .sort((a, b) => b[0].localeCompare(a[0]))
    .slice(0, 14);
  const maxDay = Math.max(1, ...days.map(([, v]) => (v.tokens || {}).total || 0));
  setHtml(document.getElementById('usage-by-day'), days.map(([day, v]) => {
    const tok = (v.tokens || {}).total || 0;
    const pct = Math.max(tok > 0 ? 1 : 0, (tok / maxDay) * 100);
    return `
      <div class="usage-bar-row">
        <span class="usage-name usage-day">${escapeHtml(day)}</span>
        <div class="usage-bar-track"><div class="usage-bar-fill day" style="width: ${pct.toFixed(1)}%"></div></div>
        <span class="usage-val">${fmtTokens(tok)}</span>
        <span class="usage-cost">${fmtCost(v.cost)}</span>
        <span class="muted">${v.tasks} 任务</span>
      </div>
    `;
  }).join('') || '<div class="muted">无数据</div>');
}

// ---------- 连接 tab ----------

let connectLastFetch = 0;
let connectBusy = false;

async function renderConnect(force = false) {
  if (connectBusy && !force) return;
  const now = Date.now();
  if (!force && connectLastFetch && now - connectLastFetch < 8000) return;
  if (connectBusy) return;
  connectLastFetch = now;
  const d = await fetchJson('/api/connections');
  if (!d || !d.ok) return;

  const rtEl = document.getElementById('connect-runtimes');
  setHtml(rtEl, (d.runtimes || []).map(r => {
    const st = r.connected ? '已连接' : (r.ready ? '已就绪，未连接' : (r.installed ? '已安装，需登录' : '未安装'));
    const dot = r.connected ? 'green' : (r.ready ? 'gray' : 'red');
    const loginBtn = r.has_login_command && !r.connected
      ? `<button class="btn conn-login" data-name="${escapeHtml(r.name)}">打开登录窗口</button>`
      : '';
    const action = r.connected
      ? `<button class="btn danger-btn conn-rt-off" data-name="${escapeHtml(r.name)}">断开</button>`
      : `<button class="btn-primary conn-rt-on" data-name="${escapeHtml(r.name)}" ${r.installed ? '' : 'disabled'}>连接</button>`;
    return `<div class="connect-row" id="conn-rt-${escapeHtml(r.name)}">
      <span class="dot ${dot}"></span>
      <div class="connect-main">
        <div><strong>${escapeHtml(r.name)}</strong> <span class="muted">${escapeHtml(r.binary || '')}</span></div>
        <div class="muted" style="font-size:11px">${escapeHtml(st)} · ${escapeHtml(r.login_hint || '')}</div>
      </div>
      <div class="connect-actions">${loginBtn}${action}</div>
    </div>`;
  }).join('') || '<div class="muted">无 runtime</div>');

  const toolEl = document.getElementById('connect-tools');
  setHtml(toolEl, (d.tools || []).map(t => {
    const connected = t.connected;
    const isHedge = t.name === 'hedge';
    return `<div class="connect-tool" id="conn-tool-${escapeHtml(t.name)}">
      <div class="connect-row" style="border-bottom:none">
        <span class="dot ${connected ? 'green' : 'gray'}"></span>
        <div class="connect-main">
          <div><strong>${escapeHtml(t.name)}</strong> ${connected ? '<span class="muted">已连接</span>' : ''}</div>
          <div class="muted" style="font-size:11px">${escapeHtml(t.hint || '')}</div>
        </div>
      </div>
      <div class="connect-form">
        ${isHedge ? `<label class="form-label">Base URL</label>
          <input class="form-input conn-base" id="conn-base-${escapeHtml(t.name)}" data-name="${escapeHtml(t.name)}" value="${escapeHtml(t.base_url || '')}" placeholder="https://example.com/v1">` : ''}
        <label class="form-label">API Key（不会显示已保存的值）</label>
        <input class="form-input conn-key" id="conn-key-${escapeHtml(t.name)}" data-name="${escapeHtml(t.name)}" type="password" autocomplete="off" placeholder="${t.has_api_key ? '已保存，留空则保持' : '可选'}">
        ${t.name === 'mmx' ? `<label class="form-label">Region</label>
          <input class="form-input conn-region" id="conn-region-${escapeHtml(t.name)}" data-name="${escapeHtml(t.name)}" value="${escapeHtml(t.region || '')}" placeholder="cn 或 global">` : ''}
        <div class="connect-actions" style="margin-top:8px">
          <button class="btn-primary conn-tool-on" data-name="${escapeHtml(t.name)}">连接</button>
          ${connected ? `<button class="btn danger-btn conn-tool-off" data-name="${escapeHtml(t.name)}">断开</button>` : ''}
        </div>
        <div class="muted conn-msg" data-name="${escapeHtml(t.name)}" style="font-size:11px;margin-top:4px"></div>
      </div>
    </div>`;
  }).join(''));
}

document.getElementById('tab-connect').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  const name = btn.dataset.name;
  if (!name) return;
  if (connectBusy) return;
  connectBusy = true;
  btn.disabled = true;
  const oldLabel = btn.textContent;
  btn.textContent = '处理中…';
  try {
    if (btn.classList.contains('conn-rt-on')) {
      const r = await fetch(`/api/connections/runtimes/${encodeURIComponent(name)}/connect`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const d = await r.json();
      if (!d.ok) alert(d.error || '连接失败');
    } else if (btn.classList.contains('conn-login')) {
      const r = await fetch(`/api/connections/runtimes/${encodeURIComponent(name)}/connect`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ open_login: true }),
      });
      const d = await r.json();
      if (!d.ok && d.error) alert(d.error);
    } else if (btn.classList.contains('conn-rt-off')) {
      await fetch(`/api/connections/runtimes/${encodeURIComponent(name)}/disconnect`, { method: 'POST' });
    } else if (btn.classList.contains('conn-tool-on')) {
      const box = btn.closest('.connect-tool');
      const body = {
        base_url: (box.querySelector('.conn-base') || {}).value || '',
        api_key: (box.querySelector('.conn-key') || {}).value || '',
        region: (box.querySelector('.conn-region') || {}).value || '',
      };
      const r = await fetch(`/api/connections/tools/${encodeURIComponent(name)}/connect`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      const msg = box.querySelector('.conn-msg');
      if (msg) msg.textContent = d.ok ? '已连接' : (d.error || '失败');
      if (!d.ok) alert(d.error || '连接失败');
    } else if (btn.classList.contains('conn-tool-off')) {
      await fetch(`/api/connections/tools/${encodeURIComponent(name)}/disconnect`, { method: 'POST' });
    } else {
      return;
    }
    connectLastFetch = 0;
    await renderConnect(true);
  } catch (e) {
    btn.textContent = oldLabel;
    btn.disabled = false;
    alert('异常: ' + e.message);
  } finally {
    connectBusy = false;
  }
});

// ---------- Dispatch tab ----------

// 通用快速派活
document.getElementById('quick-submit').addEventListener('click', submitQuickDispatch);
document.getElementById('quick-runtime').addEventListener('change', () => {
  refreshQuickModelOptions();
});
document.getElementById('wall-runtime-filter').addEventListener('change', () => {
  renderModelWall();
});

async function submitQuickDispatch() {
  const runtime = document.getElementById('quick-runtime').value;
  const model = document.getElementById('quick-model').value;
  const topic = document.getElementById('quick-topic').value || 'cluster.work';
  const payload = document.getElementById('quick-payload').value;
  const statusEl = document.getElementById('quick-status');
  if (!payload.trim()) {
    statusEl.textContent = '任务内容不能为空';
    statusEl.style.color = 'var(--err)';
    return;
  }
  statusEl.textContent = '派发中...';
  statusEl.style.color = 'var(--text-dim)';
  const btn = document.getElementById('quick-submit');
  btn.disabled = true;
  try {
    const body = { payload, topic, from_model: '用户' };
    if (runtime) body.runtime = runtime;
    if (model) body.model = model;
    // 长任务模式：timeout 3600s
    if (document.getElementById('quick-long-task').checked) {
      body.timeout_sec = 3600;
    }
    // 收集验收配置
    const criteriaText = document.getElementById('quick-criteria').value;
    if (criteriaText.trim()) {
      const criteria = criteriaText.split('\n').map(s => s.trim()).filter(s => s);
      if (criteria.length) {
        body.acceptance_criteria = criteria;
        body.acceptance_verifier = document.getElementById('quick-verifier').value || '';
        body.acceptance_max_iterations = parseInt(document.getElementById('quick-max-iter').value || '2', 10);
        body.acceptance_auto_retry = document.getElementById('quick-auto-retry').checked;
      }
    }
    const r = await fetch('/api/cluster/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (data.ok) {
      addDispatchedTask(data, payload);
      const tag = data.has_acceptance ? '（带验收）' : '';
      statusEl.textContent = `已派发 ${data.task_id} ${tag}`;
      statusEl.style.color = 'var(--ok)';
      document.getElementById('quick-payload').value = '';
      renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = 'var(--err)';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = 'var(--err)';
  } finally {
    btn.disabled = false;
  }
}

async function refreshQuickModelOptions() {
  const runtime = document.getElementById('quick-runtime').value;
  const modelSel = document.getElementById('quick-model');
  if (!modelsCache) {
    modelsCache = await fetchJson('/api/models/all');
  }
  // 只列 selected runtime 的 models
  const models = runtime
    ? (modelsCache.by_runtime[runtime] || []).map(m => m.name)
    : [];
  // 保留当前选中的 model（如果新选项里还有的话）—— 避免 rebuild 期间用户的 model 选择被重置
  const prev = modelSel.value;
  modelSel.innerHTML = '<option value="">（任意）</option>' +
    models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
  if (prev && Array.from(modelSel.options).some(o => o.value === prev)) {
    modelSel.value = prev;
  }
}

function addDispatchedTask(data, payload) {
  dispatchedTasks.unshift({
    task_id: data.task_id,
    topic: data.topic,
    payload: payload,
    submitted_at: data.created_at,
    status: 'pending',
    result: null,
    error: null,
    claimed_by: null,
    for_model: data.for_model || '',
    duration_sec: 0,
  });
}

// 卡片墙
async function renderModelWall() {
  if (!modelsCache) {
    modelsCache = await fetchJson('/api/models/all');
  }
  const filter = document.getElementById('wall-runtime-filter').value;
  const el = document.getElementById('model-wall');
  const allModels = modelsCache.all;

  const visible = filter ? allModels.filter(m => m.runtime === filter) : allModels;
  // 按 runtime 分组
  const groups = {};
  for (const m of visible) {
    if (!groups[m.runtime]) groups[m.runtime] = [];
    groups[m.runtime].push(m);
  }

  // 也更新 wall-runtime-filter 选项（只一次）
  if (!document.getElementById('wall-runtime-filter').dataset.ready) {
    const filterSel = document.getElementById('wall-runtime-filter');
    for (const r of modelsCache.runtimes) {
      const opt = document.createElement('option');
      opt.value = r.name;
      opt.textContent = `${r.name} (${r.binary})`;
      filterSel.appendChild(opt);
    }
    filterSel.dataset.ready = '1';
  }
  // 同样更新 quick-runtime 选项
  if (!document.getElementById('quick-runtime').dataset.ready) {
    const quickSel = document.getElementById('quick-runtime');
    for (const r of modelsCache.runtimes) {
      const opt = document.createElement('option');
      opt.value = r.name;
      opt.textContent = `${r.name} (${r.binary})`;
      quickSel.appendChild(opt);
    }
    quickSel.dataset.ready = '1';
  }

  const html = Object.entries(groups).map(([runtime, models]) => `
    <div class="wall-group">
      <h3 class="wall-group-title">
        <span class="runtime-tag">${escapeHtml(runtime)}</span>
        <span class="muted">${models.length} 个模型</span>
      </h3>
      <div class="wall-grid">
        ${models.map(m => renderModelCard(m)).join('')}
      </div>
    </div>
  `).join('');

  setHtml(el, html || '<div class="muted">没有可用模型</div>');
}

function renderModelCard(m) {
  const id = `mc-${m.runtime}-${m.name}`.replace(/[^a-zA-Z0-9]/g, '_');
  const dotClass = m.available ? 'green' : 'red';
  return `
    <div class="model-card" id="${escapeHtml(id)}" data-runtime="${escapeHtml(m.runtime)}" data-model="${escapeHtml(m.name)}" data-available="${m.available}">
      <div class="card-header">
        <span class="dot ${dotClass}"></span>
        <span class="card-model-name" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</span>
      </div>
      <div class="card-runtime">${escapeHtml(m.runtime)}</div>
      <textarea class="card-payload" id="${escapeHtml(id)}-payload" placeholder="任务内容..."></textarea>
      <div class="card-actions">
        <input type="text" class="card-topic" id="${escapeHtml(id)}-topic" value="${m.runtime}.work" title="topic（默认 runtime.work）">
        <button class="card-submit" ${m.available ? '' : 'disabled'}>派发</button>
      </div>
      <div class="card-status"></div>
    </div>
  `;
}

async function submitFromCard(card) {
  const runtime = card.dataset.runtime;
  const model = card.dataset.model;
  const payload = card.querySelector('.card-payload').value;
  const topic = card.querySelector('.card-topic').value || `${runtime}.work`;
  const statusEl = card.querySelector('.card-status');

  if (!payload.trim()) {
    statusEl.textContent = '请先填任务内容';
    statusEl.style.color = 'var(--err)';
    return;
  }
  const btn = card.querySelector('.card-submit');
  btn.disabled = true;
  statusEl.textContent = '派发中...';
  statusEl.style.color = 'var(--text-dim)';

  try {
    const r = await fetch('/api/cluster/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload, topic, from_model: '用户', runtime, model }),
    });
    const data = await r.json();
    if (data.ok) {
      addDispatchedTask(data, payload);
      statusEl.textContent = `已派发 ${data.task_id}`;
      statusEl.style.color = 'var(--ok)';
      card.querySelector('.card-payload').value = '';
      // 跳到 dispatch 列表
      renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = 'var(--err)';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = 'var(--err)';
  } finally {
    btn.disabled = false;
  }
}

async function renderDispatch() {
  // form 部分（白名单、quick dispatch、模型卡片墙）只在第一次进 tab 或显式重置时渲染，
  // 避免 2 秒轮询 innerHTML 重写把用户正在编辑的内容清掉。
  if (!dispatchFormRendered) {
    await renderDispatchForm();
    dispatchFormRendered = true;
  }
  // 派发列表每次轮询都更新（这是 live 数据）
  await renderDispatchList();
}

async function renderDispatchForm() {
  if (!modelsCache) {
    modelsCache = await fetchJson('/api/models/all');
  }
  await renderModelWall();
  await refreshQuickModelOptions();
  await loadPinnedModels();
}

function resetDispatchForm() {
  dispatchFormRendered = false;
  // 清掉 ready 标记让 runtime/model filter 重新拉选项
  const wallFilter = document.getElementById('wall-runtime-filter');
  if (wallFilter) delete wallFilter.dataset.ready;
  const quickRt = document.getElementById('quick-runtime');
  if (quickRt) delete quickRt.dataset.ready;
  modelsCache = null;
  renderDispatch();
}

async function renderDispatchList() {
  // 拉最新状态
  await Promise.all(dispatchedTasks.map(async (t) => {
    if (t.status === 'pending' || t.status === 'claimed') {
      try {
        const r = await fetchJson(`/api/cluster/task/${t.task_id}`);
        if (r.ok) {
          t.status = r.task.status;
          t.result = r.task.result;
          t.error = r.task.error;
          t.claimed_at = r.task.claimed_at;
          t.completed_at = r.task.completed_at;
          t.claimed_by = r.task.claimed_by;
          if (t.claimed_at && t.completed_at) {
            t.duration_sec = t.completed_at - t.claimed_at;
          } else if (t.claimed_at) {
            t.duration_sec = (t.completed_at || Date.now() / 1000) - t.claimed_at;
          }
        }
      } catch (e) {
        // ignore
      }
    }
  }));

  const el = document.getElementById('dispatch-list');
  if (!dispatchedTasks.length) {
    setHtml(el, '<div class="muted">还没派过任务。在上方选 model 填任务内容点"派发"。</div>');
    return;
  }

  setHtml(el, dispatchedTasks.map(t => {
    const resultClass = t.status === 'done' ? '' : (t.status === 'failed' ? 'failed' : 'pending');
    const resultText = t.status === 'done'
      ? t.result
      : t.status === 'failed'
        ? `错误: ${t.error || '(unknown)'}`
        : (t.status === 'claimed' ? `认领中 by ${t.claimed_by || '?'}...` : '等待 worker 认领...');
    return `
      <div class="dispatch-item" id="disp-${escapeHtml(t.task_id)}">
        <div class="row1">
          <span class="tid" data-tid="${escapeHtml(t.task_id)}">${escapeHtml(t.task_id)}</span>
          <span class="task-status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
          <span class="muted">${escapeHtml(t.topic)}</span>
          ${t.for_model ? `<span class="muted">→ ${escapeHtml(t.for_model)}</span>` : ''}
          <span class="duration">${t.duration_sec ? fmtDuration(t.duration_sec) : ''}</span>
        </div>
        <div class="payload">${escapeHtml(t.payload)}</div>
        <div class="result ${resultClass}">${escapeHtml(resultText || '')}</div>
      </div>
    `;
  }).join(''));
}

function openSubagentFromDispatch(tid) {
  currentSubagent = tid;
  currentTab = 'subagents';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
  document.querySelector('.tab-btn[data-tab="subagents"]').classList.add('active');
  document.getElementById('tab-subagents').classList.remove('hidden');
  renderSubagents();
  setTimeout(() => {
    document.querySelectorAll('.subagent-item').forEach(e => e.classList.remove('active'));
    const li = document.querySelector(`.subagent-item[data-tid="${CSS.escape(tid)}"]`);
    if (li) {
      li.classList.add('active');
      li.scrollIntoView({ block: 'nearest' });
    }
    showSubagentDetail(tid);
  }, 500);
}

let currentTaskDetail = null;

document.getElementById('task-refresh').addEventListener('click', renderTasks);
document.getElementById('task-topic-filter').addEventListener('input', () => {
  currentTaskDetail = null;
  document.getElementById('task-detail').innerHTML = '';
  renderTasks();
});

async function renderTasks() {
  const topic = document.getElementById('task-topic-filter').value;
  const params = new URLSearchParams();
  if (topic) params.set('topic', topic);
  if (showArchivedTasks) params.set('include_archived', 'true');
  const url = '/api/tasks' + (params.toString() ? '?' + params.toString() : '');
  const r = await fetchJson(url);
  document.getElementById('task-archived-count').textContent = r.archived_count || 0;
  const el = document.getElementById('task-list');
  if (!r.tasks.length) {
    setHtml(el, `<div class="muted">${showArchivedTasks ? '无任务' : '无活跃任务（已封存 ' + (r.archived_count || 0) + ' 个）'}</div>`);
    return;
  }
  // 详情面板里盯着的任务刚结束：关 SSE + 状态置为已结束（同 renderSubagents 的处理）
  if (currentTaskDetail && liveStreamTaskId === currentTaskDetail) {
    const cur = r.tasks.find(t => t.task_id === currentTaskDetail);
    if (cur && !['running', 'claimed', 'pending'].includes(cur.status)) {
      const statusEl = liveEl('live-status');  // 先拿元素再 stop（stop 会清 liveRoot）
      stopLiveStream();
      if (statusEl) {
        statusEl.textContent = '■ 任务已结束';
        statusEl.style.color = 'var(--text-dim)';
        statusEl.classList.remove('reconnect');
      }
    }
  }
  setHtml(el, r.tasks.map(t => {
    const isActive = currentTaskDetail === t.task_id;
    const verifyingBadge = t.status === 'verifying'
      ? '<span class="verify-badge">⚠ 待验收</span>'
      : '';
    const verifyCount = (t.verify_history && t.verify_history.length) || 0;
    return `
      <div class="task-item ${isActive ? 'active' : ''} ${t.status === 'verifying' ? 'task-verify' : ''}" id="tk-${escapeHtml(t.task_id)}" data-tid="${escapeHtml(t.task_id)}" onclick="toggleTaskDetail('${escapeHtml(t.task_id)}')">
        <div class="row1">
          <span class="title" title="${escapeHtml(t.payload || '')}">${escapeHtml(makeTaskTitle(t.from_model, t.created_at, t.payload))}</span>
          <span class="task-status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span>
          ${verifyingBadge}
          ${verifyCount ? `<span class="muted" title="验收历史条数">🔁 ${verifyCount}</span>` : ''}
        </div>
        <div class="row2">
          <span class="tid">${escapeHtml(t.task_id)}</span>
          <span class="muted">${escapeHtml(t.topic || '')}</span>
          ${t.from_model ? `<span class="muted">← ${escapeHtml(t.from_model)}</span>` : ''}
          ${t.for_model && t.for_model !== t.from_model ? `<span class="muted">→ ${escapeHtml(t.for_model)}</span>` : ''}
          <span class="muted">认领者 ${escapeHtml(t.claimed_by || '-')}</span>
          ${['running', 'claimed'].includes(t.status) && (t.claimed_at || t.created_at)
            ? `<span class="elapsed-live" data-start="${t.claimed_at || t.created_at}" title="已运行时长（每秒刷新）">⏱ ${fmtElapsed(Date.now() / 1000 - (t.claimed_at || t.created_at))}</span>`
            : ''}
          <span class="muted">${fmtTime(t.created_at)}</span>
        </div>
        <div class="payload">${escapeHtml((t.payload || '').slice(0, 500))}</div>
        ${t.error ? `<div class="muted" style="color: var(--err); margin-top: 4px">错误: ${escapeHtml(t.error)}</div>` : ''}
      </div>
    `;
  }).join(''));
}

async function toggleTaskDetail(tid) {
  const detailEl = document.getElementById('task-detail');
  if (currentTaskDetail === tid) {
    currentTaskDetail = null;
    detailEl.innerHTML = '';
    document.querySelectorAll('.task-item').forEach(e => e.classList.remove('active'));
    stopLiveStream();
    return;
  }
  currentTaskDetail = tid;
  document.querySelectorAll('.task-item').forEach(e => e.classList.remove('active'));
  const li = document.querySelector(`.task-item[data-tid="${CSS.escape(tid)}"]`);
  if (li) li.classList.add('active');
  detailEl.innerHTML = '<div class="muted" style="padding: 12px">加载中...</div>';
  try {
    const d = await fetchJson(`/api/tasks/${encodeURIComponent(tid)}/details`);
    if (!d.ok) {
      detailEl.innerHTML = `<div class="muted" style="color: var(--err); padding: 12px">${escapeHtml(d.error || '加载失败')}</div>`;
      return;
    }
    const t = d.task;
    const log = d.log;
    const transcript = d.transcript;
    const hist = d.history;

    const renderHistSection = (title, items) => {
      if (!items || !items.length) return '';
      return `
        <h3 style="font-size: 13px; margin: 12px 0 4px">${title} (${items.length})</h3>
        <div class="hist-list">
          ${items.map(h => `
            <div class="hist-item" data-tid="${escapeHtml(h.task_id)}" onclick="toggleTaskDetail('${escapeHtml(h.task_id)}')">
              <span class="task-status ${escapeHtml(h.status)}">${escapeHtml(h.status)}</span>
              <span class="tid">${escapeHtml(h.task_id)}</span>
              <span class="muted">${fmtTime(h.created_at)}</span>
              <span class="hist-payload">${escapeHtml((h.payload || '').slice(0, 80))}</span>
            </div>
          `).join('')}
        </div>
      `;
    };

    // verifying 状态：醒目标识
    const verifyingBanner = t.status === 'verifying' ? `
      <div class="verify-banner">
        <div>
          <div style="font-size: 14px; font-weight: 600; color: var(--warn)">⚠️ 此任务在 verifying 状态 — 等验收</div>
          <div class="muted" style="font-size: 12px; margin-top: 2px">先用下方"会话内容"看 agent 实际干了什么，再决定通过 / 不通过。</div>
        </div>
      </div>
    ` : '';

    detailEl.innerHTML = `
      <div class="card" style="margin-top: 12px">
        <div class="row1" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
          <h2 style="font-size: 14px; margin: 0">任务详情 · ${escapeHtml(t.task_id)}</h2>
          <button onclick="stopLiveStream(); currentTaskDetail=null; document.getElementById('task-detail').innerHTML=''; document.querySelectorAll('.task-item').forEach(e=>e.classList.remove('active'))" style="background: var(--border-dim); color: var(--text); border: 1px solid var(--border); padding: 2px 8px; cursor: pointer; font-size: 11px;">收起</button>
        </div>
        ${verifyingBanner}
        <table class="detail-table">
          <tr><td>状态</td><td><span class="task-status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></td></tr>
          <tr><td>主题</td><td>${escapeHtml(t.topic || '-')}</td></tr>
          <tr><td>发起方</td><td>${escapeHtml(t.from_model || '-')}</td></tr>
          <tr><td>接收方</td><td>${escapeHtml(t.for_model || '-')}</td></tr>
          <tr><td>认领者</td><td>${escapeHtml(t.claimed_by || '-')}</td></tr>
          <tr><td>创建时间</td><td>${fmtTime(t.created_at)}</td></tr>
          <tr><td>认领时间</td><td>${fmtTime(t.claimed_at)}</td></tr>
          <tr><td>完成时间</td><td>${fmtTime(t.completed_at)}</td></tr>
          ${t.error ? `<tr><td>错误</td><td style="color: var(--err)">${escapeHtml(t.error)}</td></tr>` : ''}
          <tr><td>重试</td><td>${t.retries} / ${t.max_retries}</td></tr>
        </table>
        ${(t.acceptance && t.acceptance.criteria && t.acceptance.criteria.length) ? renderAcceptance(t) : ''}
        ${(t.verify_history && t.verify_history.length) ? renderVerifyHistory(t.verify_history) : ''}

        ${d.sub_tasks && d.sub_tasks.length ? renderSubTaskList(d.sub_tasks) : ''}

        <div class="transcript-section" style="margin-top: 12px">
          <h3 style="font-size: 13px; margin: 12px 0 4px; display: flex; align-items: center; gap: 8px">
            📡 实时日志
            <span id="live-status" class="muted" style="font-size: 11px">--</span>
          </h3>
          <div class="live-controls">
            <label><input type="checkbox" id="live-autoscroll"> 跟随滚动</label>
            <button id="live-clear">清空</button>
            <span class="muted">上翻暂停跟随，滚回底部恢复</span>
          </div>
          <div id="live-log-container">
            <pre class="log" style="max-height: 300px; background: var(--inset); margin: 0">${log && log.content ? escapeHtml(log.content) : ''}</pre>
            <button id="live-jump-latest" class="live-jump-latest hidden">⤓ 已暂停跟随，点击回到底部</button>
          </div>
        </div>

        <div class="transcript-section" style="margin-top: 12px">
          <h3 style="font-size: 13px; margin: 12px 0 4px; display: flex; align-items: center; gap: 8px">
            💬 会话内容（transcript）${transcript ? `· <span class="muted">${transcript.event_count} 个事件</span>` : ''}
            ${transcript ? `<button onclick="openSubagentTranscript('${escapeHtml(tid)}', 'task-transcript-container')" style="background: var(--accent-btn); color: #fff; border: none; padding: 2px 10px; cursor: pointer; font-size: 11px;">↻ 重新加载</button>` : ''}
          </h3>
          <div id="task-transcript-container">
            ${transcript ? `
              <div class="transcript-header">
                <span class="muted">${fmtBytes(transcript.size)} · ${escapeHtml(transcript.path)}</span>
              </div>
              <div class="transcript-body">${renderTranscriptView(transcript.events, ['running', 'claimed', 'pending'].includes(t.status))}</div>
            ` : '<div class="muted">无 transcript（这个 run 早于 v3，或子 agent 还没写完）</div>'}
          </div>
        </div>

        <h3 style="font-size: 13px; margin: 12px 0 4px">Payload（完整）</h3>
        <pre class="log">${escapeHtml(t.payload || '')}</pre>
        ${t.result ? `<h3 style="font-size: 13px; margin: 8px 0 4px">结果</h3><pre class="log">${escapeHtml(t.result)}</pre>` : ''}
        ${log ? `<h3 style="font-size: 13px; margin: 8px 0 4px">📋 子 Agent stdout/stderr 日志（末尾 32KB）<span class="muted" style="font-weight: 400">${fmtBytes(log.size)} · ${escapeHtml(log.path)}</span></h3><pre class="log">${escapeHtml(log.content)}</pre>` : ''}
        <h3 style="font-size: 13px; margin: 12px 0 4px; padding-top: 8px; border-top: 1px solid var(--border)">相关历史</h3>
        <p class="muted" style="font-size: 12px; margin-bottom: 8px">同 model / 同发起方 / 同 worker / 同主题（点开跳到那个任务）</p>
        ${renderHistSection('同 model（' + escapeHtml(t.for_model || '-') + '）的任务', hist.by_for_model)}
        ${renderHistSection('同发起方（' + escapeHtml(t.from_model || '-') + '）派过', hist.by_from_model)}
        ${renderHistSection('同 worker（' + escapeHtml(t.claimed_by || '-') + '）接的', hist.by_worker)}
        ${renderHistSection('同主题（' + escapeHtml(t.topic || '-') + '）的其他任务', hist.by_topic)}
      </div>
    `;
    detailEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // 实时日志：绑定控制条（root 限定到任务详情面板）；running 任务自动接 SSE，已结束的只留静态日志
    bindLiveLogControls(document.getElementById('task-detail'));
    startLiveStream(tid, ['running', 'claimed', 'pending'].includes(t.status));
  } catch (e) {
    detailEl.innerHTML = `<div class="muted" style="color: var(--err); padding: 12px">异常: ${escapeHtml(e.message)}</div>`;
  }
}

function renderAcceptance(t) {
  const a = t.acceptance || {};
  const crits = (a.criteria || []).map(c => `<li>${escapeHtml(c)}</li>`).join('');
  return `
    <h3 style="font-size: 13px; margin: 12px 0 4px">验收标准</h3>
    <div class="card" style="background: var(--inset); padding: 8px; margin-bottom: 8px">
      <ul style="margin: 0 0 0 16px; padding: 0; font-size: 12px">${crits}</ul>
      <div class="muted" style="font-size: 11px; margin-top: 4px">
        验收方: <b>${escapeHtml(a.verifier || '-')}</b> ·
        最大重做: <b>${a.max_iterations || 0}</b> ·
        验不过自动重发: <b>${a.auto_retry ? '是' : '否'}</b>
      </div>
    </div>
    ${t.status === 'verifying' ? renderVerifyActions(t.task_id) : ''}
  `;
}

function renderVerifyHistory(history) {
  if (!history.length) return '';
  const items = history.map(v => `
    <div class="hist-item" style="cursor: default">
      <span class="task-status ${v.passed ? 'done' : 'failed'}">${v.passed ? '✓ 通过' : '✗ 不通过'}</span>
      <span class="tid">${escapeHtml(v.verifier || '-')}</span>
      <span class="muted">分数 ${(v.score || 0).toFixed(2)}</span>
      <span class="muted">${fmtTime(v.at)}</span>
      ${v.issues ? `<span style="color: var(--err)">${escapeHtml(v.issues)}</span>` : ''}
    </div>
  `).join('');
  return `
    <h3 style="font-size: 13px; margin: 12px 0 4px">验收历史（${history.length}）</h3>
    <div class="hist-list">${items}</div>
  `;
}

function renderVerifyActions(tid) {
  return `
    <div class="card" style="background: var(--inset); padding: 8px; margin-top: 8px; border: 1px solid var(--accent-btn)">
      <p class="muted" style="font-size: 12px; margin-bottom: 6px">任务在 verifying 状态 —— 手动写验收结果：</p>
      <div style="display: flex; gap: 6px; align-items: center">
        <input type="text" id="verifier-name" value="用户" placeholder="verifier" style="width: 100px; background: var(--panel); border: 1px solid var(--border-dim); color: var(--text); padding: 4px 8px; font-size: 12px;">
        <input type="text" id="verify-issues" placeholder="issues（不通过原因）" style="flex: 1; background: var(--panel); border: 1px solid var(--border-dim); color: var(--text); padding: 4px 8px; font-size: 12px;">
        <button onclick="submitVerify('${escapeHtml(tid)}', true)" style="background: var(--ok); color: #fff; border: none; padding: 4px 12px; cursor: pointer; font-size: 12px;">✓ 通过</button>
        <button onclick="submitVerify('${escapeHtml(tid)}', false)" style="background: var(--err); color: #fff; border: none; padding: 4px 12px; cursor: pointer; font-size: 12px;">✗ 不通过</button>
      </div>
    </div>
  `;
}

async function submitVerify(tid, passed) {
  const verifier = document.getElementById('verifier-name').value || '用户';
  const issues = document.getElementById('verify-issues').value || '';
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(tid)}/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verifier, passed, issues }),
    });
    const data = await r.json();
    if (data.ok) {
      currentTaskDetail = tid;
      await toggleTaskDetail(tid);
    } else {
      alert('验收失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('异常: ' + e.message);
  }
}

// ---------- 监督 / 任务组 ----------

let currentCrewId = null;
let currentCrewMemberId = null;
let crewBusy = false;

async function renderCrew() {
  if (crewBusy) return;
  const d = await fetchJson('/api/crews');
  const list = document.getElementById('crew-list');
  const crews = d.crews || [];
  document.getElementById('crew-count').textContent = crews.length;
  setHtml(list, crews.map(c => {
    const sel = c.crew_id === currentCrewId ? ' selected' : '';
    const warn = (c.n_stuck || c.n_off_track)
      ? `<span class="badge" style="background:var(--err)">${c.n_stuck ? '卡死'+c.n_stuck : ''}${c.n_off_track ? ' 走歪'+c.n_off_track : ''}</span>`
      : '';
    return `<div class="runtime-item${sel}" id="crew-li-${escapeHtml(c.crew_id)}" onclick="selectCrew('${escapeHtml(c.crew_id)}')" style="cursor:pointer">
      <span class="dot ${c.n_stuck ? 'red' : (c.n_running ? 'green' : 'gray')}"></span>
      <div style="flex:1;min-width:0">
        <div>${escapeHtml((c.goal || '').slice(0, 80))}</div>
        <div class="muted" style="font-size:11px">${c.n_members || 0} 人 · 在跑 ${c.n_running || 0} ${warn}</div>
      </div>
    </div>`;
  }).join('') || '<div class="muted">还没有任务组</div>');
  if (currentCrewId) await renderCrewDetail(currentCrewId);
}

async function selectCrew(id) {
  currentCrewId = id;
  currentCrewMemberId = null;
  document.querySelectorAll('#crew-list .runtime-item').forEach(el => {
    el.classList.toggle('selected', el.id === 'crew-li-' + id);
  });
  await renderCrewDetail(id);
}

function activeCrewStage() {
  if (currentTab === 'cluster') return document.querySelector('#cluster-detail .crew-stage');
  return document.querySelector('#crew-detail .crew-stage');
}

function selectCrewMember(crewId, memberId) {
  currentCrewId = crewId;
  currentCrewMemberId = memberId;
  const stage = activeCrewStage();
  if (stage) {
    stage.querySelectorAll('.crew-node').forEach(n => {
      n.classList.toggle('is-sel', n.dataset.mid === memberId);
    });
  }
  loadCrewPreview(crewId, memberId, stage);
}

function crewRelationHtml(c, opts) {
  const includeForm = !opts || opts.includeForm !== false;
  const pfx = (opts && opts.pfx) || 'sv';
  const members = c.members || [];
  const board = c.blackboard || [];
  const svg = crewGraphSvg(c, pfx);
  const memberCards = members.map(m => {
    const tags = [];
    if (m.stuck) tags.push('<span class="stuck-badge">卡死</span>');
    if (m.off_track) tags.push('<span style="color:var(--err)">走歪</span>');
    if (m.status === 'running' && m.alive) tags.push('<span class="running-badge">在跑</span>');
    const sel = m.member_id === currentCrewMemberId ? ' selected' : '';
    return `<div class="connect-tool${sel}" id="${pfx}-mem-${escapeHtml(m.member_id)}" onclick="selectCrewMember('${escapeHtml(c.crew_id)}','${escapeHtml(m.member_id)}')" style="cursor:pointer">
      <div><strong>${escapeHtml(m.role)}</strong> · ${escapeHtml(m.runtime)}/${escapeHtml(m.model)} ${tags.join(' ')}</div>
      <div class="muted" style="font-size:11px">task ${escapeHtml(m.task_id || '')} · 空闲 ${m.idle_sec == null ? '-' : m.idle_sec + 's'} · ${escapeHtml(m.stuck_reason || '')}</div>
      <div class="muted" style="font-size:11px">${escapeHtml((m.task || m.summary || '').slice(0, 160))}</div>
      <div class="connect-actions" style="margin-top:6px" onclick="event.stopPropagation()">
        <button class="btn-primary" onclick="crewSupervise('${escapeHtml(c.crew_id)}','${escapeHtml(m.member_id)}','unstick')">解卡</button>
        <button class="btn" onclick="crewSupervise('${escapeHtml(c.crew_id)}','${escapeHtml(m.member_id)}','correct')">纠正</button>
        <button class="btn" onclick="crewSupervise('${escapeHtml(c.crew_id)}','${escapeHtml(m.member_id)}','flag_off_track')">标走歪</button>
        <button class="btn danger-btn" onclick="crewSupervise('${escapeHtml(c.crew_id)}','${escapeHtml(m.member_id)}','kill')">停止</button>
      </div>
    </div>`;
  }).join('') || '<p class="muted">还没有成员。加人后会出现在关系图上。</p>';
  const boardHtml = board.slice(-20).map(b =>
    `<div class="crew-board-item"><span class="who">${escapeHtml(b.from)}</span>${b.to ? ' → ' + escapeHtml(b.to) : ' → 全员'} · ${escapeHtml(b.kind)} · ${escapeHtml((b.text || '').slice(0, 240))}</div>`
  ).join('');
  return `
    <div class="crew-stage" id="${pfx}-stage" data-crew="${escapeHtml(c.crew_id)}">
      <p class="crew-goal-banner"><strong>共同目标</strong> ${escapeHtml(c.goal || '')}</p>
      <div class="crew-graph-wrap">${svg}</div>
      <p class="crew-legend">
        <span><i class="lg-run"></i>在跑</span>
        <span><i class="lg-stuck"></i>卡死</span>
        <span><i class="lg-off"></i>走歪</span>
        <span><i class="lg-board"></i>黑板留言 / 监督动作</span>
        <span class="muted">点节点看任务和思考</span>
      </p>
      <div class="crew-preview">
        <div class="crew-preview-pane">
          <h4>任务</h4>
          <div class="crew-preview-body js-keep crew-preview-task muted">点图上一个成员</div>
        </div>
        <div class="crew-preview-pane">
          <h4>思考</h4>
          <div class="crew-preview-body js-keep crew-preview-think muted">点图上一个成员</div>
        </div>
        <div class="crew-preview-pane">
          <h4>最近输出</h4>
          <div class="crew-preview-body js-keep crew-preview-out muted">点图上一个成员</div>
        </div>
      </div>
      <h3 style="margin-top:12px">成员</h3>
      ${memberCards}
      ${includeForm ? `<div class="card" style="margin-top:12px">
        <h3>加人</h3>
        <label class="form-label">角色</label>
        <input id="${pfx}-role" class="form-input crew-role" placeholder="例如 implementer / reviewer">
        <label class="form-label">分工</label>
        <textarea id="${pfx}-task" class="form-input crew-task" rows="3" placeholder="这个成员具体做什么"></textarea>
        <label class="form-label">runtime / model</label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          <input id="${pfx}-runtime" class="form-input crew-runtime" placeholder="已连接的 runtime，如 opencode">
          <input id="${pfx}-model" class="form-input crew-model" placeholder="该 runtime 下的模型名，用户自定">
        </div>
        <button type="button" class="btn-primary crew-add-btn" style="margin-top:8px">加入并开工</button>
        <span class="muted crew-add-status"></span>
      </div>` : ''}
      <h3 style="margin-top:12px">黑板（谁对谁说了什么）</h3>
      ${boardHtml || '<p class="muted">空</p>'}
      <textarea id="${pfx}-note" class="form-input crew-note" rows="2" placeholder="监督者留言"></textarea>
      <button type="button" class="btn crew-note-btn" style="margin-top:6px">写到黑板</button>
    </div>
  `;
}

function crewGraphSvg(c, pfx) {
  pfx = pfx || 'sv';
  const members = c.members || [];
  const W = 720, H = 300;
  const cx = W / 2, cy = 168, R = 108;
  const n = members.length;
  const pos = { supervisor: { x: 110, y: 46 }, board: { x: cx, y: 46 } };
  members.forEach((m, i) => {
    const a = Math.PI * (0.15 + 0.7 * (n === 1 ? 0.5 : i / Math.max(n - 1, 1)));
    pos[m.role] = {
      x: cx + Math.cos(a) * (R + 70),
      y: cy + Math.sin(a) * R,
      mid: m.member_id,
      m,
    };
  });
  const node = (key, label, sub, cls, mid) => {
    const p = pos[key];
    if (!p) return '';
    const sel = mid && mid === currentCrewMemberId ? ' is-sel' : '';
    const x = p.x - 70, y = p.y - 22;
    return `<g class="crew-node ${cls}${sel}" id="${pfx}-n-${escapeHtml(mid || key)}" data-key="${escapeHtml(key)}" data-mid="${escapeHtml(mid || '')}" transform="translate(${x},${y})">
      <rect width="140" height="44"></rect>
      <text x="10" y="18">${escapeHtml(label.slice(0, 16))}</text>
      <text class="crew-node-sub" x="10" y="34">${escapeHtml((sub || '').slice(0, 22))}</text>
    </g>`;
  };
  const edges = [];
  const lastSeq = Math.max(0, ...((c.blackboard || []).map(b => b.seq || 0)));
  (c.blackboard || []).slice(-24).forEach(b => {
    const fromKey = b.from === 'system' ? 'board' : (pos[b.from] ? b.from : (b.from === 'supervisor' ? 'supervisor' : 'board'));
    let toKey = 'board';
    if (b.to && pos[b.to]) toKey = b.to;
    else if (!b.to && fromKey !== 'board') toKey = 'board';
    const a = pos[fromKey], t = pos[toKey];
    if (!a || !t || fromKey === toKey) return;
    const kind = b.kind || 'note';
    const cls = kind === 'supervise' ? 'is-supervise' : (kind === 'flag' ? 'is-flag' : '');
    const latest = b.seq === lastSeq ? ' is-latest' : '';
    const x1 = a.x, y1 = a.y, x2 = t.x, y2 = t.y;
    const mx = (x1 + x2) / 2 + (y1 > y2 ? 18 : -18);
    const my = (y1 + y2) / 2;
    edges.push(`<path class="crew-edge ${cls}${latest}" d="M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}" marker-end="url(#${pfx}-arrow)"/>`);
  });
  const memberNodes = members.map(m => {
    let cls = 'is-run';
    if (m.stuck) cls = 'is-stuck';
    else if (m.off_track) cls = 'is-off';
    else if (m.status !== 'running') cls = '';
    const sub = m.stuck ? '卡死' : (m.off_track ? '走歪' : (m.status || ''));
    return node(m.role, m.role, sub, cls, m.member_id);
  }).join('');
  return `<svg class="crew-graph" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <marker id="${pfx}-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6" fill="none" stroke="currentColor" />
      </marker>
    </defs>
    ${edges.join('')}
    ${node('supervisor', c.supervisor || '监督者', '解卡 / 纠偏', 'is-sup', '')}
    ${node('board', '黑板', 'crew_post / poll', 'is-board', '')}
    ${memberNodes}
  </svg>`;
}

function bindCrewRelation(root, crewId) {
  if (!root) return;
  if (currentCrewMemberId) loadCrewPreview(crewId, currentCrewMemberId, root);
  else {
    const first = root.querySelector('.crew-node[data-mid]:not([data-mid=""])');
    if (first && first.dataset.mid) selectCrewMember(crewId, first.dataset.mid);
  }
}

async function loadCrewPreview(crewId, memberId, stage) {
  stage = stage || activeCrewStage();
  const taskEl = stage && stage.querySelector('.crew-preview-task');
  const thinkEl = stage && stage.querySelector('.crew-preview-think');
  const outEl = stage && stage.querySelector('.crew-preview-out');
  if (!taskEl || !thinkEl) return;
  try {
    const d = await fetchJson(`/api/crews/${encodeURIComponent(crewId)}/members/${encodeURIComponent(memberId)}/preview`);
    if (!d.ok) {
      setHtml(taskEl, `<span class="muted">${escapeHtml(d.error || '没有预览')}</span>`);
      setHtml(thinkEl, '');
      if (outEl) setHtml(outEl, '');
      return;
    }
    taskEl.classList.remove('muted');
    thinkEl.classList.remove('muted');
    if (outEl) outEl.classList.remove('muted');
    setHtml(taskEl, `
      <div class="crew-preview-meta">${escapeHtml(d.role || '')} · ${escapeHtml(d.runtime || '')}/${escapeHtml(d.model || '')}${d.live ? ' · 实时' : ''}</div>
      <div class="crew-latest">${escapeHtml(d.task || '（没有单独存分工）')}</div>
    `);
    setHtml(thinkEl, d.thinking
      ? `<div class="crew-thinking">${escapeHtml(d.thinking.slice(-1200))}</div>`
      : '<div class="muted">还没有推理片段（有的 runtime 不单独上报 thinking）</div>');
    if (outEl) {
      const tools = (d.tools || []).length
        ? `<div class="crew-tools">工具 ${d.tools.map(t => escapeHtml(t)).join(' · ')}</div>`
        : '';
      const latest = d.latest
        ? `<div class="crew-latest">${escapeHtml(d.latest.slice(-900))}</div>`
        : '<div class="muted">还没有输出</div>';
      setHtml(outEl, latest + tools);
    }
  } catch (e) {
    setHtml(thinkEl, `<span class="muted">${escapeHtml(e.message)}</span>`);
  }
}

async function renderCrewDetail(id) {
  const d = await fetchJson('/api/crews/' + encodeURIComponent(id));
  const el = document.getElementById('crew-detail');
  if (!d.ok) {
    setHtml(el, `<p class="muted">${escapeHtml(d.error || '找不到')}</p>`);
    return;
  }
  const c = d.crew;
  if (currentCrewMemberId && !(c.members || []).some(m => m.member_id === currentCrewMemberId)) {
    currentCrewMemberId = null;
  }
  setHtml(el, crewRelationHtml(c, { pfx: 'sv' }));
  bindCrewRelation(el.querySelector('.crew-stage'), c.crew_id);
}

async function refreshCrewViews() {
  if (currentTab === 'cluster') await renderCluster();
  else await renderCrew();
}

async function crewAddMember(crewId) {
  const stage = activeCrewStage();
  if (!stage) return;
  const role = (stage.querySelector('.crew-role') || {}).value;
  const task = (stage.querySelector('.crew-task') || {}).value;
  const runtime = (stage.querySelector('.crew-runtime') || {}).value;
  const model = (stage.querySelector('.crew-model') || {}).value;
  const st = stage.querySelector('.crew-add-status');
  if (st) st.textContent = '开工中...';
  crewBusy = true;
  try {
    const r = await fetch(`/api/crews/${encodeURIComponent(crewId)}/members`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, task, runtime, model }),
    });
    const d = await r.json();
    if (st) st.textContent = d.ok ? '已加入 ' + (d.task_id || '') : (d.error || '失败');
    if (!d.ok) alert(d.error || '失败');
    currentCrewId = crewId;
    await refreshCrewViews();
  } finally {
    crewBusy = false;
  }
}

async function crewPost(crewId) {
  const stage = activeCrewStage();
  const note = stage && stage.querySelector('.crew-note');
  const text = note ? note.value : '';
  const r = await fetch(`/api/crews/${encodeURIComponent(crewId)}/post`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, from_role: 'supervisor', to: '' }),
  });
  const d = await r.json();
  if (!d.ok) alert(d.error || '失败');
  await refreshCrewViews();
}

async function crewSupervise(crewId, memberId, action) {
  let instruction = '';
  if (action === 'correct' || action === 'flag_off_track') {
    instruction = prompt(action === 'correct' ? '纠正指令（成员会按这个改方向）' : '为什么走歪了？') || '';
    if (action === 'correct' && !instruction.trim()) return;
  }
  crewBusy = true;
  try {
    const r = await fetch(`/api/crews/${encodeURIComponent(crewId)}/members/${encodeURIComponent(memberId)}/supervise`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, instruction }),
    });
    const d = await r.json();
    if (!d.ok) alert(d.error || '失败');
    currentCrewId = crewId;
    currentCrewMemberId = memberId;
    await refreshCrewViews();
  } finally {
    crewBusy = false;
  }
}

document.getElementById('crew-create').addEventListener('click', async () => {
  const goal = document.getElementById('crew-goal').value;
  const st = document.getElementById('crew-create-status');
  st.textContent = '创建中...';
  const r = await fetch('/api/crews', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ goal, supervisor: '用户' }),
  });
  const d = await r.json();
  if (!d.ok) {
    st.textContent = d.error || '失败';
    return;
  }
  st.textContent = '已创建';
  currentCrewId = (d.crew || {}).crew_id;
  document.getElementById('crew-goal').value = '';
  await renderCrew();
});

// 事件委托：morph 会保留节点，不能在每次轮询后再 addEventListener（会叠一层）
document.getElementById('subagent-list').addEventListener('click', (ev) => {
  const item = ev.target.closest('.subagent-item');
  if (!item) return;
  currentSubagent = item.dataset.tid;
  document.querySelectorAll('#subagent-list .subagent-item').forEach(e => e.classList.remove('active'));
  item.classList.add('active');
  showSubagentDetail(currentSubagent);
});

document.getElementById('dispatch-list').addEventListener('click', (ev) => {
  const tidEl = ev.target.closest('.tid');
  if (!tidEl || !tidEl.dataset.tid) return;
  openSubagentFromDispatch(tidEl.dataset.tid);
});

document.getElementById('model-wall').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.card-submit');
  if (!btn) return;
  const card = btn.closest('.model-card');
  if (card) submitFromCard(card);
});

function onCrewUi(ev) {
  const t = ev.target;
  if (ev.type === 'change' && t.classList && t.classList.contains('cluster-crew-pick')) {
    currentCrewId = t.value;
    currentCrewMemberId = null;
    renderCluster();
    return;
  }
  const add = t.closest && t.closest('.crew-add-btn');
  const note = t.closest && t.closest('.crew-note-btn');
  const node = t.closest && t.closest('.crew-node');
  const stage = t.closest && t.closest('.crew-stage');
  const crewId = (stage && stage.dataset.crew) || currentCrewId;
  if (add) { ev.preventDefault(); crewAddMember(crewId); return; }
  if (note) { ev.preventDefault(); crewPost(crewId); return; }
  if (node && node.dataset.mid) selectCrewMember(crewId, node.dataset.mid);
}
document.getElementById('tab-crew').addEventListener('click', onCrewUi);
document.getElementById('tab-cluster').addEventListener('click', onCrewUi);
document.getElementById('tab-cluster').addEventListener('change', onCrewUi);

// ---------- 启动 ----------

// running 任务的已运行时长：每秒就地更新文本节点，不用整表重渲染
setInterval(() => {
  const now = Date.now() / 1000;
  document.querySelectorAll('.elapsed-live').forEach(el => {
    const start = parseFloat(el.dataset.start || '0');
    if (start > 0) el.textContent = '⏱ ' + fmtElapsed(now - start);
  });
}, 1000);

refresh();
if (autoRefresh) startLive();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && autoRefresh) refresh(true);
});
