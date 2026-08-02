// mcp-hub dashboard 前端逻辑

const POLL_MS = 2000;
let autoRefresh = true;
let pollTimer = null;
let currentTab = 'overview';
let currentSubagent = null;
let liveEventSource = null;  // 当前正在实时 tail 的 SSE
let liveStreamTaskId = null; // 当前正在实时看的 task_id

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

function fmtBytes(b) {
  if (!b) return '0 B';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1024 / 1024).toFixed(1) + ' MB';
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
    refresh();  // 切 tab 立即刷一次
  });
});

// ---------- Auto refresh ----------

document.getElementById('auto-refresh').addEventListener('change', e => {
  autoRefresh = e.target.checked;
  document.getElementById('auto-refresh-status').textContent = autoRefresh ? '▶ 运行中' : '⏸ 已暂停';
  if (autoRefresh) startPoll();
  else stopPoll();
});

document.getElementById('refresh-btn').addEventListener('click', refresh);

// 封存会话开关
document.getElementById('subagent-show-archived').addEventListener('change', e => {
  showArchivedSubagents = e.target.checked;
  renderSubagents();
});
document.getElementById('task-show-archived').addEventListener('change', e => {
  showArchivedTasks = e.target.checked;
  renderTasks();
});

function startPoll() {
  stopPoll();
  pollTimer = setInterval(refresh, POLL_MS);
}

function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function refresh() {
  switch (currentTab) {
    case 'overview':  renderOverview(); break;
    case 'dispatch':  renderDispatch(); break;
    case 'runtimes':  renderRuntimes(); break;
    case 'subagents': renderSubagents(); break;
    case 'cluster':   renderCluster(); break;
    case 'tasks':     renderTasks(); break;
  }
  document.getElementById('last-update').textContent = '更新于 ' + fmtTime(Date.now() / 1000);
}

// 全局缓存 models（派活 tab 用）
let modelsCache = null;

// dispatch tab form 渲染状态：避免 2s 轮询 innerHTML 重写把用户正在编辑的内容清掉
let dispatchFormRendered = false;

// 暴露给 inline onclick
window.toggleTaskDetail = toggleTaskDetail;
window.submitVerify = submitVerify;
window.openSubagentTranscript = openSubagentTranscript;

// ---------- 实时日志流（SSE） ----------

function stopLiveStream() {
  if (liveEventSource) {
    liveEventSource.close();
    liveEventSource = null;
    liveStreamTaskId = null;
  }
}

