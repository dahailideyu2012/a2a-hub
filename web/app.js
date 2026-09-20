/* ==========================================================================
   A2A Hub 控制台前端 —— 原生 JS，无构建步骤
   ========================================================================== */

const $ = (id) => document.getElementById(id);

const State = {
  agents: [],
  mode: 'single',
  running: false,
  abort: null,
  steps: new Map(),   // stepId -> DOM refs
};

/* --------------------------------------------------------------- 主题 --- */

function initTheme() {
  const saved = localStorage.getItem('a2a-theme');
  const prefer = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = saved || prefer;
}
initTheme();
$('btn-theme').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('a2a-theme', next);
};

/* ----------------------------------------------------------- HTTP 层 --- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return res.json();
}

/**
 * 通用 SSE 读取：用 fetch + ReadableStream，这样能带 POST body 和自定义头
 * （原生 EventSource 只支持 GET）。
 */
async function streamSSE(url, { body = null, method = 'POST', signal, onEvent }) {
  const res = await fetch(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    },
    body: body ? JSON.stringify(body) : null,
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 以空行分隔报文
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const parsed = parseSSEBlock(raw);
      if (parsed) onEvent(parsed);
    }
  }
}

function parseSSEBlock(raw) {
  const lines = raw.split('\n');
  let event = 'message';
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  if (!dataLines.length) return null;
  const data = dataLines.join('\n');
  let parsed = data;
  try { parsed = JSON.parse(data); } catch (e) { /* 纯文本 */ }
  return { event, data: parsed };
}

/* ------------------------------------------------------------ Agent --- */

async function loadAgents(refresh = false) {
  try {
    const data = await api(`/agents${refresh ? '?refresh=true' : ''}`);
    State.agents = data.agents;
    renderAgents();
    populateSelects();
    const healthy = State.agents.filter(a => a.health?.status === 'healthy').length;
    setPill('agent-count', `${State.agents.length} 个 Agent · ${healthy} 健康`,
      healthy ? 'ok' : (healthy === 0 ? 'err' : 'warn'));
    setPill('hub-status', '已连接', 'ok');
  } catch (err) {
    setPill('hub-status', '连接失败', 'err');
    $('agents-list').innerHTML = `<div class="empty">加载失败：${esc(err.message)}</div>`;
  }
}

function setPill(id, text, cls) {
  const el = $(id);
  el.className = 'pill ' + (cls || '');
  el.innerHTML = `<span class="dot"></span><span>${esc(text)}</span>`;
}

function renderAgents() {
  const box = $('agents-list');
  $('agents-count').textContent = `${State.agents.length}`;
  if (!State.agents.length) {
    box.innerHTML = '<div class="empty">没有已注册的 Agent</div>';
    return;
  }
  box.innerHTML = State.agents.map(a => {
    const st = a.health?.status || 'unknown';
    const skills = (a.skills || []).slice(0, 4)
      .map(s => `<span class="tag">${esc(s.name || s.id)}</span>`).join('');
    return `
      <div class="agent" data-id="${esc(a.id)}" title="${esc(a.health?.detail || '')}">
        <div class="row1">
          <span class="dot-s s-${st}"></span>
          <span class="name">${esc(a.name)}</span>
          <span class="type">${esc(a.type)}</span>
        </div>
        <div class="desc">${esc(a.description || '')}</div>
        <div class="skills">${skills}</div>
      </div>`;
  }).join('');
}

function populateSelects() {
  const opts = State.agents.map(a =>
    `<option value="${esc(a.id)}">${esc(a.name)}${a.health?.status !== 'healthy' ? '（' + (a.health?.status || '?') + '）' : ''}</option>`
  ).join('');
  $('agent-select').innerHTML = '<option value="">自动路由（按能力匹配）</option>' + opts;
  $('reviewer').innerHTML = '<option value="">不评审</option>' + opts;
  $('synthesizer').innerHTML = '<option value="">结构化汇总（不调用模型）</option>' + opts;
}

/* ------------------------------------------------------------- 模式 --- */

document.querySelectorAll('.mode').forEach(el => {
  el.onclick = () => {
    document.querySelectorAll('.mode').forEach(m => m.classList.remove('active'));
    el.classList.add('active');
    State.mode = el.dataset.mode;
    syncModeUi();
  };
});

function syncModeUi() {
  const m = State.mode;
  const show = (id, on) => $(id).classList.toggle('hidden', !on);
  show('wrap-agent', m === 'single');
  show('wrap-topk', m === 'broadcast' || m === 'delegate' || m === 'roundtable' || m === 'pipeline');
  show('wrap-rounds', m === 'roundtable');
  show('wrap-reviewer', m === 'delegate');
  show('wrap-synth', m === 'broadcast' || m === 'pipeline' || m === 'roundtable');
}

