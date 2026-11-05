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
    const typing = (el) => document.activeElement === el && !!el.value.trim();
    const h = !!pop || !!document.querySelector('.viewer') || typing(input) || typing(sInput) || typing($('#ph-text'))
      || (typeof P !== 'undefined' && P.open && !!P.qr);  // 扫码配对时别收起
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
    focus() { if (S.open) sInput.focus(); else if (P.open) { if (document.activeElement !== $('#ph-text')) $('#ph-scroll').focus(); } else input.focus(); },
    search_open() { if (P.open) closePhone(); openSearch(); },
    phone(ev) {
      if (ev.event === 'error') toast(ev.message, 'error');
      else if (ev.event === 'started') toast(`${ev.name} 已投屏到电脑`, 'success');
      phoneRefresh();
    },
    phone_open(ev) { if (ev.serial) P.target = ev.serial; openPhone(ev.focus); },
    index(ev) { S.index = ev; renderStatus(); if (S.open && ev.state === 'ready' && S.q) runSearch(true); },
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
    phone_status: '手机状态', phone_screenshot: '手机截图', phone_inspect: '读取手机界面', phone_tap: '点手机',
    phone_tap_element: '点手机控件', phone_swipe: '手机滑动', phone_type_text: '手机打字', phone_press_key: '手机按键',
    phone_open_app: '打开手机 App', phone_list_apps: '查手机 App', phone_mirror: '手机投屏', phone_shell: '手机命令',
    TodoWrite: '计划', Agent: '子任务', Task: '子任务', ToolSearch: '加载工具', Skill: '技能',
  };
  const label = (n) => LABELS[n] || n;

  window.bot = {
    onEvents(list) { for (const ev of list) { try { (handlers[ev.type] || (() => {}))(ev); } catch (e) { console.error(e); } } },
    focusInput() { input.focus(); },
    // 截图工具的「AI」按钮 / OCR「发给 AI」：把内容放进输入框，光标停在末尾等主人补问题
    prefill(text, quiet) {
      if (P.open) closePhone();
      input.value = input.value.trim() ? `${input.value.trimEnd()}\n${text}` : text;
      autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length);
      if (!quiet) toast('已附上，补一句你的问题后按 Enter');
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
  const trim = (x) => String(+x.toFixed(x >= 100 ? 0 : 1));
  const fmtTokens = (n) => (n >= 1e8 ? `${trim(n / 1e8)} 亿` : n >= 1e4 ? `${trim(n / 1e4)} 万` : String(n));
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
    if (a.need_key) return `<p class="usage-note">填入 Admin API key 后显示最近 7 天花了多少钱、用了多少。</p>${keyForm}`;
    if (a.error) return `<p class="usage-note err">${esc(a.error)}</p>${a.auth ? keyForm : ''}`;
    const max = Math.max(...a.daily.map((d) => d.cost), 0.01);
    const bars = a.daily.map((d) => `<div title="${esc(d.label)}：花了 ${fmtUsd(d.cost)}，用了 ${fmtTokens(d.tokens)} token">
        <i style="height:${Math.max(d.cost ? 8 : 2, Math.round((d.cost / max) * 100))}%"></i><small>${esc(d.label)}</small></div>`).join('');
    return `${a.credit ? usageCredit(a.credit) : ''}<div class="usage-sub">最近 7 天</div><div class="stats">
        <div><small>花了</small><b>${fmtUsd(a.spend_7d)}</b><small>平均每天 ${fmtUsd(a.spend_7d / 7)}</small></div>
        <div title="token 是 AI 计算字数的单位，1 个汉字大约算 1～2 个 token"><small>用了</small><b>${fmtTokens(a.tokens_7d)}</b><small>个 token</small></div>
        <div title="重复发给 AI 的内容（比如同一段对话历史）会被缓存，再次读取只按约 1 折收费"><small>缓存复用</small><b>${a.cache_hit == null ? '—' : a.cache_hit + '%'}</b><small>这部分只收约 1 折</small></div>
      </div>
      <div class="spark-h">每天花费<small>鼠标移到柱子上看详情</small></div>
      <div class="spark">${bars}</div>
      <div class="usage-links"><a data-url="billing">去官网充值 ↗</a><a data-url="usage">看详细账单 ↗</a><a id="admin-key-clear" title="删除保存在本机的 Admin API key，之后不再显示 API 用量">删除密钥</a></div>`;
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

  // ---------------- 文件搜索（替代 Everything：只搜文档、图片、视频、音乐、压缩包、软件） ----------------
  const S = { open: false, q: '', cat: 'all', sort: 'smart', items: [], total: 0, more: false, sel: -1, gen: 0, loading: false, index: null };
  const CATS = [['all', '全部'], ['doc', '文档'], ['image', '图片'], ['video', '视频'], ['audio', '音乐'], ['archive', '压缩包'], ['app', '应用'], ['folder', '文件夹']];
  const SORTS = { smart: ['智能排序', '常用的、刚改过的、名字最像的排前面'], recent: ['最近修改', ''], name: ['名称', ''], size: ['大小', '大文件在前'] };
  const sInput = $('#s-input'), sList = $('#s-list');
  const iconCache = new Map();
  let iconQueue = [], iconBusy = false, sTimer = 0;

  function terms(q) {
    return (q.match(/"[^"]+"|\S+/g) || []).map((t) => t.replace(/"/g, '').toLowerCase())
      .filter((t) => t && !/^(ext:|!)/.test(t) && !/[*?\\/]/.test(t));
  }
  function highlight(name, ts) {
    const low = name.toLowerCase(), marks = new Array(name.length).fill(false);
    for (const t of ts) { let i = low.indexOf(t); while (i >= 0) { for (let k = i; k < i + t.length; k++) marks[k] = true; i = low.indexOf(t, i + t.length); } }
    let out = '', on = false;
    for (let i = 0; i < name.length; i++) {
      if (marks[i] !== on) { out += marks[i] ? '<mark>' : '</mark>'; on = marks[i]; }
      out += esc(name[i]);
    }
    return out + (on ? '</mark>' : '');
  }
  const fmtCount = (n) => (n >= 1e4 ? `${trim(n / 1e4)} 万` : n.toLocaleString());

  function renderCats() {
    $('#s-cats').innerHTML = CATS.map(([k, v]) => `<button class="s-cat${k === S.cat ? ' on' : ''}" data-k="${k}">${v}</button>`).join('');
    $('#s-cats').querySelectorAll('.s-cat').forEach((b) => b.onclick = () => { S.cat = b.dataset.k; renderCats(); runSearch(true); sInput.focus(); });
    $('#s-sort').textContent = SORTS[S.sort][0];
  }

  function renderStatus() {
    const st = S.index; const box = $('#s-status');
    if (!st) { box.textContent = ''; return; }
    if (st.state === 'ready') box.textContent = `已索引 ${fmtCount(st.files)} 个文件 · 实时更新${st.built ? ` · ${st.built} 全盘核对` : ''}`;
    else box.innerHTML = `<i class="spin"></i>${st.state === 'loading' ? '正在载入索引…' : `正在建立索引… 已找到 ${fmtCount(st.scanned || 0)} 个${st.files ? '（先搜旧的）' : ''}`}`;
  }

  function rowHtml(it, i) {
    const ts = terms(S.q);
    const cached = iconCache.get(it.path);
    const thumb = it.cat === 'image' || it.cat === 'video';
    const ico = cached ? `<img src="${cached}" alt="">` : `<span class="s-ext">${esc((it.ext || (it.cat === 'folder' ? '' : '?')).slice(0, 4))}</span>`;
    const side = [it.time, it.size].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join('');
    return `<div class="s-row${i === S.sel ? ' sel' : ''}" data-i="${i}">
      <div class="s-ico${thumb ? ' thumb' : ''}">${it.cat === 'folder' && !cached ? '<span class="s-ext">📁</span>' : ico}</div>
      <div class="s-main"><div class="s-name" title="${esc(it.path)}">${highlight(it.name, ts)}</div><div class="s-dir" title="${esc(it.dir_path)}">${esc(it.dir)}</div></div>
      <div class="s-side">${side}</div>
      <button class="s-act" title="打开所在文件夹（Ctrl+Enter）"><svg class="ico" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></button>
    </div>`;
  }

  function renderList(r, append) {
    if (!append) sList.scrollTop = 0;
    if (!S.items.length) {
      if (S.q) sList.innerHTML = `<div class="s-empty"><b>没有找到「${esc(S.q)}」</b>换个关键词，或者少打几个字试试。<br>只搜文档、图片、视频、音乐、压缩包和软件，<br>系统文件、程序文件和代码不在范围内。</div>`;
      else sList.innerHTML = `<div class="s-empty"><b>输入文件名就能找</b>按你打开的习惯排序：常用的、刚改过的在前面。
        <ul class="s-tips"><li><code>合同 2024</code> 多个词空格分开</li><li><code>*.pdf</code> <code>ext:docx;xlsx</code> 按类型</li>
        <li><code>下载\\ 发票</code> 限定在某个文件夹里</li><li><code>!备份</code> 排除</li>
        <li><code>↑↓</code> 选择 · <code>Enter</code> 打开 · <code>Ctrl+Enter</code> 打开位置 · <code>Tab</code> 切换类型</li></ul></div>`;
      return;
    }
    const head = !S.q ? '<div class="s-head">最近打开</div>' : '';
    const html = S.items.map(rowHtml).join('');
    sList.innerHTML = head + html + (S.more ? '<div class="s-more">往下滚动加载更多</div>' : '');
    queueIcons(S.items);
  }

  function setCount(r) {
    const c = $('#s-count');
    if (!S.q) c.textContent = r && r.items.length ? `最近打开的 ${r.items.length} 个` : '';
    else c.textContent = `${S.total.toLocaleString()} 个结果${r && r.ms != null ? ` · ${r.ms} ms` : ''}`;
  }

  async function runSearch(reset) {
    if (!api) return;
    const gen = ++S.gen;
    const offset = reset ? 0 : S.items.length;
    S.loading = true;
    const r = await api.search(S.q, S.cat, S.sort, offset);
    S.loading = false;
    if (gen !== S.gen) return;  // 已经又打了字
    S.index = { state: r.state, files: r.files, scanned: r.scanned, built: r.built }; renderStatus();
    if (reset) { S.items = r.items; S.sel = r.items.length ? 0 : -1; } else S.items = S.items.concat(r.items);
    S.total = r.total; S.more = !!r.more;
    renderList(r, !reset); setCount(r);
  }

  function queueIcons(items) {
    for (const it of items) if (!iconCache.has(it.path) && !iconQueue.some((x) => x.path === it.path)) iconQueue.push({ path: it.path, cat: it.cat, ext: it.ext });
    pumpIcons();
  }
  async function pumpIcons() {
    if (iconBusy || !iconQueue.length || !api) return;
    iconBusy = true;
    const batch = iconQueue.splice(0, 24);
    try {
      const got = await api.search_icons(batch);
      for (const it of batch) {
        if (!(it.path in got)) { iconQueue.push(it); continue; }  // 这次没来得及，下次再要
        iconCache.set(it.path, got[it.path] || '');
        if (!got[it.path]) continue;
        S.items.forEach((x, i) => {
          if (x.path !== it.path) return;
          const box = sList.querySelector(`.s-row[data-i="${i}"] .s-ico`);
          if (box) box.innerHTML = `<img src="${got[it.path]}" alt="">`;
        });
      }
      if (iconCache.size > 3000) iconCache.clear();
    } catch (e) { console.warn(e); }
    iconBusy = false;
    if (iconQueue.length && S.open) setTimeout(pumpIcons, 0);
  }

  function select(i, scroll = true) {
    if (!S.items.length) return;
    S.sel = Math.max(0, Math.min(S.items.length - 1, i));
    sList.querySelectorAll('.s-row.sel').forEach((r) => r.classList.remove('sel'));
    const row = sList.querySelector(`.s-row[data-i="${S.sel}"]`);
    if (row) { row.classList.add('sel'); if (scroll) row.scrollIntoView({ block: 'nearest' }); }
    if (S.sel >= S.items.length - 5 && S.more && !S.loading) runSearch(false);
  }

  async function openItem(it, how = 'open') {
    if (!it) return;
    const r = how === 'reveal' ? await api.search_reveal(it.path, S.q) : await api.search_open(it.path, S.q);
    if (r && !r.ok) toast(r.error, 'error');
  }

  function itemMenu(it, x, y) {
    const anchor = { getBoundingClientRect: () => ({ left: x, right: x, top: y, bottom: y }) };
    const isDir = it.cat === 'folder';
    popup(anchor, [
      { label: isDir ? '打开文件夹' : '打开', hint: 'Enter', run: () => openItem(it) },
      { label: '打开所在位置', hint: 'Ctrl+Enter', run: () => openItem(it, 'reveal') },
      '-',
      ...(isDir ? [] : [{ label: '复制文件', hint: '可直接粘贴到微信、文件夹', run: async () => { await api.search_copy(it.path, 'file'); toast('已复制文件，可以粘贴了', 'success'); } }]),
      { label: '复制路径', run: async () => { await api.search_copy(it.path, 'path'); toast('已复制路径', 'success'); } },
      { label: '让 Bot 处理这个文件…', run: () => { closeSearch(); window.bot.prefill(`${isDir ? '文件夹' : '文件'}：${it.path}\n`); } },
      '-',
      { label: '属性', run: () => api.search_props(it.path) },
    ]);
  }

  function openSearch() {
    if (P.open) closePhone();
    if (!S.open) {
      S.open = true; app.classList.add('searching'); renderCats();
      if (api) api.set_searching(true);
      runSearch(true);
    }
    sInput.focus(); sInput.select(); syncHold();
  }
  function closeSearch() {
    S.open = false; app.classList.remove('searching');
    if (api) api.set_searching(false);
    syncHold(); input.focus();
  }

  $('#btn-search').onclick = () => (S.open ? closeSearch() : openSearch());
  sInput.addEventListener('input', () => {
    S.q = sInput.value.trim(); $('#s-clear').hidden = !sInput.value;
    clearTimeout(sTimer); sTimer = setTimeout(() => runSearch(true), S.q.length === 1 ? 220 : 90);
    syncHold();
  });
  sInput.addEventListener('focus', syncHold);
  sInput.addEventListener('blur', syncHold);
  $('#s-clear').onclick = () => { sInput.value = ''; S.q = ''; $('#s-clear').hidden = true; runSearch(true); sInput.focus(); };
  sInput.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); select(S.sel + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); select(S.sel - 1); }
    else if (e.key === 'PageDown') { e.preventDefault(); select(S.sel + 8); }
    else if (e.key === 'PageUp') { e.preventDefault(); select(S.sel - 8); }
    else if (e.key === 'Enter') { e.preventDefault(); openItem(S.items[Math.max(0, S.sel)], e.ctrlKey ? 'reveal' : 'open'); }
    else if (e.key === 'Tab') {
      e.preventDefault();
      const i = CATS.findIndex(([k]) => k === S.cat);
      S.cat = CATS[(i + (e.shiftKey ? CATS.length - 1 : 1)) % CATS.length][0]; renderCats(); runSearch(true);
    } else if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      if (sInput.value) $('#s-clear').onclick(); else closeSearch();
    }
  });
  sList.addEventListener('click', (e) => {
    const row = e.target.closest('.s-row'); if (!row) return;
    const i = +row.dataset.i;
    if (e.target.closest('.s-act')) { openItem(S.items[i], 'reveal'); return; }
    select(i, false);
  });
  sList.addEventListener('dblclick', (e) => { const row = e.target.closest('.s-row'); if (row && !e.target.closest('.s-act')) openItem(S.items[+row.dataset.i]); });
  sList.addEventListener('contextmenu', (e) => {
    const row = e.target.closest('.s-row'); if (!row) return;
    e.preventDefault(); select(+row.dataset.i, false); itemMenu(S.items[+row.dataset.i], e.clientX, e.clientY);
  });
  sList.addEventListener('scroll', () => { if (S.more && !S.loading && sList.scrollTop + sList.clientHeight > sList.scrollHeight - 200) runSearch(false); });
  $('#search').addEventListener('keydown', (e) => { if (e.key === 'Escape' && e.target !== sInput) { e.preventDefault(); closeSearch(); } });
  $('#s-sort').onclick = (e) => popup(e.currentTarget, Object.entries(SORTS).map(([k, [name, hint]]) => ({
    html: `<span class="menu-label">${esc(name)}${hint ? `<small>${esc(hint)}</small>` : ''}</span>`, checked: k === S.sort,
    run: () => { S.sort = k; renderCats(); runSearch(true); sInput.focus(); },
  })));
  $('#s-rebuild').onclick = async () => { await api.search_rebuild(); toast('开始重新扫描，大约半分钟，期间照常可以搜'); S.index = { ...(S.index || {}), state: 'scanning', scanned: 0 }; renderStatus(); };


  // ---------------- 手机投屏（Android：scrcpy；iPhone：AirPlay 只能看） ----------------
  var P = { open: false, st: null, timer: 0, tab: 'qr', qr: null, target: null, busy: {} };
  const phoneIcon = (cls = '') => `<i class="dev-ico ${cls}"></i>`;

  function phoneDevices() { return (P.st && P.st.devices || []).filter((d) => d.state === 'device'); }
  function phoneTarget() {
    const ready = phoneDevices();
    if (P.target && ready.some((d) => d.serial === P.target)) return P.target;
    const live = ready.find((d) => P.st.mirrors.includes(d.serial));
    return (live || ready[0] || {}).serial || null;
  }

  async function phoneRefresh() {
    if (!api) return;
    try { P.st = await api.phone_state(); } catch (e) { console.warn(e); return; }
    $('#btn-phone').classList.toggle('live', P.st.mirrors.length > 0);
    if (P.open) renderPhone();
  }
  function phonePoll() {
    clearTimeout(P.timer);
    if (!P.open) return;
    const fast = (P.st && P.st.download && 'pct' in P.st.download) || (P.st && P.st.pairing && ['waiting', 'pairing', 'connecting'].includes(P.st.pairing.state));
    P.timer = setTimeout(async () => { await phoneRefresh(); phonePoll(); }, fast ? 700 : 1600);
  }

  function renderPhone() {
    const st = P.st; if (!st) return;
    // 首次使用：下载投屏组件
    const setup = $('#ph-setup'), main = $('#ph-main');
    if (!st.tools) {
      main.hidden = true; setup.hidden = false;
      const dl = st.download;
      let body;
      if (dl && 'pct' in dl) body = `<div class="ph-bar"><i style="width:${dl.pct}%"></i></div><p>正在下载… ${dl.mb || 0} MB（${dl.pct}%）</p>`;
      else body = `${dl && dl.error ? `<p style="color:var(--danger)">${esc(dl.error)}</p>` : ''}<button class="btn btn-primary" id="ph-dl">${dl && dl.error ? '重试' : '下载投屏组件（约 11 MB）'}</button>`;
      setup.innerHTML = `<div class="ph-card ph-hero">${phoneIcon()}<b>把手机搬到电脑上</b>
        <p>手机画面实时显示在电脑上，用鼠标点、用键盘打字，声音也从电脑出来。文件拖进去就传到手机，两边剪贴板自动同步。</p><p>第一次用要下载开源投屏组件 scrcpy（含 adb）。</p>${body}</div>`;
      const b = $('#ph-dl'); if (b) b.onclick = async () => { await api.phone_setup(); await phoneRefresh(); phonePoll(); };
      return;
    }
    setup.hidden = true; main.hidden = false;
    renderDevices(); renderPairing(); renderKnown(); renderIos(); renderPhoneSettings();
    $('#ph-foot').innerHTML = `投屏组件：scrcpy ${esc(st.version || '')}（开源，Genymobile） · <a id="ph-shots">打开手机截图文件夹</a>`;
    $('#ph-shots').onclick = () => api.phone_open_shots();
  }

  function deviceCard(d) {
    const live = P.st.mirrors.includes(d.serial);
    const sub = [];
    if (d.state === 'device') {
      if (d.os) sub.push(esc(d.os));
      sub.push(`<span class="tag">${d.wireless ? '无线' : 'USB'}</span>`);
      if (d.battery != null) sub.push(`电量 ${d.battery}%`);
      if (live) sub.push('<span class="tag live">投屏中</span>');
    } else if (d.state === 'unauthorized') sub.push('<span class="tag warn">等待手机授权</span>');
    else sub.push(`<span class="tag warn">${d.state === 'offline' ? '连接断开' : esc(d.state)}</span>`);
    const busy = P.busy[d.serial];
    let actions = '', hint = '';
    if (d.state === 'device') {
      actions = live
        ? `<button class="btn btn-sm btn-primary" data-a="focus">回到投屏窗口</button><button class="btn btn-sm" data-a="stop">结束投屏</button>`
        : `<button class="btn btn-sm btn-primary" data-a="mirror">${busy === 'mirror' ? '<i class="ph-busy"></i>正在打开' : '投屏'}</button>`;
      actions += `<button class="btn btn-sm" data-a="shot">截图</button>`;
      if (!d.wireless) actions += `<button class="btn btn-sm" data-a="wireless" title="打开手机的无线调试端口，之后可以拔掉数据线">${busy === 'wireless' ? '<i class="ph-busy"></i>切换中' : '切换到无线'}</button>`;
      else actions += `<button class="btn btn-sm" data-a="disconnect">断开</button>`;
    } else if (d.state === 'unauthorized') {
      hint = '解锁手机，在弹出的「允许 USB 调试吗？」里点<b>允许</b>（建议勾上「一律允许使用这台计算机进行调试」）。没弹出的话拔插一下数据线。';
    } else {
      hint = d.wireless ? '无线连接断开了。确认手机和电脑在同一个 Wi-Fi，再在下面重新连接。' : '拔插一下数据线；还不行就在开发者选项里关掉再打开 USB 调试。';
      actions = d.wireless ? `<button class="btn btn-sm" data-a="disconnect">移除</button>` : '';
    }
    return `<div class="ph-card" data-serial="${esc(d.serial)}"><div class="dev">${phoneIcon(live ? 'on' : d.state === 'device' ? '' : 'wait')}
      <div class="dev-main"><div class="dev-name" title="${esc(d.serial)}">${esc(d.name)}</div><div class="dev-sub">${sub.join('')}</div></div></div>
      ${actions ? `<div class="dev-actions">${actions}</div>` : ''}${hint ? `<div class="dev-hint">${hint}</div>` : ''}</div>`;
  }

  function renderDevices() {
    const box = $('#ph-devices'), list = P.st.devices || [];
    const sig = JSON.stringify([list, P.st.mirrors, P.busy, P.st.nearby]);
    if (box.dataset.sig === sig) return;  // 没变就不重画（免得按钮闪）
    box.dataset.sig = sig;
    box.innerHTML = list.length ? list.map(deviceCard).join('')
      : `<div class="ph-card ph-empty"><b>还没有连上手机</b>${(P.st.nearby || []).length ? `同一个 Wi-Fi 里发现 ${P.st.nearby.length} 台开着无线调试的手机（${P.st.nearby.map((n) => esc(n.host)).join('、')}），用下面的二维码配对一次就能用`
        : '手机打开「无线调试」，用下面的二维码配对一次，以后自动连上'}${P.st.error ? `<br><span style="color:var(--danger)">${esc(P.st.error)}</span>` : ''}</div>`;
    box.querySelectorAll('[data-a]').forEach((b) => b.onclick = () => phoneAction(b.closest('[data-serial]').dataset.serial, b.dataset.a));
    const ready = phoneDevices();
    $('#ph-send').hidden = !ready.length;
    const t = phoneTarget();
    const name = (ready.find((d) => d.serial === t) || {}).name;
    $('#ph-text').placeholder = ready.length > 1 ? `打字发到「${name}」当前的输入框（中文也行），Enter 发送` : '在这里打字（中文也行），按 Enter 发到手机当前的输入框';
  }

  async function phoneAction(serial, a) {
    P.target = serial;
    if (a === 'mirror') {
      P.busy[serial] = 'mirror'; renderDevices();
      const r = await api.phone_mirror(serial);
      if (r && !r.ok) toast(r.error, 'error');
      setTimeout(() => { delete P.busy[serial]; phoneRefresh(); }, 2500);
    } else if (a === 'focus') await api.phone_mirror(serial);
    else if (a === 'stop') { await api.phone_stop(serial); phoneRefresh(); }
    else if (a === 'shot') {
      const r = await api.phone_shot(serial);
      toast(r.ok ? '截图已保存并复制，可以直接粘贴' : r.error, r.ok ? 'success' : 'error');
    } else if (a === 'wireless') {
      P.busy[serial] = 'wireless'; renderDevices();
      const r = await api.phone_wireless(serial);
      delete P.busy[serial];
      toast(r.ok ? '已切换到无线，现在可以拔掉数据线了' : r.error, r.ok ? 'success' : 'error');
      phoneRefresh();
    } else if (a === 'disconnect') { await api.phone_disconnect(serial); phoneRefresh(); }
  }

  async function phoneSendText() {
    const box = $('#ph-text'), text = box.value;
    const serial = phoneTarget();
    if (!text.trim() || !serial) return;
    const r = await api.phone_text(serial, text);
    if (r.ok) { box.value = ''; toast('已发到手机', 'success'); } else {
      if (r.need_mirror) { toast('中文要通过投屏窗口粘贴，正在打开投屏…'); await api.phone_mirror(serial); setTimeout(() => phoneSendText(), 3500); } else toast(r.error, 'error');
    }
    syncHold();
  }
  $('#ph-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); phoneSendText(); }
  });
  $('#ph-text').addEventListener('input', syncHold);
  $('#ph-text-go').onclick = phoneSendText;

  // 连接方式
  function phoneTab(t) {
    P.tab = t;
    $('#ph-tabs').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.t === t));
    document.querySelectorAll('.ph-tab').forEach((x) => { x.hidden = x.dataset.t !== t; });
    if (t === 'qr' && !P.qr) startQr();
    if (t !== 'qr' && P.qr) { api.phone_pair_cancel(); P.qr = null; $('#ph-qr').innerHTML = ''; }
    syncHold();
  }
  $('#ph-tabs').querySelectorAll('button').forEach((b) => b.onclick = () => phoneTab(b.dataset.t));
  async function startQr() {
    $('#ph-qr').innerHTML = '<div class="qr-msg"><i class="ph-busy"></i>正在生成二维码…</div>';
    P.qr = await api.phone_pair_qr();
    $('#ph-qr').innerHTML = `<img src="${P.qr.qr}" alt="配对二维码"><div class="qr-msg" id="qr-msg">用手机的「使用二维码配对设备」扫这个码</div>`;
    phoneRefresh(); phonePoll();
  }
  function renderPairing() {
    if (!P.qr && P.tab === 'qr' && !$('#ph-qr').children.length) {
      $('#ph-qr').innerHTML = '<button class="btn btn-sm" id="qr-show">显示配对二维码</button>';
      $('#qr-show').onclick = startQr;
    }
    const p = P.st.pairing, msg = $('#qr-msg');
    if (!p || !msg || !P.qr) return;
    msg.className = `qr-msg ${p.state === 'error' ? 'err' : p.state === 'done' ? 'ok' : ''}`;
    const spin = ['pairing', 'connecting'].includes(p.state) ? '<i class="ph-busy"></i>' : '';
    msg.innerHTML = spin + esc(p.message) + (p.state === 'error' ? ' <a id="qr-retry" style="cursor:pointer;color:var(--accent-text)">刷新</a>' : '');
    const retry = $('#qr-retry'); if (retry) retry.onclick = startQr;
    if (p.state === 'done' && !P.qrDone) {
      P.qrDone = true; toast('手机已无线连接', 'success');
      setTimeout(() => { P.qrDone = false; if (P.qr) { P.qr = null; api.phone_pair_cancel(); $('#ph-qr').innerHTML = ''; } }, 1500);
    }
  }
  $('#ph-pair-go').onclick = async () => {
    const b = $('#ph-pair-go'); b.innerHTML = '<i class="ph-busy"></i>正在配对'; b.disabled = true;
    const r = await api.phone_pair_code($('#ph-pair-addr').value, $('#ph-pair-code').value);
    b.textContent = '配对并连接'; b.disabled = false;
    if (r.ok) { toast(r.serial ? '配对成功，已连接' : '配对成功。再在「直接连接」里填无线调试页面上的 IP 地址和端口', 'success'); $('#ph-pair-code').value = ''; }
    else toast(r.error, 'error');
    phoneRefresh();
  };
  async function phoneConnect(addr) {
    if (!addr.trim()) return;
    const b = $('#ph-connect'); b.innerHTML = '<i class="ph-busy"></i>连接中'; b.disabled = true;
    const r = await api.phone_connect(addr);
    b.textContent = '连接'; b.disabled = false;
    toast(r.ok ? '已连接' : r.error, r.ok ? 'success' : 'error');
    phoneRefresh();
  }
  $('#ph-connect').onclick = () => phoneConnect($('#ph-addr').value);
  $('#ph-addr').addEventListener('keydown', (e) => { if (e.key === 'Enter') phoneConnect($('#ph-addr').value); });
  function renderKnown() {
    const box = $('#ph-known'), rows = P.st.known || [];
    const online = new Set((P.st.devices || []).map((d) => d.serial));
    const sig = JSON.stringify([rows, [...online]]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.innerHTML = rows.length ? `<div class="ph-form-h" style="margin-top:6px"><small>以前连过的</small></div>` + rows.map((k) => `<div class="known" data-addr="${esc(k.addr)}">
      <span>${esc(k.name)}</span><small>${esc(k.addr)}</small>${online.has(k.addr) ? '<span class="tag live" style="flex:none">已连接</span>' : '<a data-k="go">连接</a>'}<a data-k="forget" title="从列表里删掉">忘记</a></div>`).join('') : '';
    box.querySelectorAll('[data-k]').forEach((a) => a.onclick = async () => {
      const addr = a.closest('[data-addr]').dataset.addr;
      if (a.dataset.k === 'go') phoneConnect(addr); else { await api.phone_forget(addr); phoneRefresh(); }
    });
  }

  function renderIos() {
    const box = $('#ph-ios'), sig = String(P.st.iphone);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.innerHTML = `<p>苹果不允许电脑控制 iPhone（这个功能只在 Mac 上有），在 Windows 上只能用 <b>AirPlay 屏幕镜像</b>把画面投过来看，不能用鼠标点。</p>
      <ol class="ph-steps"><li>iPhone 和电脑连同一个 Wi-Fi</li><li>电脑上打开 AirPlay 接收端${P.st.iphone ? '' : '（第一次要先安装开源的 uxplay-windows）'}</li>
      <li>iPhone 从右上角下拉控制中心 → <b>屏幕镜像</b> → 选这台电脑</li></ol>
      <button class="btn btn-sm" id="ph-ios-go">${P.st.iphone ? '打开 AirPlay 接收端' : '下载 uxplay-windows（GitHub）↗'}</button>`;
    $('#ph-ios-go').onclick = async () => {
      const r = await api.phone_iphone();
      toast(r.started ? '已打开接收端，在 iPhone 控制中心点「屏幕镜像」' : '已在浏览器打开下载页，装好后回来点这里', 'success');
    };
  }

  const MAX_SIZES = [[0, '原画（最清楚）'], [1920, '1920'], [1280, '1280（省流量）'], [1024, '1024（最流畅）']];
  function renderPhoneSettings() {
    const box = $('#ph-settings'), s = P.st.settings, sig = JSON.stringify(s);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const sw = (k, label, sub) => `<div class="set-row"><label>${label}<small>${sub}</small></label><button class="switch${s[k] ? ' on' : ''}" data-k="${k}" role="switch" aria-checked="${!!s[k]}"></button></div>`;
    box.innerHTML = sw('audio', '声音从电脑播放', '需要 Android 11 及以上；关掉则声音留在手机上')
      + sw('screen_off', '投屏时手机熄屏', '省电、防偷看，电脑上照常操作')
      + sw('on_top', '投屏窗口置顶', '一直浮在其他窗口上面')
      + `<div class="set-row"><label>画质<small>卡顿的话调低一点</small></label><select data-k="max_size">${MAX_SIZES.map(([v, n]) => `<option value="${v}"${+s.max_size === v ? ' selected' : ''}>${n}</option>`).join('')}</select></div>`
      + `<div class="set-row"><label>键盘打字<small>${s.keyboard === 'uhid' ? '电脑键盘当成手机的实体键盘，用手机输入法打中文' : '用电脑的输入法直接打字，中文也行'}</small></label>
        <select data-k="keyboard"><option value="sdk"${s.keyboard !== 'uhid' ? ' selected' : ''}>电脑输入</option><option value="uhid"${s.keyboard === 'uhid' ? ' selected' : ''}>手机输入法</option></select></div>`;
    box.querySelectorAll('.switch').forEach((b) => b.onclick = async () => { P.st.settings = await api.phone_set(b.dataset.k, !s[b.dataset.k]); renderPhoneSettings(); });
    box.querySelectorAll('select').forEach((x) => x.onchange = async () => { P.st.settings = await api.phone_set(x.dataset.k, x.value); renderPhoneSettings(); });
  }

  function openPhone(focus) {
    if (S.open) closeSearch();
    if (!P.open) { P.open = true; app.classList.add('phoning'); }
    phoneRefresh().then(() => {
      phonePoll();
      if (P.tab === 'qr' && !P.qr && P.st && P.st.tools && !(P.st.devices || []).length) startQr();
      if (focus === 'text') { const t = $('#ph-text'); if (!$('#ph-send').hidden) { t.focus(); } } });
    syncHold();
  }
  function closePhone() {
    P.open = false; app.classList.remove('phoning'); clearTimeout(P.timer);
    if (P.qr) { api.phone_pair_cancel(); P.qr = null; $('#ph-qr').innerHTML = ''; }
    syncHold(); input.focus();
  }
  $('#btn-phone').onclick = () => (P.open ? closePhone() : openPhone());
  $('#ph-close').onclick = closePhone;
  $('#phone').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePhone(); } });

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
    $('#btn-search').title = `搜索文件${state.search_hotkey ? `（${state.search_hotkey.toUpperCase()}）` : ''}`;
    S.index = await api.search_status();
    phoneRefresh();
    input.focus();
  });
})();
