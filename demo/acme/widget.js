// Acme storefront + support chat widget. Plain JS, no build step.
// The widget talks only to the support API (/v1/...). Acme's own login lives at /demo/login.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === 'class' ? (el.className = v) : k.startsWith('on') ? el.addEventListener(k.slice(2), v) : el.setAttribute(k, v);
    for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ''));
    return el;
  };
  const post = async (url, body) => {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error('request failed'), { status: r.status, data });
    return data;
  };

  // ---------- storefront ----------
  const emoji = (n) => /boot/i.test(n) ? '🥾' : /sandal/i.test(n) ? '🩴' : /loafer|oxford|flat/i.test(n) ? '👞' : '👟';
  fetch('/demo/catalog').then(r => r.json()).then(items => {
    $('#grid').replaceChildren(...items.map(p => h('div', { class: 'item' },
      h('div', { class: 'shoe' }, emoji(p.name)),
      h('div', { class: 'body' }, h('h3', {}, p.name), h('div', { class: 'meta' }, `${p.colors.length} colors · ${p.widths.join(', ')} width`),
        h('div', { class: 'price' }, `$${Number(p.price).toFixed(2)}`)))));
  });

  // ---------- Acme's own login (the "already logged-in site") ----------
  let user = null; // {name, customer_id}
  const setUser = (u) => {
    user = u;
    const b = $('#account');
    b.textContent = u ? `Hi, ${u.name}` : 'Sign in';
    b.classList.toggle('on', !!u);
    const who = $('#who');
    if (who) { who.hidden = !u; who.textContent = u ? `Signed in to Acme as ${u.name}` : ''; }
  };
  let loginDone = null;
  $('#account').addEventListener('click', () => user ? setUser(null) : openLogin());
  $('#login-cancel').addEventListener('click', () => { $('#login').hidden = true; loginDone = null; });
  document.querySelectorAll('.acct').forEach(b => b.addEventListener('click', async () => {
    const res = await post('/demo/login', { customer_id: b.dataset.id });
    setUser({ name: res.name, customer_id: res.customer_id });
    $('#login').hidden = true;
    const cb = loginDone; loginDone = null;
    if (cb) cb(res.token);
  }));
  const openLogin = (cb) => { loginDone = cb || null; $('#login').hidden = false; };
  // A fresh token is minted on demand, like a site backend endpoint would.
  const freshToken = async () => (await post('/demo/login', { customer_id: user.customer_id })).token;

  // ---------- chat widget ----------
  const root = h('div', { id: 'chat-root' });
  const launcher = h('button', { class: 'launcher', 'aria-label': 'Open support chat', onclick: () => open(true) }, '💬');
  const log = h('div', { class: 'log', 'aria-live': 'polite' });
  const input = h('input', { type: 'text', placeholder: 'Ask about orders, sizes, returns…', 'aria-label': 'Message', maxlength: 2000 });
  const sendBtn = h('button', { class: 'btn pri', type: 'submit' }, 'Send');
  const who = h('div', { class: 'who', id: 'who', hidden: '' });
  const inspector = h('div', { class: 'inspector', hidden: '' },
    h('h5', {}, 'Pipeline inspector'), h('div', { class: 'sub' }, 'What the agent did on each turn (demo only)'),
    h('div', { id: 'turns' }, h('div', { class: 'empty' }, 'Send a message to see the stages.')));
  const panel = h('div', { class: 'panel', hidden: '' },
    h('div', { class: 'phead' }, h('div', { class: 'avatar' }, 'A'),
      h('div', { class: 't' }, h('b', {}, 'Acme Support'), h('small', {}, 'AI assistant · replies in seconds')),
      h('button', { onclick: () => { inspector.hidden = !inspector.hidden; } }, 'Pipeline'),
      h('button', { onclick: () => open(false), 'aria-label': 'Close' }, '✕')),
    who, log,
    h('form', { class: 'compose', onsubmit: (e) => { e.preventDefault(); const t = input.value.trim(); if (t) { input.value = ''; send(t); } } }, input, sendBtn),
    h('div', { class: 'foot' }, "You're chatting with an AI assistant. Don't share card numbers or passwords."));
  root.append(inspector, panel, launcher);
  document.body.append(root);
  if (new URLSearchParams(location.search).has('inspector')) inspector.hidden = false;

  let sessionId = null, parkedAuthCard = null;
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  const add = (el) => { log.append(el); scroll(); return el; };
  const sys = (t) => add(h('div', { class: 'msg sys' }, t));
  const typing = () => add(h('div', { class: 'typing' }, h('i'), h('i'), h('i')));

  const rich = (text) => { // escape, then a tiny subset of markdown: **bold**, "- " lists, links
    const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/(https?:\/\/[^\s)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    const out = []; let list = false;
    for (const line of esc.split('\n')) {
      const m = line.match(/^\s*[-*]\s+(.*)/);
      if (m) { if (!list) { out.push('<ul>'); list = true; } out.push(`<li>${inline(m[1])}</li>`); }
      else { if (list) { out.push('</ul>'); list = false; } out.push(inline(line) + '<br>'); }
    }
    if (list) out.push('</ul>');
    return out.join('').replace(/(<br>)+$/, '');
  };
  const bot = (text) => { const el = h('div', { class: 'msg bot' }); el.innerHTML = rich(text); return add(el); };
  const chips = (labels) => { const c = add(h('div', { class: 'chips' }, labels.map(l => h('button', { class: 'chip', onclick: () => { c.remove(); send(l); } }, l)))); };

  async function open(show) {
    panel.hidden = !show; launcher.hidden = show;
    if (show && !sessionId) {
      sessionId = (await post('/v1/sessions')).session_id;
      bot("Hi! I'm Acme's AI support assistant. I can help with sizing, orders, returns and exchanges. What do you need?");
      chips(['Track my order', 'Which size should I get?', 'Start a return', 'Talk to a person']);
    }
    if (show) input.focus();
  }
  window.acmeChat = { open, send: (t) => send(t), get user() { return user; } };

  async function send(text) {
    add(h('div', { class: 'msg me' }, text));
    const t = typing();
    try { handle(await post(`/v1/sessions/${sessionId}/messages`, { text }), text, t); }
    catch { t.remove(); bot('Sorry, something went wrong on my side. Please try again.'); }
  }

  function handle(reply, userText, typingEl) {
    typingEl?.remove();
    bot(reply.text);
    const p = reply.payload || {};
    if (p.type === 'quick_replies' && p.quick_replies?.length) chips(p.quick_replies.map(q => q.label));
    if (p.type === 'confirm') confirmCard(p);
    if (p.type === 'sign_in') signInCard();
    if (p.type === 'action_done') doneCard(p);
    if (p.type === 'handoff') add(h('div', { class: 'card2 hand' }, h('h4', {}, 'Handed to our team'), h('p', {}, 'A person will follow up. Your conversation has been passed on, so you won\'t need to repeat anything.')));
    inspect(reply, userText);
  }

  function confirmCard(p) {
    const yes = h('button', { class: 'btn pri' }, 'Confirm'), no = h('button', { class: 'btn sec' }, 'Cancel');
    const card = add(h('div', { class: 'card2' }, h('h4', {}, 'Please confirm'), h('div', { class: 'row' }, yes, no)));
    const answer = (accept) => async () => {
      yes.disabled = no.disabled = true; const t = typing();
      try { const r = await post(`/v1/sessions/${sessionId}/actions/${p.action_id}`, { accept }); card.remove(); handle(r, accept ? 'Confirm' : 'Cancel', t); }
      catch { t.remove(); yes.disabled = no.disabled = false; bot('Sorry, that did not go through. Please try again.'); }
    };
    yes.onclick = answer(true); no.onclick = answer(false);
  }

  function doneCard(p) {
    const lines = [];
    if (p.return_id) lines.push(`Reference ${p.return_id}`);
    if (p.refund_estimate) lines.push(`Estimated refund ${p.refund_estimate}`);
    if (p.order_id && !p.return_id) lines.push(`Order ${p.order_id} cancelled`);
    const card = h('div', { class: 'card2 done' }, h('h4', {}, 'Done ✓'), h('p', {}, lines.join(' · ')));
    if (p.label_url) card.append(h('div', { class: 'row' }, h('a', { class: 'btn pri', href: p.label_url, target: '_blank', rel: 'noopener', style: 'text-decoration:none' }, 'Download return label')));
    add(card);
  }

  async function runSignIn(token) {
    const t = typing();
    try {
      const res = await post(`/v1/sessions/${sessionId}/identity`, { token });
      t.remove(); sys(`Signed in${res.first_name ? ' as ' + res.first_name : ''}`);
      if (res.reply) handle(res.reply, 'Signed in → resumed request');
    } catch (e) { t.remove(); sys("We couldn't verify your sign-in"); }
  }
  function signInCard() {
    if (user) { // already logged in on the site: sign the chat in silently
      freshToken().then(runSignIn); return;
    }
    const btn = h('button', { class: 'btn pri' }, 'Sign in to Acme');
    parkedAuthCard = add(h('div', { class: 'card2' }, h('h4', {}, 'Sign in to continue'),
      h('p', {}, "I'll pick up your request right after. I never see your password."), h('div', { class: 'row' }, btn)));
    btn.onclick = () => openLogin((token) => { parkedAuthCard?.remove(); runSignIn(token); });
  }

  // ---------- pipeline inspector ----------
  async function inspect(reply, userText) {
    let tr; try { tr = await (await fetch(`/v1/turns/${reply.turn_id}/trace`)).json(); } catch { return; }
    const box = $('#turns'); box.querySelector('.empty')?.remove();
    const cls = { ok: 'ok', short_circuit: 'short', error: 'error' };
    const pill = (v) => v ? h('span', { class: `pill ${v}` }, v) : '—';
    box.prepend(h('div', { class: 'turn' },
      h('div', { class: 'q' }, `“${userText}”`),
      h('div', { class: 'stages' }, tr.stages.map(s => h('span', { class: `stage ${cls[s.status] || ''}` }, `${s.stage}${s.ms != null ? ' ' + s.ms + 'ms' : ''}`))),
      h('div', { class: 'kv' },
        h('span', {}, 'intent'), h('span', {}, tr.intent ? `${tr.intent}${tr.confidence != null ? ' (' + tr.confidence + ')' : ''}` : '—'),
        h('span', {}, 'policy'), h('span', {}, pill(tr.policy_outcome), tr.policy_reason ? ' ' + tr.policy_reason : ''),
        h('span', {}, 'handler'), h('span', {}, tr.handler || '—'),
        h('span', {}, 'outcome'), h('span', {}, pill(tr.outcome)))));
  }
})();