/* ------------------------------------------------------------ 时间线 --- */

function clearTimeline() {
  State.steps.clear();
  $('timeline').innerHTML = '';
}

function addResultCard(text, status) {
  const el = document.createElement('div');
  el.className = 'result-card';
  el.innerHTML = `
    <div class="hd">✅ 协同结果 <span class="badge ${status}">${esc(status)}</span></div>
    <div class="bd md">${renderMarkdown(text || '(无产出)')}</div>`;
  $('timeline').prepend(el);
}

function addStep(step) {
  const el = document.createElement('div');
  el.className = 'step';
  el.innerHTML = `
    <div class="step-head">
      <span class="spinner"></span>
      <span class="label">${esc(step.label || '执行')}</span>
      <span class="who">${esc(step.agentName || step.agentId || '')}</span>
      <span class="ms"></span>
    </div>
    <div class="step-body"></div>`;
  $('timeline').appendChild(el);
  State.steps.set(step.stepId || step.taskId || Math.random().toString(36), {
    el, body: el.querySelector('.step-body'), ms: el.querySelector('.ms'),
    head: el.querySelector('.step-head'), text: '',
    stepId: step.stepId,
  });
  el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  return State.steps.get(step.stepId);
}

function updateStep(stepId, patch) {
  const ref = State.steps.get(stepId);
  if (!ref) return;
  if (patch.append) {
    ref.text += patch.append;
    ref.body.textContent = ref.text;
    ref.body.scrollTop = ref.body.scrollHeight;
  }
  if (patch.state) {
    const ok = patch.state === 'completed';
    ref.head.querySelector('.spinner')?.remove();
    const mark = document.createElement('span');
    mark.textContent = ok ? '●' : '✗';
    mark.style.color = ok ? 'var(--ok)' : 'var(--err)';
    ref.head.prepend(mark);
  }
  if (patch.durationMs != null) ref.ms.textContent = patch.durationMs + 'ms';
  if (patch.error) {
    ref.body.textContent = (ref.text || '') + '\n\n[错误] ' + patch.error;
  }
}

/* ------------------------------------------------------------ 动态流 --- */

function pushFeed(kind, title, detail) {
  const feed = $('feed');
  if (feed.querySelector('.empty')) feed.innerHTML = '';
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const el = document.createElement('div');
  el.className = 'feed-item';
  el.innerHTML = `<span class="t">${t}</span><span class="m"><b>${esc(title)}</b> ${esc(detail || '')}</span>`;
  feed.prepend(el);
  while (feed.children.length > 80) feed.lastChild.remove();
}

function connectGlobalEvents() {
  const es = new EventSource('/events');
  es.addEventListener('hub', (e) => {
    let ev; try { ev = JSON.parse(e.data); } catch (err) { return; }
    const name = ev.agentId || '?';
    if (ev.event === 'task-created') {
      pushFeed('create', `${name}`, '收到新任务');
      setTimeout(loadTasks, 400);
    } else if (ev.event === 'task-progress') {
      const st = ev.state;
      if (st === 'completed' || st === 'failed' || st === 'canceled') {
        pushFeed('done', `${name}`, `任务 ${st}`);
        loadTasks();
      }
    } else if (ev.event === 'task-finished') {
      loadTasks();
    }
  });
  es.onerror = () => { /* EventSource 自动重连 */ };
}

/* -------------------------------------------------------------- 任务 --- */

async function loadTasks() {
  try {
    const data = await api('/tasks?limit=25');
    $('tasks-count').textContent = `${data.count}`;
    const box = $('tasks');
    if (!data.tasks.length) { box.innerHTML = '<div class="empty">暂无</div>'; return; }
    box.innerHTML = data.tasks.map(t => `
      <div class="task-row" data-id="${esc(t.id)}" title="${esc(t.id)}">
        <div class="r1">
          <span class="badge ${esc(t.state)}">${esc(t.state)}</span>
          <span class="agent-id">${esc(t.agentId || '-')}</span>
        </div>
        <div class="p">${esc((t.prompt || '').slice(0, 70))}</div>
      </div>`).join('');
  } catch (err) { /* 静默 */ }
}

/* ------------------------------------------------------------ 执行流 --- */

async function run() {
  const prompt = $('prompt').value.trim();
  if (!prompt) { $('prompt').focus(); return; }

  clearTimeline();
  State.running = true;
  $('btn-run').disabled = true;
  $('btn-stop').disabled = false;
  State.abort = new AbortController();

  try {
    if (State.mode === 'single') await runSingle(prompt);
    else await runCollab(prompt);
  } catch (err) {
    if (err.name !== 'AbortError') {
      const el = document.createElement('div');
      el.className = 'empty';
      el.style.color = 'var(--err)';
      el.textContent = '执行失败：' + err.message;
      $('timeline').appendChild(el);
    }
  } finally {
    State.running = false;
    $('btn-run').disabled = false;
    $('btn-stop').disabled = true;
    State.abort = null;
    loadTasks();
  }
}

