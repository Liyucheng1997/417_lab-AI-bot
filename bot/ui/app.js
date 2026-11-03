/* Bot 面板前端：接收 Python 推来的事件并渲染，调用 window.pywebview.api 发指令 */
(() => {
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const msgs = $('#messages'), scroller = $('#scroll'), input = $('#input'), app = $('#app');
  let api = null, state = {}, busy = false;
  let curBot = null, curRaw = '', curSteps = null;
  const steps = {};

  // ---------------- 小工具 ----------------
  function md(src) {
    const blocks = [];
    let s = src.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (_, lang, code) => { blocks.push(`<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
    s = esc(s);
    const inline = (t) => t
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    const lines = s.split('\n'), out = [];
    let list = null, para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join('<br>'))}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join('')}</${list.tag}>`); list = null; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if (/^\u0000\d+\u0000$/.test(line.trim())) { flushPara(); flushList(); out.push(line.trim()); continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flushPara(); flushList(); out.push(`<h3>${inline(m[2])}</h3>`); continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        flushPara(); flushList();
        const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        let html = `<table><tr>${cells(line).map((c) => `<th>${c}</th>`).join('')}</tr>`;
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { html += `<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join('')}</tr>`; i++; }
        i--; out.push(html + '</table>'); continue;
      }
      if ((m = line.match(/^\s*([-*•]|\d+\.)\s+(.*)/))) {
        flushPara();
        const tag = /\d/.test(m[1]) ? 'ol' : 'ul';
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(m[2]); continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      flushList(); para.push(line);
    }
    flushPara(); flushList();
    return out.join('').replace(/\u0000(\d+)\u0000/g, (_, n) => blocks[n]);
  }

  // ---------------- 先别收起：菜单开着、输入框里有字时告诉 Python ----------------
  let held = false;
  function syncHold() {
    const h = !!pop || !!document.querySelector('.viewer') || (document.activeElement === input && !!input.value.trim());
    if (h !== held && api) { held = h; api.set_hold(h); }
  }

  function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120; }
  function toBottom(force) { if (force || nearBottom()) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; }); }
  function add(node) { const stick = nearBottom(); $('#welcome').style.display = 'none'; if (avatar) avatar.pause(); msgs.appendChild(node); toBottom(stick); return node; }
  function toast(text, kind = '') {
    const t = el('div', `toast ${kind ? 'toast-' + kind : ''}`, esc(text));
    $('#toast-host').appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 2600);
  }
  function viewImage(src) {
    const v = el('div', 'viewer'); const img = el('img'); img.src = src; v.appendChild(img);
    v.onclick = () => { v.remove(); syncHold(); }; document.body.appendChild(v); syncHold();
  }

  function setBusy(b) { busy = b; app.classList.toggle('busy', b); if (avatar) avatar.setBusy(b); $('#btn-send').title = b ? '停止' : '发送'; }
  function setStatus(s) {
    const map = { idle: '空闲', thinking: '工作中', connecting: '连接中', error: '出错' };
    const st = $('#status'); st.className = `status ${s}`; st.querySelector('span').textContent = map[s] || s;
  }
  let typing = null;
  function showTyping() { if (!typing) typing = add(el('div', 'msg bot typing', '<i></i><i></i><i></i>')); }
  function hideTyping() { if (typing) { typing.remove(); typing = null; } }
  function endBot() {
    if (curBot) { curBot.classList.remove('streaming'); if (!curRaw.trim()) curBot.remove(); }
    curBot = null; curRaw = '';
  }

  // ---------------- 事件处理 ----------------
  const handlers = {
    status(ev) {
      setStatus(ev.state);
      setBusy(ev.state === 'thinking');
      if (ev.state === 'idle') { hideTyping(); endBot(); curSteps = null; }
    },
    text_delta(ev) {
      hideTyping(); curSteps = null;
      if (!curBot) { curBot = add(el('div', 'msg bot streaming')); curRaw = ''; }
      curRaw += ev.text;
      const stick = nearBottom();
      curBot.innerHTML = md(curRaw);
      toBottom(stick);
    },
    text(ev) { handlers.text_delta({ text: ev.text }); },
    thinking(ev) {
      hideTyping(); endBot();
      const d = el('details', 'thinking');
      d.innerHTML = `<summary>${esc(ev.text.split('\n')[0].slice(0, 60))}…</summary><div>${esc(ev.text)}</div>`;
      add(d);
    },
    tool_use(ev) {
      hideTyping(); endBot();
      if (!curSteps) curSteps = add(el('div', 'steps'));
      const [name, ...rest] = ev.summary.split('  ');
      const s = el('div', 'step running');
      s.innerHTML = `<div class="step-head"><i class="step-dot"></i><span class="step-name">${esc(label(name))}</span><span class="step-sum">${esc(rest.join(' '))}</span></div><div class="step-body"></div>`;
      s.querySelector('.step-head').onclick = () => s.classList.toggle('open');
      curSteps.appendChild(s); steps[ev.id] = s; toBottom();
    },
    tool_result(ev) {
      const s = steps[ev.id]; if (!s) return;
      s.classList.remove('running'); s.classList.add(ev.is_error ? 'err' : 'ok');
      const body = s.querySelector('.step-body');
      if (ev.text && ev.text.trim()) body.appendChild(el('pre', null, esc(ev.text)));
      for (const src of ev.images || []) {
        const img = el('img', 'shot'); img.src = src; img.onclick = (e) => { e.stopPropagation(); viewImage(src); };
        s.appendChild(img);
      }
      toBottom();
    },
    permission(ev) {
      hideTyping(); endBot(); curSteps = null;
      const c = el('div', 'perm');
      c.innerHTML = `<div class="perm-title">需要你确认：${esc(label(ev.short))}</div>
        <div class="perm-reason">${esc(ev.reason || '')}</div><pre>${esc(ev.detail)}</pre>
        <div class="perm-actions"><button class="btn btn-primary btn-sm" data-d="allow">允许</button>
        <button class="btn btn-sm" data-d="always">本次对话都允许</button>
        <button class="btn btn-sm" data-d="deny">拒绝</button></div>`;
      c.querySelectorAll('button').forEach((b) => b.onclick = () => {
        api.permission(ev.id, b.dataset.d);
        c.classList.add('done');
        c.appendChild(el('div', 'perm-verdict', { allow: '已允许', always: '已允许（本次对话不再询问同类操作）', deny: '已拒绝' }[b.dataset.d]));
      });
      add(c);
      toBottom(true);
    },
    result(ev) {
      hideTyping(); endBot(); curSteps = null;
      if (!ev.ok && ev.error && !/interrupt/i.test(ev.error)) add(el('div', 'notice err', esc(ev.error)));
    },
    rate_limit(ev) {
      if (ev.status === 'allowed') return;
      const pct = ev.utilization != null ? `，已用 ${Math.round(ev.utilization * 100)}%` : '';
      const when = ev.resets_at ? `，${new Date(ev.resets_at * 1000).toLocaleString()} 重置` : '';
      add(el('div', 'notice', esc(`订阅额度${ev.status === 'rejected' ? '已用完' : '接近上限'}${pct}${when}`)));
    },
    error(ev) { hideTyping(); endBot(); add(el('div', 'notice err', esc(ev.message))); },
    session() {},
    shown() { app.classList.remove('enter'); void app.offsetWidth; app.classList.add('enter'); if (window.fluid) fluid.resume(); if (avatar && $('#welcome').style.display !== 'none') avatar.resume(); },
    hidden() { if (window.fluid) fluid.pause(); if (avatar) avatar.pause(); },
    focus() { input.focus(); },
    suggestions(ev) { renderSuggestions(ev, true); },
    suggestions_failed() { $('#btn-reshuffle').classList.remove('spinning'); if (!$('#suggest').children.length || $('#suggest .skeleton')) renderSuggestions(null, true); },
    update(ev) {
      const bar = $('#update-bar'), title = bar.querySelector('b'), sub = bar.querySelector('small');
      bar.hidden = false;
      bar.classList.toggle('working', ev.state === 'downloading' || ev.state === 'installing');
      bar.querySelector('.update-progress i').style.width = ev.state === 'downloading' ? `${ev.pct}%` : '0';
      if (ev.state === 'available') {
        title.textContent = `发现新版本 v${ev.version}`;
        const notes = (ev.notes || '').replace(/[#*`>-]/g, ' ').replace(/\s+/g, ' ').trim();
        sub.textContent = notes || `${ev.size ? Math.round(ev.size / 1048576) + ' MB · ' : ''}更新后自动重启，对话和设置都会保留`;
        bar.title = ev.notes || '';
        $('#btn-update').textContent = '立即更新';
      } else if (ev.state === 'downloading') {
        title.textContent = `正在下载更新 ${ev.pct}%`;
        sub.textContent = '下载完会自动安装并重启';
      } else if (ev.state === 'installing') {
        title.textContent = '正在安装更新…';
        sub.textContent = 'Bot 会暂时关闭，几秒后自动重新打开';
      } else if (ev.state === 'error') {
        title.textContent = '更新没有完成';
        sub.textContent = ev.message;
        bar.title = ev.message;
        $('#btn-update').textContent = '重试';
      }
    },
  };

  const LABELS = {
    screenshot: '截图', click: '点击', type_text: '输入文字', press_keys: '按键', scroll: '滚动', drag: '拖动',
    move_mouse: '移动鼠标', open: '打开', list_windows: '查看窗口', focus_window: '切换窗口', inspect_window: '读取界面',
    click_element: '点击控件', get_clipboard: '读剪贴板', set_clipboard: '写剪贴板', wait: '等待', list_apps: '查找软件',
    list_processes: '查看进程', screen_info: '屏幕信息', PowerShell: 'PowerShell', Bash: '命令行', Read: '读文件',
    Write: '写文件', Edit: '改文件', Glob: '找文件', Grep: '搜索内容', WebSearch: '网页搜索', WebFetch: '读网页',
    TodoWrite: '计划', Agent: '子任务', Task: '子任务', ToolSearch: '加载工具', Skill: '技能',
  };
  const label = (n) => LABELS[n] || n;

  window.bot = {
    onEvents(list) { for (const ev of list) { try { (handlers[ev.type] || (() => {}))(ev); } catch (e) { console.error(e); } } },
    focusInput() { input.focus(); },
    // 截图工具的「AI」按钮 / OCR「发给 AI」：把内容放进输入框，光标停在末尾等主人补问题
    prefill(text) {
      input.value = input.value.trim() ? `${input.value.trimEnd()}\n${text}` : text;
      autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length);
      toast('已附上，补一句你的问题后按 Enter');
    },
    clear() {
      msgs.innerHTML = ''; hideTyping(); endBot(); curSteps = null;
      $('#welcome').style.display = ''; welcomeIn(); loadSuggestions(false);
      if (avatar) avatar.resume();
    },
  };

  // ---------------- 发送 ----------------
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px'; }
  function send(text) {
    text = (text ?? input.value).trim();
    if (!text || !api) return;
    if (busy) { toast('还在处理上一个任务，可以先点停止'); return; }
    add(el('div', 'msg user', esc(text)));
    showTyping();
    toBottom(true);
    input.value = ''; autosize(); syncHold();
    setBusy(true); setStatus('thinking');
    api.send(text);
  }
  input.addEventListener('input', () => { autosize(); syncHold(); });
  input.addEventListener('focus', syncHold);
  input.addEventListener('blur', syncHold);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    if (e.key === 'Escape') { if (busy) api.stop(); else api.hide(); }
  });
  $('#btn-send').onclick = () => (busy ? api.stop() : send());

  // ---------------- 欢迎页：问候 + AI 猜你想做 ----------------
  const FALLBACK = [
    { icon: '👀', text: '看看我屏幕上现在有什么' },
    { icon: '🗂️', text: '整理一下我的「下载」文件夹，按类型分好' },
    { icon: '💻', text: '查一下这台电脑的配置和磁盘空间' },
    { icon: '📝', text: '打开记事本，写一首关于秋天的小诗' },
  ];
  function greeting() {
    const h = new Date().getHours();
    const w = h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
    return state.nickname ? `${w}，${state.nickname}` : w;
  }
  function welcomeIn() {
    $('#greet').textContent = greeting();
    const w = $('#welcome'); w.classList.remove('in'); void w.offsetWidth; w.classList.add('in');
  }
  function renderSuggestions(data, animate) {
    const box = $('#suggest');
    const items = data && data.items && data.items.length ? data.items : FALLBACK;
    $('#hint').textContent = data && data.hint ? data.hint : '我可以操作这台电脑：运行命令、读写文件、打开软件、点击和输入';
    $('#hint').classList.toggle('ai', !!(data && data.hint));
    $('#btn-reshuffle').classList.remove('spinning');
    box.innerHTML = '';
    items.forEach((it, i) => {
      const c = el('button', `chip ${animate ? 'pop' : ''}`, `<span class="chip-ico">${esc(it.icon || '✨')}</span><span class="chip-text">${esc(it.text)}</span><span class="chip-go">→</span>`);
      c.style.setProperty('--i', i);
      c.onclick = () => send(it.text);
      box.appendChild(c);
    });
  }
  function skeleton() {
    $('#suggest').innerHTML = [0, 1, 2, 3].map((i) => `<div class="chip skeleton" style="--i:${i}"></div>`).join('');
  }
  async function loadSuggestions(refresh) {
    if (!api) return;
    if (refresh) $('#btn-reshuffle').classList.add('spinning');
    const cached = await api.suggestions(!!refresh);
    if (cached) { if (!refresh) renderSuggestions(cached, true); } else if (!refresh) skeleton();
  }
  $('#btn-reshuffle').onclick = () => loadSuggestions(true);

  // ---------------- 弹出菜单 ----------------
  let pop = null;
  function closePop() { if (pop) { const p = pop; pop = null; p.classList.add('out'); setTimeout(() => p.remove(), 120); syncHold(); } }
  document.addEventListener('mousedown', (e) => { if (pop && !pop.contains(e.target)) closePop(); });
  function popup(anchor, items, cls = '') {
    closePop();
    pop = el('div', `popover menu ${cls}`);
    for (const it of items) {
      if (it === '-') { pop.appendChild(el('div', 'menu-sep')); continue; }
      if (it.header) { pop.appendChild(el('div', 'menu-header', esc(it.header))); continue; }
      const m = el('div', `menu-item ${it.checked ? 'checked' : ''}`, it.html || `<span class="menu-label">${esc(it.label)}</span>${it.hint ? `<small>${esc(it.hint)}</small>` : ''}`);
      m.onclick = () => { closePop(); it.run(); };
      pop.appendChild(m);
    }
    document.body.appendChild(pop); syncHold();
    const r = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
    const up = r.top > window.innerHeight / 2;
    pop.style.top = (up ? Math.max(4, r.top - pr.height - 6) : r.bottom + 6) + 'px';
    pop.style.left = Math.max(4, Math.min(r.left, window.innerWidth - pr.width - 8)) + 'px';
    pop.style.transformOrigin = up ? 'bottom left' : 'top left';
  }

  function refreshPills() {
    $('#pill-model').textContent = state.models[state.model] || state.model;
    $('#pill-policy').textContent = state.policies[state.policy];
  }
  $('#pill-model').onclick = (e) => popup(e.currentTarget, Object.entries(state.models).map(([id, name]) => ({
    label: name, checked: id === state.model, run: () => { state.model = id; api.set_model(id); refreshPills(); toast(`已切换到 ${name}`); },
  })));
  $('#pill-policy').onclick = (e) => popup(e.currentTarget, [
    ['smart', '读取和日常操作直接做，删除/安装/改系统等先问你'], ['full', '所有操作都自动执行'], ['strict', '除了查看，每一步都问你'],
  ].map(([id, hint]) => ({
    html: `<span class="menu-label">${esc(state.policies[id])}<small>${esc(hint)}</small></span>`, checked: id === state.policy,
    run: () => { state.policy = id; api.set_policy(id); refreshPills(); },
  })));

  const THEMES = { auto: '跟随系统', light: '浅色', dark: '深色' };
  function applyTheme() {
    let t = 'auto'; try { t = localStorage.getItem('theme') || 'auto'; } catch (e) {}
    const dark = t === 'dark' || (t === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    return t;
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

  $('#btn-settings').onclick = (e) => {
    const theme = applyTheme();
    popup(e.currentTarget, [
      { header: '思考深度（新对话生效）' },
      ...state.efforts.map((x) => ({ label: { low: '快速', medium: '适中', high: '深入', xhigh: '很深入', max: '极致' }[x], hint: x, checked: x === state.effort, run: () => { state.effort = x; api.set_effort(x); toast('新对话生效'); } })),
      '-', { header: '外观' },
      ...Object.entries(THEMES).map(([k, v]) => ({ label: v, checked: k === theme, run: () => { try { localStorage.setItem('theme', k); } catch (e) {} applyTheme(); } })),
      { label: '墨迹颜色与形象大小…', run: openLook },
      '-',
      { label: '开机自动启动', checked: state.autostart, run: async () => { state.autostart = await api.set_autostart(!state.autostart); toast(state.autostart ? '已开启开机自启' : '已关闭开机自启'); } },
      '-', { header: '截图与识字' },
      { label: '编辑快捷短语', hint: 'r1 → [1]', run: () => api.sop_cmd('hotstrings-edit') },
      { label: '热键与工作流设置', run: () => api.sop_cmd('edit-config') },
      { label: '热键一览', run: () => api.sop_cmd('hotkeys') },
      '-',
      { label: '编辑长期记忆', run: () => api.open_path('memory') },
      { label: '打开配置文件', hint: '快捷键等', run: () => api.open_path('config') },
      { label: '打开程序文件夹', run: () => api.open_path('folder') },
      '-',
      { label: '检查更新', hint: `当前 v${state.version}`, run: checkUpdate },
    ], 'settings');
    pop.style.left = ''; pop.style.right = '8px';
  };

  // ---------------- 用量：订阅额度 + API ----------------
  const fmtTokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(n));
  const fmtUsd = (x) => `$${x.toFixed(2)}`;

  function usageSub(sub) {
    if (sub.error) return `<p class="usage-note">${esc(sub.error)}</p>`;
    return sub.windows.map((w) => {
      const level = w.pct >= 95 ? 'danger' : w.pct >= 80 ? 'warn' : '';
      return `<div class="meter ${level}"><div class="meter-top"><span>${esc(w.label)}</span><b>${Math.round(w.pct)}%</b></div>
        <div class="meter-bar"><i style="width:${Math.min(100, w.pct)}%"></i></div>
        ${w.resets ? `<small>重置于 ${esc(w.resets)}</small>` : ''}</div>`;
    }).join('');
  }

  function usageCredit(c) {
    const pct = c.amount ? (c.used / c.amount) * 100 : 0;
    const level = c.expired ? 'danger' : pct >= 90 || c.days_left <= 3 ? 'warn' : '';
    const when = c.expired ? `已于 ${c.end}到期` : `${c.start} – ${c.end} · 还剩 ${c.days_left} 天到期`;
    return `<div class="meter credit ${level}"><div class="meter-top"><span>赠送额度 剩余</span><b>${fmtUsd(c.remaining)}<small> / ${fmtUsd(c.amount)}</small></b></div>
      <div class="meter-bar"><i style="width:${Math.min(100, pct)}%"></i></div>
      <small>已用 ${fmtUsd(c.used)} · ${esc(when)}</small></div>`;
  }

  function usageApi(a) {
    const keyForm = `<div class="key-row"><input type="password" id="admin-key" placeholder="sk-ant-admin01-…" autocomplete="off" spellcheck="false">
      <button class="btn btn-primary btn-sm" id="admin-key-save">保存</button></div>
      <p class="usage-note">用 Console 里创建的 Admin API key（需要组织账号），只保存在本机的 Windows 凭据管理器里。<a data-url="admin_keys">怎么创建？</a></p>`;
    if (a.need_key) return `<p class="usage-note">填入 Admin API key 后显示本月花费、近 7 天 token 用量和缓存命中率。</p>${keyForm}`;
    if (a.error) return `<p class="usage-note err">${esc(a.error)}</p>${a.auth ? keyForm : ''}`;
    const max = Math.max(...a.daily, 1);
    const bars = a.daily.map((v) => `<i style="height:${Math.max(v ? 8 : 2, Math.round((v / max) * 100))}%" title="${fmtTokens(v)}"></i>`).join('');
    return `${a.credit ? usageCredit(a.credit) : ''}<div class="stats">
        <div><small>本月花费</small><b>${fmtUsd(a.spend)}</b></div>
        <div><small>近 7 天</small><b>${fmtTokens(a.tokens_7d)}</b><small>tokens</small></div>
        <div><small>缓存命中</small><b>${a.cache_hit == null ? '—' : a.cache_hit + '%'}</b></div>
      </div>
      <div class="spark">${bars}</div>
      <div class="usage-links"><a data-url="billing">余额与充值 ↗</a><a data-url="usage">用量明细 ↗</a><a id="admin-key-clear">移除密钥</a></div>`;
  }

  function renderUsage(u) {
    pop.innerHTML = `<div class="usage-sec"><div class="usage-h">Claude 订阅<small>本机 Claude Code</small></div>${usageSub(u.sub)}</div>
      <div class="usage-sec"><div class="usage-h">API<small>Claude Console</small></div>${usageApi(u.api)}</div>
      <div class="usage-foot"><span>更新于 ${esc(u.time)}</span><a id="usage-refresh">刷新</a></div>`;
    pop.querySelectorAll('[data-url]').forEach((a) => a.onclick = () => api.open_url(a.dataset.url));
    const refresh = pop.querySelector('#usage-refresh');
    refresh.onclick = () => openUsage(true);
    const save = pop.querySelector('#admin-key-save');
    if (save) {
      const inp = pop.querySelector('#admin-key');
      const submit = async () => {
        if (!inp.value.trim()) return;
        save.disabled = true; save.textContent = '验证中…';
        const r = await api.set_admin_key(inp.value);
        inp.value = '';
        if (r.ok) { toast('已保存，正在读取 API 用量', 'success'); openUsage(true); }
        else { toast(r.error, 'error'); save.disabled = false; save.textContent = '保存'; }
      };
      save.onclick = submit;
      inp.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
    }
    const clear = pop.querySelector('#admin-key-clear');
    if (clear) clear.onclick = async () => { await api.clear_admin_key(); toast('已移除 Admin API key'); openUsage(true); };
  }

  async function openUsage(force) {
    if (!pop || !pop.classList.contains('usage-pop')) {
      closePop();
      pop = el('div', 'popover usage-pop');
      document.body.appendChild(pop); syncHold();
      const r = $('#btn-usage').getBoundingClientRect();
      pop.style.top = `${r.bottom + 6}px`; pop.style.right = '8px'; pop.style.transformOrigin = 'top right';
    }
    const mine = pop;
    mine.innerHTML = '<div class="usage-loading">正在读取用量…</div>';
    const u = await api.usage(!!force);
    if (pop === mine) renderUsage(u);
  }
  $('#btn-usage').onclick = () => openUsage(false);

  async function checkUpdate() {
    toast('正在检查更新…');
    const r = await api.check_update();
    if (r.error) toast(r.error, 'error');
    else if (!r.available) toast(`已是最新版本 v${r.current}`, 'success');
    // 有新版本时 Python 会推 update 事件，显示提示条
  }
  $('#btn-update').onclick = () => {
    if (busy) { toast('等当前任务做完再更新，或先点停止'); return; }
    handlers.update({ state: 'downloading', pct: 0 });
    api.do_update();
  };
  $('#btn-update-close').onclick = () => { $('#update-bar').hidden = true; };

  $('#btn-history').onclick = async (e) => {
    const anchor = e.currentTarget;
    const rows = await api.history();
    if (!rows.length) { toast('还没有历史对话'); return; }
    popup(anchor, rows.map((r) => ({
      html: `<span>${esc(r.title)}</span><time>${esc(r.time)}</time>`,
      run: async () => {
        window.bot.clear();
        const list = await api.resume(r.id);
        for (const m of list) {
          if (m.role === 'user') add(el('div', 'msg user', esc(m.text)));
          else add(el('div', 'msg bot', md(m.text)));
        }
        add(el('div', 'notice', '已恢复这段对话，可以继续聊'));
        toBottom(true);
      },
    })), 'history-list');
  };
  $('#btn-new').onclick = () => { if (busy) api.stop(); api.new_chat(); window.bot.clear(); input.focus(); };
  $('#btn-hide').onclick = () => api.hide();
  function setPin(on) {
    $('#btn-pin').classList.toggle('active', on);
    $('#btn-pin').title = on ? '已固定（点一下改为自动收起）' : '自动收起：鼠标离开就滑到屏幕右边缘，碰一下边缘再出来（点一下固定）';
  }
  $('#btn-pin').onclick = async () => { const on = await api.toggle_pin(); setPin(on); toast(on ? '已固定，不会自动收起' : '自动收起：鼠标碰屏幕右边缘就出来'); };

  // ---------------- 3D 数字分身（同 liyucheng.me；加载失败就留着小时钟 Logo） ----------------
  let avatar = null;
  function applyLook(l) {
    if (window.fluid) fluid.set({ color: l.fluid_color, opacity: l.fluid_opacity });
    $('#avatar').style.setProperty('--avatar-size', `${l.avatar_size}px`);
  }
  async function loadAvatar() {
    try {
      const { initAvatar3D } = await import('./avatar.js');
      avatar = await initAvatar3D($('#avatar'), {
        modelUrl: 'avatar/avatar-kid.glb',
        badge: { url: 'avatar/badge-unimore.png', size: 0.017, offset: [0.055, 0.085, 0.108] },
      });
      $('#avatar').classList.add('on');
      avatar.setBusy(busy);
      if ($('#welcome').style.display === 'none') avatar.pause();
    } catch (e) { console.warn('3D 形象加载失败，用 Logo 代替', e); }
  }

  const SWATCHES = [['#af52de', '紫'], ['#007aff', '蓝'], ['#5ac8fa', '青'], ['#34c759', '绿'], ['#ff9500', '橙'], ['#ff2d55', '粉'], ['#1d1d1f', '墨']];
  function openLook() {
    closePop();
    const l = state.look;
    pop = el('div', 'popover look-pop');
    pop.innerHTML = `<div class="look-h">墨迹颜色</div>
      <div class="swatches">${SWATCHES.map(([c, n]) => `<button class="sw${c === l.fluid_color ? ' on' : ''}" data-c="${c}" title="${n}" style="--c:${c}"></button>`).join('')}
        <label class="sw custom" title="自定义颜色"><input type="color" value="${l.fluid_color}"></label></div>
      <div class="look-h">墨迹浓淡 <span id="op-v">${Math.round(l.fluid_opacity * 100)}%</span></div>
      <input type="range" id="op" min="0" max="100" value="${Math.round(l.fluid_opacity * 100)}">
      <div class="look-h">形象大小 <span id="sz-v">${l.avatar_size}px</span></div>
      <input type="range" id="sz" min="80" max="360" step="10" value="${l.avatar_size}">
      <p class="look-note">在面板空白处移动鼠标就能看到墨迹。透明度拉到 0 就关掉。</p>`;
    document.body.appendChild(pop); syncHold();
    const r = $('#btn-settings').getBoundingClientRect();
    pop.style.top = `${r.bottom + 6}px`; pop.style.right = '8px'; pop.style.transformOrigin = 'top right';
    let timer = 0;
    const save = () => { applyLook(l); clearTimeout(timer); timer = setTimeout(() => api.set_look(l), 300); };
    const pick = (c) => { l.fluid_color = c; pop.querySelectorAll('.sw').forEach((b) => b.classList.toggle('on', b.dataset.c === c)); save(); };
    pop.querySelectorAll('.sw[data-c]').forEach((b) => b.onclick = () => pick(b.dataset.c));
    pop.querySelector('input[type=color]').oninput = (e) => pick(e.target.value);
    pop.querySelector('#op').oninput = (e) => { l.fluid_opacity = e.target.value / 100; pop.querySelector('#op-v').textContent = `${e.target.value}%`; save(); };
    pop.querySelector('#sz').oninput = (e) => {
      l.avatar_size = +e.target.value; pop.querySelector('#sz-v').textContent = `${e.target.value}px`;
      if ($('#welcome').style.display === 'none') window.bot.clear();  // 在对话里调大小时回到欢迎页看效果
      save();
    };
  }

  // ---------------- 启动 ----------------
  applyTheme();
  window.addEventListener('pywebviewready', async () => {
    api = window.pywebview.api;
    state = await api.state();
    refreshPills();
    $('#hotkey-hint').textContent = `${state.hotkey.toUpperCase()} 或鼠标碰屏幕右边缘呼出我`;
    setPin(state.pinned);
    applyLook(state.look);
    loadAvatar();
    welcomeIn();
    loadSuggestions(false);
    handlers.shown();
    if (state.update) handlers.update({ state: 'available', ...state.update });
    input.focus();
  });
})();