function startLiveStream(tid) {
  // 避免重复连接同一个 task
  if (liveStreamTaskId === tid && liveEventSource) return;
  stopLiveStream();
  liveStreamTaskId = tid;

  const statusEl = document.getElementById('live-status');
  const container = document.getElementById('live-log-container');
  if (!statusEl || !container) return;

  statusEl.textContent = '连接中...';
  statusEl.style.color = '#7d8590';

  const es = new EventSource(`/api/subagents/${encodeURIComponent(tid)}/stream`);
  liveEventSource = es;

  es.onopen = () => {
    statusEl.textContent = '● 实时连接中';
    statusEl.style.color = '#2ea043';
  };
  es.onerror = () => {
    statusEl.textContent = '× 连接断开';
    statusEl.style.color = '#f85149';
  };
  es.onmessage = (e) => {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    if (data.type === 'log') {
      const pre = container.querySelector('pre');
      if (pre) {
        pre.textContent += data.content;
        pre.scrollTop = pre.scrollHeight;
      }
    } else if (data.type === 'meta') {
      statusEl.title = data.path || '';
    } else if (data.type === 'error') {
      statusEl.textContent = '错误: ' + (data.message || '');
      statusEl.style.color = '#f85149';
      es.close();
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
  statusEl.style.color = '#7d8590';
  try {
    const r = await fetch('/api/dashboard/pinned-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinned_models: models }),
    });
    const data = await r.json();
    if (data.ok) {
      statusEl.textContent = `已保存 (${data.pinned_models.length} 个)`;
      statusEl.style.color = '#2ea043';
      // 重新拉 models 让卡片墙刷新
      modelsCache = null;
      await renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = '#f85149';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = '#f85149';
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
  rl.innerHTML = o.runtimes.items.map(r => `
    <div class="runtime-item">
      <span class="dot ${r.available ? 'green' : 'red'}"></span>
      <span class="runtime-name">${escapeHtml(r.name)}</span>
      <span class="runtime-models">${r.models.length ? r.models.slice(0, 3).join(', ') + (r.models.length > 3 ? '…' : '') : '(stub)'}</span>
    </div>
  `).join('') || '<div class="muted">无运行时</div>';

  document.getElementById('tool-count').textContent = o.tools.count;
  const tl = document.getElementById('tool-list');
  tl.innerHTML = o.tools.items.map(t => `
    <div class="tool-item">
      <span class="dot ${t.available ? 'green' : 'red'}"></span>
      <span class="tool-name">${escapeHtml(t.name)}</span>
      <span class="tool-ops">${(t.operations || []).slice(0, 4).join(', ')}</span>
    </div>
  `).join('') || '<div class="muted">无工具</div>';

  const cfg = o.config;
  document.getElementById('cluster-summary').innerHTML = `
    <table class="detail-table">
      <tr><td>状态</td><td>${cfg.cluster_enabled ? `<span class="dot green"></span> 已开启` : `<span class="dot gray"></span> 已关闭`}</td></tr>
      <tr><td>数量</td><td>${cfg.cluster_size || 0}</td></tr>
      <tr><td>模型</td><td>${escapeHtml(cfg.cluster_model)}</td></tr>
      <tr><td>主题</td><td>${escapeHtml(cfg.cluster_topic)}</td></tr>
      <tr><td>队列</td><td>${escapeHtml(cfg.queue_path)}</td></tr>
    </table>
  `;

  const q = o.queue;
  const s = q.stats || {};
  document.getElementById('queue-summary').innerHTML = `
    <table class="detail-table">
      <tr><td>总数</td><td>${s.total || 0}</td></tr>
      ${Object.entries(s.by_status || {}).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}
      <tr><td>主题数</td><td>${(q.topics || []).length}</td></tr>
    </table>
  `;
}

// ---------- Runtimes ----------

async function renderRuntimes() {
  const r = await fetchJson('/api/runtimes');
  const el = document.getElementById('runtime-detail');
  el.innerHTML = r.items.map(rt => `
    <div class="card" style="margin-bottom: 12px">
      <h2>
        <span class="dot ${rt.available ? 'green' : 'red'}"></span>
        ${escapeHtml(rt.name)}
        <span class="muted" style="font-weight: 400">二进制=${escapeHtml(rt.binary)}</span>
      </h2>
      ${rt.status ? `<div class="muted" style="margin-bottom: 8px">状态: ${escapeHtml(rt.status)} · ${escapeHtml(rt.note || '')}</div>` : ''}
      <div class="muted" style="font-size: 12px">${rt.models.length ? rt.models.map(m => `<span style="background: #21262d; padding: 1px 6px; border-radius: 3px; margin-right: 4px; display: inline-block; margin-bottom: 4px">${escapeHtml(m)}</span>`).join('') : '（占位，无可用模型）'}</div>
    </div>
  `).join('') || '<div class="muted">无运行时</div>';
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
      const body = role === 'assistant'
        ? `<div class="msg-content md">${renderMarkdown(ev.content || '')}</div>`
        : `<div class="msg-content">${escapeHtml(ev.content || '')}</div>`;
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
          <pre class="msg-args">${escapeHtml(argsStr)}</pre>
          ${resultPreview}
        </div>
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
        ${r.live ? '<span style="color:#3fb950;font-size:11px">● 实时预览</span>' : ''}
      </div>
      <div class="transcript-body">${renderTranscriptView(r.events, isRunning)}</div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="muted" style="color: #f85149">异常: ${escapeHtml(e.message)}</div>`;
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
    list.innerHTML = `<div class="muted">${showArchivedSubagents ? '暂无子 Agent' : '暂无活跃子 Agent（已封存 ' + (r.archived_count || 0) + ' 个）'}</div>`;
    if (currentSubagent) {
      currentSubagent = null;
      document.getElementById('subagent-detail').innerHTML = '<p class="muted">选左侧一项查看详情</p>';
    }
    return;
  }

  list.innerHTML = r.subagents.map(s => {
    const isRunning = ['running', 'claimed', 'pending'].includes(s.status);
    return `
    <div class="subagent-item ${currentSubagent === s.task_id ? 'active' : ''} ${s.status === 'verifying' ? 'task-verify' : ''}" data-tid="${escapeHtml(s.task_id)}">
      <div class="row1">
        <span class="title" title="${escapeHtml(s.payload || '')}">${escapeHtml(makeTaskTitle(s.from_model, s.created_at, s.payload))}</span>
        <span class="status">${escapeHtml(s.status || '-')}</span>
        ${isRunning ? '<span class="running-badge">⏳ 工作中</span>' : ''}
        ${s.status === 'verifying' ? '<span class="verify-badge">⚠ 待验收</span>' : ''}
      </div>
      <div class="row2">
        <span class="tid">${escapeHtml(s.task_id)}</span>
        <span class="muted">${escapeHtml(s.claimed_by || s.topic || '-')}</span>
        ${s.for_model ? `<span class="model-badge" title="调用的模型">🧠 ${escapeHtml(s.for_model)}</span>${effortBadge(s.for_model, s.reasoning_effort)}` : ''}
        <span class="muted">${fmtDuration(s.duration_sec)}</span>
        <span class="muted">${fmtTime(s.claimed_at)}</span>
        ${s.last_activity ? `<span class="muted" title="最后活动时间">⏱ ${fmtTime(s.last_activity)}</span>` : ''}
        ${s.possibly_stuck ? '<span class="stuck-badge">⚠ 疑似卡死</span>' : ''}
      </div>
      ${s.result_preview ? `<div class="payload">${escapeHtml(s.result_preview.slice(0, 120))}</div>` : ''}
    </div>
  `;
  }).join('');

  list.querySelectorAll('.subagent-item').forEach(el => {
    el.addEventListener('click', () => {
      currentSubagent = el.dataset.tid;
      list.querySelectorAll('.subagent-item').forEach(e => e.classList.remove('active'));
      el.classList.add('active');
      showSubagentDetail(currentSubagent);
    });
  });

  // 如果当前有打开详情的 task，只轻量更新列表里的状态高亮，
  // 不要重刷详情面板：实时日志面板靠 SSE 自己推，重刷会打断滚动。
  if (currentSubagent) {
    const stillThere = r.subagents.find(s => s.task_id === currentSubagent);
    if (!stillThere) {
      currentSubagent = null;
      stopLiveStream();
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
      ${s.error ? `<tr><td>错误</td><td style="color: #f85149">${escapeHtml(s.error)}</td></tr>` : ''}
    </table>
    ${s.result_preview ? `<h3 style="font-size: 13px; margin: 8px 0 4px">结果（预览）</h3><pre class="log">${escapeHtml(s.result_preview)}</pre>` : ''}

    ${taskDetails.ok ? renderSubTaskList(taskDetails.sub_tasks) : ''}

    <div class="transcript-section" style="margin-top: 12px">
      <h3 style="font-size: 13px; margin: 12px 0 4px; display: flex; align-items: center; gap: 8px">
        📡 实时日志
        <span id="live-status" class="muted" style="font-size: 11px">--</span>
      </h3>
      ${isRunning ? `
        <div class="msg msg-running" style="margin-bottom: 8px">
          <div class="msg-role">⏳ 模型工作中<span class="working-dots"><span>.</span><span>.</span><span>.</span></span> <span class="ts">已运行 ${fmtDuration(s.duration_sec)}</span></div>
          <div class="msg-content muted">日志持续滚动说明还在工作；长时间无新内容可判断为卡死。</div>
        </div>
      ` : ''}
      <div id="live-log-container">
        <pre class="log" style="max-height: 300px; background: #0a0d12; margin: 0">${log && log.content ? escapeHtml(log.content) : ''}</pre>
      </div>
    </div>

    <div class="transcript-section">
      <h3 style="font-size: 13px; margin: 16px 0 4px; display: flex; align-items: center; gap: 8px">
        💬 会话内容（transcript）
        <button onclick="openSubagentTranscript('${escapeHtml(tid)}', 'transcript-container')" style="background: #1f6feb; color: #fff; border: none; padding: 2px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">${transcript.ok ? '↻ 重新加载' : '加载'}</button>
      </h3>
      <div id="transcript-container">
        ${transcript.ok ? `
          <div class="transcript-header">
            <span class="muted">${transcript.event_count} 个事件</span>
            <span class="muted">${fmtBytes(transcript.size)} · ${escapeHtml(transcript.path)}</span>
            ${transcript.live ? '<span style="color:#3fb950;font-size:11px">● 实时预览（任务进行中）</span>' : ''}
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
        <input type="text" id="user-msg-input" placeholder="例如：继续 / 上一步错了，重新解析 ..." style="flex: 1; background: #0a0d12; border: 1px solid #30363d; color: #e6edf3; padding: 6px 10px; border-radius: 4px; font-size: 13px;">
        <button id="user-msg-save" style="background: #21262d; color: #e6edf3; border: 1px solid #30363d; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">仅保存</button>
        <button id="user-msg-continue" style="background: #1f6feb; color: #fff; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">继续任务</button>
      </div>
      <div id="user-msg-status" class="muted" style="font-size: 12px; margin-bottom: 4px"></div>
      <div id="user-msg-list" style="font-size: 12px"></div>
    </div>

    ${log && log.content ? `
      <h3 style="font-size: 13px; margin: 16px 0 4px">📋 完整 stdout/stderr 日志（末尾 ${log.tail_kb}KB）</h3>
      <pre class="log">${escapeHtml(log.content)}</pre>
    ` : ''}

    <h3 style="font-size: 13px; margin: 16px 0 4px; border-top: 1px solid #30363d; padding-top: 12px">会话历史</h3>
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

  // 启动实时日志 SSE
  startLiveStream(tid);

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
      msgList.innerHTML = `<div class="muted" style="color:#f85149">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function saveUserMessage() {
    const msg = msgInput.value.trim();
    if (!msg) {
      msgStatus.textContent = '消息不能为空';
      msgStatus.style.color = '#f85149';
      return;
    }
    msgStatus.textContent = '保存中...';
    msgStatus.style.color = '#7d8590';
    try {
      const r = await fetch(`/api/subagents/${tid}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      const data = await r.json();
      if (data.ok) {
        msgStatus.textContent = '已保存';
        msgStatus.style.color = '#2ea043';
        msgInput.value = '';
        await loadUserMessages();
      } else {
        msgStatus.textContent = '保存失败: ' + (data.error || '未知');
        msgStatus.style.color = '#f85149';
      }
    } catch (e) {
      msgStatus.textContent = '异常: ' + e.message;
      msgStatus.style.color = '#f85149';
    }
  }

  async function continueTask() {
    const msg = msgInput.value.trim();
    if (!confirm(`将基于原任务 spawn 一个新的续跑子 agent${msg ? '，并带上消息：" ' + msg.slice(0, 50) + '"' : ''}。继续？`)) {
      return;
    }
    msgContinueBtn.disabled = true;
    msgStatus.textContent = '续跑中（spawn 新子 agent）...';
    msgStatus.style.color = '#7d8590';
    try {
      const r = await fetch(`/api/subagents/${tid}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      const data = await r.json();
      if (data.ok) {
        const note = data.note ? `（${data.note}）` : '';
        msgStatus.textContent = `已续跑，新任务: ${data.new_task_id} ${note}`;
        msgStatus.style.color = '#2ea043';
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
        msgStatus.style.color = '#f85149';
      }
    } catch (e) {
      msgStatus.textContent = '异常: ' + e.message;
      msgStatus.style.color = '#f85149';
    } finally {
      msgContinueBtn.disabled = false;
    }
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
  const c = await fetchJson('/api/cluster');
  const el = document.getElementById('cluster-detail');
  if (!c.enabled) {
    el.innerHTML = `<div class="card"><h2>集群未启用</h2><p class="muted">${c.note || ''}</p></div>`;
    return;
  }

  // 多 pool 渲染
  const pools = c.pools || [];
  const totalSize = pools.reduce((sum, p) => sum + (p.size || 0), 0);
  const totalPending = (c.totals && c.totals.pending) || 0;
  const totalClaimed = (c.totals && c.totals.claimed) || 0;
  const totalDone = (c.totals && c.totals.done) || 0;
  const totalFailed = (c.totals && c.totals.failed) || 0;

  const poolsHtml = pools.map(p => `
    <div class="card" style="margin-bottom: 12px">
      <h2>
        <span class="pool-name">${escapeHtml(p.name)}</span>
        <span class="pool-tag" style="font-size: 12px; color: #8b949e; margin-left: 8px">
          ${p.enabled ? '✓ 启用' : '× 禁用'} · size=${p.size} · runtime=${escapeHtml(p.runtime)} · model=${escapeHtml(p.model)}
        </span>
      </h2>
      <table class="detail-table">
        <tr><td>主题</td><td><code>${escapeHtml(p.topic)}</code></td></tr>
        <tr><td>工作目录</td><td>${escapeHtml(p.workdir)}</td></tr>
        <tr><td>每 worker 并发数</td><td>${p.concurrency_per_worker}</td></tr>
        <tr><td>任务超时</td><td>${p.task_timeout_sec}秒</td></tr>
      </table>
      <div class="cluster-stat" style="margin-top: 8px">
        <div class="stat"><div class="num" style="color: #d29922">${p.queue.pending}</div><div class="label">待处理</div></div>
        <div class="stat"><div class="num" style="color: #1f6feb">${p.queue.claimed}</div><div class="label">认领中</div></div>
        <div class="stat"><div class="num" style="color: #2ea043">${p.queue.done}</div><div class="label">已完成</div></div>
        <div class="stat"><div class="num" style="color: #f85149">${p.queue.failed}</div><div class="label">失败</div></div>
      </div>
    </div>
  `).join('');

  el.innerHTML = `
    <div class="card">
      <h2>总览（${pools.length} 个 pool，共 ${totalSize} 个 worker）</h2>
      <div class="cluster-stat">
        <div class="stat"><div class="num" style="color: #d29922">${totalPending}</div><div class="label">待处理</div></div>
        <div class="stat"><div class="num" style="color: #1f6feb">${totalClaimed}</div><div class="label">认领中</div></div>
        <div class="stat"><div class="num" style="color: #2ea043">${totalDone}</div><div class="label">已完成</div></div>
        <div class="stat"><div class="num" style="color: #f85149">${totalFailed}</div><div class="label">失败</div></div>
      </div>
    </div>
    ${poolsHtml}
    <div class="card" style="margin-top: 12px">
      <h2>说明</h2>
      <p class="muted">${c.note || ''}</p>
      <p class="muted" style="margin-top: 8px">
        多 pool 路由：dashboard 派活选 model 时，自动按 <code>for_model = runtime/model</code> 路由到对应 pool 的 topic。
        例：选 <code>codex</code> runtime + <code>gpt-5.6-terra</code> model → 派到 <code>cluster.work.codex</code>。
      </p>
    </div>
  `;
}

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
    statusEl.style.color = '#f85149';
    return;
  }
  statusEl.textContent = '派发中...';
  statusEl.style.color = '#7d8590';
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
      statusEl.style.color = '#2ea043';
      document.getElementById('quick-payload').value = '';
      renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = '#f85149';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = '#f85149';
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

  el.innerHTML = html || '<div class="muted">没有可用模型</div>';

  // 绑定每张卡的派发按钮
  el.querySelectorAll('.model-card').forEach(card => {
    const submitBtn = card.querySelector('.card-submit');
    submitBtn.addEventListener('click', () => submitFromCard(card));
  });
}

function renderModelCard(m) {
  const id = `mc-${m.runtime}-${m.name}`.replace(/[^a-zA-Z0-9]/g, '_');
  const dotClass = m.available ? 'green' : 'red';
  return `
    <div class="model-card" data-runtime="${escapeHtml(m.runtime)}" data-model="${escapeHtml(m.name)}" data-available="${m.available}">
      <div class="card-header">
        <span class="dot ${dotClass}"></span>
        <span class="card-model-name" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</span>
      </div>
      <div class="card-runtime">${escapeHtml(m.runtime)}</div>
      <textarea class="card-payload" placeholder="任务内容..."></textarea>
      <div class="card-actions">
        <input type="text" class="card-topic" value="${m.runtime}.work" title="topic（默认 runtime.work）">
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
    statusEl.style.color = '#f85149';
    return;
  }
  const btn = card.querySelector('.card-submit');
  btn.disabled = true;
  statusEl.textContent = '派发中...';
  statusEl.style.color = '#7d8590';

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
      statusEl.style.color = '#2ea043';
      card.querySelector('.card-payload').value = '';
      // 跳到 dispatch 列表
      renderDispatch();
    } else {
      statusEl.textContent = `失败: ${data.error}`;
      statusEl.style.color = '#f85149';
    }
  } catch (e) {
    statusEl.textContent = `异常: ${e.message}`;
    statusEl.style.color = '#f85149';
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
    el.innerHTML = '<div class="muted">还没派过任务。在上方选 model 填任务内容点"派发"。</div>';
    return;
  }

  el.innerHTML = dispatchedTasks.map(t => {
    const resultClass = t.status === 'done' ? '' : (t.status === 'failed' ? 'failed' : 'pending');
    const resultText = t.status === 'done'
      ? t.result
      : t.status === 'failed'
        ? `错误: ${t.error || '(unknown)'}`
        : (t.status === 'claimed' ? `认领中 by ${t.claimed_by || '?'}...` : '等待 worker 认领...');
    return `
      <div class="dispatch-item">
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
  }).join('');

  // 点 task_id → 跳到子 Agent tab 看完整日志
  el.querySelectorAll('.tid').forEach(el => {
    el.addEventListener('click', () => {
      const tid = el.dataset.tid;
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
    });
  });
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
    el.innerHTML = `<div class="muted">${showArchivedTasks ? '无任务' : '无活跃任务（已封存 ' + (r.archived_count || 0) + ' 个）'}</div>`;
    return;
  }
  el.innerHTML = r.tasks.map(t => {
    const isActive = currentTaskDetail === t.task_id;
    const verifyingBadge = t.status === 'verifying'
      ? '<span class="verify-badge">⚠ 待验收</span>'
      : '';
    const verifyCount = (t.verify_history && t.verify_history.length) || 0;
    return `
      <div class="task-item ${isActive ? 'active' : ''} ${t.status === 'verifying' ? 'task-verify' : ''}" data-tid="${escapeHtml(t.task_id)}" onclick="toggleTaskDetail('${escapeHtml(t.task_id)}')">
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
          <span class="muted">${fmtTime(t.created_at)}</span>
        </div>
        <div class="payload">${escapeHtml((t.payload || '').slice(0, 500))}</div>
        ${t.error ? `<div class="muted" style="color: #f85149; margin-top: 4px">错误: ${escapeHtml(t.error)}</div>` : ''}
      </div>
    `;
  }).join('');
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
      detailEl.innerHTML = `<div class="muted" style="color: #f85149; padding: 12px">${escapeHtml(d.error || '加载失败')}</div>`;
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
          <div style="font-size: 14px; font-weight: 600; color: #d29922">⚠️ 此任务在 verifying 状态 — 等验收</div>
          <div class="muted" style="font-size: 12px; margin-top: 2px">先用下方"会话内容"看 agent 实际干了什么，再决定通过 / 不通过。</div>
        </div>
      </div>
    ` : '';

    detailEl.innerHTML = `
      <div class="card" style="margin-top: 12px">
        <div class="row1" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
          <h2 style="font-size: 14px; margin: 0">任务详情 · ${escapeHtml(t.task_id)}</h2>
          <button onclick="currentTaskDetail=null; document.getElementById('task-detail').innerHTML=''; document.querySelectorAll('.task-item').forEach(e=>e.classList.remove('active'))" style="background: #21262d; color: #e6edf3; border: 1px solid #30363d; padding: 2px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;">收起</button>
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
          ${t.error ? `<tr><td>错误</td><td style="color: #f85149">${escapeHtml(t.error)}</td></tr>` : ''}
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
          <div id="live-log-container">
            <pre class="log" style="max-height: 300px; background: #0a0d12; margin: 0">${log && log.content ? escapeHtml(log.content) : ''}</pre>
          </div>
        </div>

        <div class="transcript-section" style="margin-top: 12px">
          <h3 style="font-size: 13px; margin: 12px 0 4px; display: flex; align-items: center; gap: 8px">
            💬 会话内容（transcript）${transcript ? `· <span class="muted">${transcript.event_count} 个事件</span>` : ''}
            ${transcript ? `<button onclick="openSubagentTranscript('${escapeHtml(tid)}', 'task-transcript-container')" style="background: #1f6feb; color: #fff; border: none; padding: 2px 10px; border-radius: 3px; cursor: pointer; font-size: 11px;">↻ 重新加载</button>` : ''}
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
        <h3 style="font-size: 13px; margin: 12px 0 4px; padding-top: 8px; border-top: 1px solid #30363d">相关历史</h3>
        <p class="muted" style="font-size: 12px; margin-bottom: 8px">同 model / 同发起方 / 同 worker / 同主题（点开跳到那个任务）</p>
        ${renderHistSection('同 model（' + escapeHtml(t.for_model || '-') + '）的任务', hist.by_for_model)}
        ${renderHistSection('同发起方（' + escapeHtml(t.from_model || '-') + '）派过', hist.by_from_model)}
        ${renderHistSection('同 worker（' + escapeHtml(t.claimed_by || '-') + '）接的', hist.by_worker)}
        ${renderHistSection('同主题（' + escapeHtml(t.topic || '-') + '）的其他任务', hist.by_topic)}
      </div>
    `;
    detailEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // 启动实时日志 SSE
    startLiveStream(tid);
  } catch (e) {
    detailEl.innerHTML = `<div class="muted" style="color: #f85149; padding: 12px">异常: ${escapeHtml(e.message)}</div>`;
  }
}

function renderAcceptance(t) {
  const a = t.acceptance || {};
  const crits = (a.criteria || []).map(c => `<li>${escapeHtml(c)}</li>`).join('');
  return `
    <h3 style="font-size: 13px; margin: 12px 0 4px">验收标准</h3>
    <div class="card" style="background: #0a0d12; padding: 8px; margin-bottom: 8px">
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
      ${v.issues ? `<span style="color: #f85149">${escapeHtml(v.issues)}</span>` : ''}
    </div>
  `).join('');
  return `
    <h3 style="font-size: 13px; margin: 12px 0 4px">验收历史（${history.length}）</h3>
    <div class="hist-list">${items}</div>
  `;
}

function renderVerifyActions(tid) {
  return `
    <div class="card" style="background: #0a0d12; padding: 8px; margin-top: 8px; border: 1px solid #1f6feb">
      <p class="muted" style="font-size: 12px; margin-bottom: 6px">任务在 verifying 状态 —— 手动写验收结果：</p>
      <div style="display: flex; gap: 6px; align-items: center">
        <input type="text" id="verifier-name" value="用户" placeholder="verifier" style="width: 100px; background: #161b22; border: 1px solid #21262d; color: #e6edf3; padding: 4px 8px; border-radius: 4px; font-size: 12px;">
        <input type="text" id="verify-issues" placeholder="issues（不通过原因）" style="flex: 1; background: #161b22; border: 1px solid #21262d; color: #e6edf3; padding: 4px 8px; border-radius: 4px; font-size: 12px;">
        <button onclick="submitVerify('${escapeHtml(tid)}', true)" style="background: #2ea043; color: #fff; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">✓ 通过</button>
        <button onclick="submitVerify('${escapeHtml(tid)}', false)" style="background: #f85149; color: #fff; border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px;">✗ 不通过</button>
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

// ---------- 启动 ----------

refresh();
startPoll();