async function runSingle(prompt) {
  const agentId = $('agent-select').value;
  const params = { message: { role: 'user', parts: [{ kind: 'text', text: prompt }] } };
  if (agentId) params.agentId = agentId;

  const ref = addStep({ stepId: 'single', label: '执行', agentName: agentId || '自动路由' });

  await streamSSE('/', {
    body: { jsonrpc: '2.0', id: '1', method: 'message/stream', params },
    signal: State.abort.signal,
    onEvent: ({ event, data }) => {
      if (event === 'error') {
        updateStep('single', { state: 'failed', error: data?.error?.message || JSON.stringify(data) });
        return;
      }
      if (data?.kind === 'artifact-update' && data.artifact?.name === 'output') {
        const parts = data.artifact.parts || [];
        const last = parts[parts.length - 1];
        if (last?.text) updateStep('single', { append: last.text });
      } else if (data?.kind === 'status-update') {
        const st = data.status?.state;
        if (['completed', 'failed', 'canceled'].includes(st)) {
          updateStep('single', { state: st === 'completed' ? 'completed' : 'failed' });
        }
      }
    },
  });
  // 补一次任务详情，拿到准确状态
  const t = await api('/tasks?limit=1').catch(() => null);
  if (t?.tasks?.[0]) updateStep('single', { durationMs: null, state: t.tasks[0].state });
}

async function runCollab(prompt) {
  const options = {};
  const topk = parseInt($('topk').value, 10);
  if (!Number.isNaN(topk)) options.topK = topk;
  const rounds = parseInt($('rounds').value, 10);
  if (!Number.isNaN(rounds)) options.rounds = rounds;
  if ($('synthesizer').value) options.synthesizer = $('synthesizer').value;
  if (State.mode === 'delegate' && $('reviewer').value) options.reviewer = $('reviewer').value;

  const run = await api('/collab', {
    method: 'POST',
    body: JSON.stringify({ mode: State.mode, prompt, options, blocking: false }),
  });
  pushFeed('create', State.mode, `协同 ${run.id.slice(0, 14)}…`);

  const stepMap = new Map();

  await streamSSE(`/collab/${run.id}/events`, {
    method: 'GET',
    signal: State.abort.signal,
    onEvent: ({ data }) => {
      const ev = data?.event;
      if (ev === 'plan') {
        const items = data.plan?.steps || data.plan?.stages || [];
        if (items.length) {
          pushFeed('plan', State.mode, items.map(i => i.agent).join(' → '));
        }
      } else if (ev === 'step-started') {
        const s = data.step;
        const ref = addStep(s);
        stepMap.set(s.stepId, s.stepId);
      } else if (ev === 'step-finished') {
        const s = data.step;
        updateStep(s.stepId, {
          state: s.state,
          durationMs: s.durationMs,
          error: s.error,
          append: s.output && !State.steps.get(s.stepId)?.text ? s.output : '',
        });
      } else if (ev === 'collab-finished') {
        if (data.result) addResultCard(data.result, data.error ? 'failed' : 'completed');
        if (data.error) {
          const el = document.createElement('div');
          el.className = 'empty';
          el.style.color = 'var(--err)';
          el.textContent = '协同失败：' + data.error;
          $('timeline').appendChild(el);
        }
      }
    },
  });
}

/* ------------------------------------------------------------ 工具 --- */

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** 极简 markdown：标题 / 粗体 / 行内代码 / 代码块 / 列表 */
function renderMarkdown(text) {
  const blocks = String(text).split(/```/);
  return blocks.map((chunk, i) => {
    if (i % 2 === 1) {
      const nl = chunk.indexOf('\n');
      const code = nl === -1 ? chunk : chunk.slice(nl + 1);
      return `<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`;
    }
    return esc(chunk)
      .replace(/^###### (.*)$/gm, '<h3>$1</h3>')
      .replace(/^### (.*)$/gm, '<h3>$1</h3>')
      .replace(/^## (.*)$/gm, '<h2>$1</h2>')
      .replace(/^# (.*)$/gm, '<h1>$1</h1>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/^([-*]) (.*)$/gm, '• $2');
  }).join('');
}

/* ------------------------------------------------------------ 事件 --- */

$('btn-run').onclick = run;
$('btn-refresh').onclick = () => { loadAgents(true); loadTasks(); };
$('btn-stop').onclick = () => {
  if (State.abort) State.abort.abort();
  pushFeed('stop', '用户', '已请求中断');
};
$('prompt').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); }
});

/* ------------------------------------------------------------ 启动 --- */

syncModeUi();
loadAgents(true);
loadTasks();
connectGlobalEvents();
setInterval(loadTasks, 15000);
