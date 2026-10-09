/* RentalAi front end: a small hash-routed single-page app, no build step. */
const root = document.getElementById('root');
let token = localStorage.getItem('rentalai_token'), me = null, timer = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = v => v == null || v === '' ? '—' : '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = s => new Date(s + (s.endsWith('Z') ? '' : 'Z')).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
const LABEL = { pass: 'Passed', review: 'Needs review', fail: 'Failed', unreadable: 'Unreadable' };
const TYPE = { paystub: 'Pay stub', bank_statement: 'Bank statement', unknown: 'Unrecognised' };
const SEV = { critical: 'Conclusive', high: 'Serious', medium: 'Needs explanation', low: 'Note', info: 'Confirmed' };
const tag = (v, pending) => pending ? '<span class="tag wait">Checking…</span>' : v ? `<span class="tag ${v}">${LABEL[v]}</span>` : '<span class="tag wait">No documents</span>';

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  if (opts.json) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.json); }
  const r = await fetch('/api' + path, { ...opts, headers });
  if (r.status === 401 && token) { signOut(); throw new Error('Session expired'); }
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) { } throw new Error(m); }
  return opts.raw ? r : r.json();
}
function signOut() { token = null; me = null; localStorage.removeItem('rentalai_token'); location.hash = '#/'; route(); }
function shell(html, on) {
  root.innerHTML = `<div class="bar"><a class="brand" href="#/">Rental<i>Ai</i></a>
    <nav><a href="#/" class="${on === 'apps' ? 'on' : ''}">Applications</a><a href="#/model" class="${on === 'model' ? 'on' : ''}">Detection model</a></nav>
    <span class="who">${esc(me.company)}<button class="btn ghost sm" id="out">Sign out</button></span></div><main>${html}</main>`;
  document.getElementById('out').onclick = signOut;
}
const fail = e => { root.querySelector('main') ? root.querySelector('main').insertAdjacentHTML('afterbegin', `<p class="err">${esc(e.message)}</p>`) : alert(e.message); };

/* ---------- sign in ---------- */
function gate(mode = 'login') {
  const reg = mode === 'register';
  root.innerHTML = `<div class="gate"><div class="pitch"><div><a class="brand" href="#/">Rental<i>Ai</i></a>
    <h1 style="margin-top:56px">Know exactly why a document failed.</h1>
    <p>RentalAi reads every pay stub and bank statement an applicant sends, tests it against arithmetic, payroll tax law and the file's own structure, and writes out what is wrong, why it matters and what it would take to pass.</p></div>
    <div class="specimen"><div class="row"><span>Gross pay</span><span>4,400.00</span></div>
      <div class="row"><span>Federal income tax</span><span>264.00</span></div>
      <div class="row hit"><span>Social Security</span><span>148.80</span></div>
      <div class="row"><span>Medicare</span><span>34.80</span></div>
      <p class="say">Social Security is 6.2% of wages. 148.80 is what is withheld on $2,400.00, not $4,400.00.</p></div></div>
    <div class="form"><div><h2 style="font-size:22px">${reg ? 'Create your account' : 'Sign in'}</h2>
    <form id="f">${reg ? '<label for="company">Company</label><input id="company" type="text" required><label for="name">Your name</label><input id="name" type="text" required>' : ''}
    <label for="email">Email</label><input id="email" type="email" required autocomplete="email">
    <label for="password">Password</label><input id="password" type="password" required minlength="8" autocomplete="${reg ? 'new-password' : 'current-password'}">
    <p class="err" id="e" hidden></p>
    <button class="btn" style="margin-top:20px;width:100%">${reg ? 'Create account' : 'Sign in'}</button></form>
    <p class="sub small" style="margin-top:16px">${reg ? 'Already have an account? <a href="#" id="sw">Sign in</a>' : 'New here? <a href="#" id="sw">Create an account</a>'}</p></div></div></div>`;
  document.getElementById('sw').onclick = e => { e.preventDefault(); gate(reg ? 'login' : 'register'); };
  document.getElementById('f').onsubmit = async e => {
    e.preventDefault();
    const v = id => document.getElementById(id)?.value;
    try {
      const r = await api(reg ? '/auth/register' : '/auth/login', { method: 'POST', json: { email: v('email'), password: v('password'), company: v('company'), name: v('name') } });
      token = r.token; localStorage.setItem('rentalai_token', token); route();
    } catch (err) { const el = document.getElementById('e'); el.hidden = false; el.textContent = err.message; }
  };
}

/* ---------- applications ---------- */
async function appsView() {
  const apps = await api('/applications');
  shell(`<div class="head"><div><h1>Applications</h1></div>
    <div><button class="btn ghost" id="demo">Add sample applications</button> <button class="btn" id="new">New application</button></div></div>
    <div id="form"></div>
    <div class="sheet">${apps.length ? `<table><thead><tr><th>Applicant</th><th>Result</th><th>Property</th><th class="num">Monthly income</th><th class="num">Rent</th><th class="num">Documents</th><th>Opened</th></tr></thead><tbody>
      ${apps.map(a => `<tr class="link" data-id="${a.id}"><td><b>${esc(a.applicant_name)}</b></td><td>${tag(a.verdict, a.pending)}</td><td>${esc(a.property || '—')}</td>
        <td class="num">${money(a.monthly_income)}</td><td class="num">${money(a.monthly_rent)}</td><td class="num">${a.n_docs}</td><td>${when(a.created_at)}</td></tr>`).join('')}</tbody></table>`
      : `<div class="empty"><h2>No applications yet</h2><p>Create one and upload an applicant's pay stubs or bank statements, or add the sample applications to see a genuine and a tampered file side by side.</p></div>`}</div>`, 'apps');
  root.querySelectorAll('tr.link').forEach(tr => tr.onclick = () => location.hash = '#/app/' + tr.dataset.id);
  document.getElementById('demo').onclick = async e => { e.target.disabled = true; try { await api('/demo', { method: 'POST' }); appsView(); } catch (x) { fail(x); } };
  document.getElementById('new').onclick = () => {
    document.getElementById('form').innerHTML = `<form class="sheet pad" id="nf" style="margin-bottom:20px"><h2>New application</h2>
      <div class="cols" style="margin-top:0"><div><label for="an">Applicant's full name</label><input id="an" type="text" required></div>
      <div><label for="ae">Applicant's email (optional)</label><input id="ae" type="email"></div>
      <div><label for="ap">Property or unit</label><input id="ap" type="text"></div>
      <div><label for="ar">Monthly rent</label><input id="ar" type="number" min="0" step="1"></div></div>
      <button class="btn" style="margin-top:18px">Create application</button></form>`;
    document.getElementById('an').focus();
    document.getElementById('nf').onsubmit = async e => {
      e.preventDefault();
      const v = id => document.getElementById(id).value;
      try { const r = await api('/applications', { method: 'POST', json: { applicant_name: v('an'), applicant_email: v('ae') || null, property: v('ap') || null, monthly_rent: v('ar') ? Number(v('ar')) : null } }); location.hash = '#/app/' + r.id; } catch (x) { fail(x); }
    };
  };
  if (apps.some(a => a.pending)) timer = setTimeout(appsView, 1500);
}

function findingCard(f, i) {
  return `<article class="finding ${f.severity}" data-i="${i}"><h3><span>${esc(f.title)}</span><span class="sev">${SEV[f.severity]}</span></h3>
    <dl><dt>What</dt><dd>${esc(f.what)}</dd><dt>Why it matters</dt><dd>${esc(f.why)}</dd>${f.severity === 'info' ? '' : `<dt>To pass</dt><dd class="fix">${esc(f.fix)}</dd>`}</dl></article>`;
}
function uploader(el, url, done) {
  el.innerHTML = `<div class="drop" tabindex="0" role="button">Drop pay stubs or bank statements here, or choose files<br><span class="small">PDF, JPEG or PNG. Original PDFs from the payroll or bank portal can be checked most thoroughly.</span><input type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff" hidden></div><p class="err" hidden></p>`;
  const drop = el.querySelector('.drop'), input = el.querySelector('input'), err = el.querySelector('.err');
  const send = async files => {
    if (!files.length) return;
    const fd = new FormData(); [...files].forEach(f => fd.append('files', f));
    drop.firstChild.textContent = 'Uploading…'; err.hidden = true;
    try { await api(url, { method: 'POST', body: fd }); done(); } catch (x) { err.hidden = false; err.textContent = x.message; drop.firstChild.textContent = 'Drop files here, or choose files'; }
  };
  drop.onclick = () => input.click();
  drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  input.onchange = () => send(input.files);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); send(e.dataTransfer.files); };
}

async function appView(id) {
  const a = await api('/applications/' + id), inc = a.income || {};
  const head = a.pending ? 'Checking documents…' : !a.verdict ? 'Waiting for documents' :
    { pass: 'Documents passed', review: 'Needs a closer look', fail: 'Failed verification' }[a.verdict];
  const reasons = a.documents.filter(d => d.verdict && d.verdict !== 'pass').flatMap(d => d.failed_because.map(r => `${r} <span class="sub">in ${esc(d.filename)}</span>`))
    .concat(a.cross.filter(f => ['high', 'medium'].includes(f.severity)).map(f => esc(f.title)));
  const link = location.origin + '/apply/' + a.token;
  shell(`<p class="small"><a href="#/">Applications</a> / ${esc(a.applicant_name)}</p>
    <div class="sheet pad verdict ${a.pending ? '' : a.verdict || ''}" style="margin-top:10px"><div><p class="sub">${esc(a.applicant_name)}${a.property ? ' for ' + esc(a.property) : ''}</p><h1>${head}</h1>
      ${reasons.length ? `<div class="cols"><div><h3>Why</h3><ul class="plain bad">${reasons.map(r => `<li>${r}</li>`).join('')}</ul></div>
      <div><h3>What it would take to pass</h3><ul class="plain">${a.needs_to_pass.map(n => `<li>${esc(n)}</li>`).join('')}</ul></div></div>` : a.verdict === 'pass' ? '<p class="sub" style="margin-top:8px">Every check that could be run on these documents passed.</p>' : ''}</div>
      <button class="btn ghost sm" id="del">Delete</button></div>
    <div class="sheet section cols3"><div class="stat"><span class="sub small">Supported monthly income</span><b>${money(inc.monthly_gross)}</b>${inc.ytd_monthly ? `<span class="sub small">Year-to-date average ${money(inc.ytd_monthly)}</span>` : ''}</div>
      <div class="stat"><span class="sub small">Monthly rent</span><b>${money(a.monthly_rent)}</b>${inc.required_monthly ? `<span class="sub small">Requires ${money(inc.required_monthly)} at ${me.income_multiple}× rent</span>` : ''}</div>
      <div class="stat"><span class="sub small">Income to rent</span><b>${inc.income_to_rent ? inc.income_to_rent + '×' : '—'}</b>${inc.income_to_rent ? `<span class="small" style="color:var(--${inc.meets_requirement ? 'pass' : 'fail'})">${inc.meets_requirement ? 'Meets the requirement' : 'Below the requirement'}</span>` : ''}</div></div>
    ${(inc.notes || []).length ? `<p class="sub small" style="margin-top:8px">${inc.notes.map(esc).join(' ')}</p>` : ''}
    <div class="section"><h2>Documents</h2><div class="sheet">${a.documents.length ? `<table><thead><tr><th>File</th><th>Result</th><th>Type</th><th>Main reason</th><th class="num">Risk</th></tr></thead><tbody>
      ${a.documents.map(d => `<tr class="link" data-id="${d.id}"><td><b>${esc(d.filename)}</b>${d.source === 'applicant' ? '<br><span class="sub small">Sent by applicant</span>' : ''}</td>
      <td>${tag(d.verdict, ['queued', 'processing'].includes(d.status))}${d.reviewed ? '<br><span class="sub small">Analyst decision</span>' : ''}</td><td>${TYPE[d.doc_type] || '—'}</td>
      <td>${esc(d.failed_because[0] || (d.verdict === 'pass' ? 'All checks passed' : '—'))}${d.failed_because.length > 1 ? ` <span class="sub">and ${d.failed_because.length - 1} more</span>` : ''}</td><td class="num">${d.risk ?? '—'}</td></tr>`).join('')}</tbody></table>` : '<div class="empty"><p>No documents yet. Upload them below or send the applicant their link.</p></div>'}</div></div>
    ${a.cross.length ? `<div class="section"><h2>Across documents</h2>${a.cross.map(findingCard).join('')}</div>` : ''}
    <div class="section cols"><div><h2 style="margin-bottom:10px">Upload documents</h2><div id="up"></div></div>
      <div><h2 style="margin-bottom:10px">Applicant's upload link</h2><div class="sheet pad"><p class="sub small">The applicant uploads here without an account. They see what to send next, not the forensic findings.</p>
      <div class="copy" style="margin-top:12px"><input type="text" readonly value="${esc(link)}" aria-label="Applicant link"><button class="btn ghost sm" id="cp">Copy link</button></div>
      <label class="consent" style="margin-top:18px"><input type="checkbox" id="tc" ${a.training_consent ? 'checked' : ''}><span>Applicant has consented to their documents being used to improve fraud detection${a.consent_by ? ` <span class="sub">(last set by ${esc(a.consent_by === 'applicant' ? 'the applicant' : a.consent_by.replace('staff:', ''))})</span>` : ''}. Without this, these documents are never used for training.</span></label>
      ${a.applicant_requests.length ? `<h3 style="margin-top:18px">What the applicant is being asked for</h3><ul class="plain">${a.applicant_requests.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}</div></div></div>`, 'apps');
  root.querySelectorAll('tr.link').forEach(tr => tr.onclick = () => location.hash = '#/doc/' + tr.dataset.id);
  uploader(document.getElementById('up'), `/applications/${id}/documents`, () => appView(id));
  document.getElementById('tc').onchange = async e => { try { await api(`/applications/${id}/consent`, { method: 'POST', json: { training: e.target.checked } }); appView(id); } catch (x) { fail(x); } };
  document.getElementById('cp').onclick = e => { navigator.clipboard?.writeText(link); e.target.textContent = 'Link copied'; };
  document.getElementById('del').onclick = async () => { if (confirm('Delete this application and its documents?')) { await api('/applications/' + id, { method: 'DELETE' }); location.hash = '#/'; } };
  if (a.pending) timer = setTimeout(() => appView(id), 1500);
}

/* ---------- document examiner ---------- */
async function docView(id) {
  const d = await api('/documents/' + id), r = d.report || {}, fs = r.findings || [], fl = r.fields || {};
  if (['queued', 'processing'].includes(d.status)) { shell('<p class="sub">Checking this document…</p>', 'apps'); timer = setTimeout(() => docView(id), 1200); return; }
  const real = fs.filter(f => f.severity !== 'info');
  const v = d.verdict, m = r.model || {};
  const fields = [['Employer', fl.employer], ['Issued to', fl.employee], ['Pay period', fl.period_start && fl.period_end ? `${fl.period_start} to ${fl.period_end}` : null], ['Pay date', fl.pay_date],
  ['Pay frequency', fl.frequency], ['Gross pay', fl.gross && money(fl.gross)], ['Total deductions', fl.total_deductions && money(fl.total_deductions)], ['Net pay', fl.net && money(fl.net)], ['Gross year to date', fl.gross_ytd && money(fl.gross_ytd)],
  ['Beginning balance', fl.begin_balance && money(fl.begin_balance)], ['Ending balance', fl.end_balance && money(fl.end_balance)], ['Transactions read', (fl.txns || []).length || null]].filter(x => x[1]);
  shell(`<p class="small"><a href="#/">Applications</a> / <a href="#/app/${d.application_id}">${esc(d.applicant_name)}</a> / ${esc(d.filename)}</p>
  <div class="exam" style="margin-top:12px"><div class="plate" id="plate">${(r.pages || []).map(p => `<div class="page" data-p="${p.number}" style="aspect-ratio:${p.width}/${p.height}"></div>`).join('') || '<p class="sub">No preview available.</p>'}</div>
  <div><div class="sheet pad verdict ${v}" style="grid-template-columns:1fr"><div><p class="sub">${TYPE[r.doc_type] || 'Document'}, ${esc(d.filename)}</p><h1>${esc(d.headline || '')}</h1>
    ${d.review ? `<p class="small" style="margin-top:8px">Decided ${d.review.decision} by ${esc(d.review.by)}${d.review.notes ? ': ' + esc(d.review.notes) : ''}. The automated result was “${LABEL[d.engine_verdict]}”.</p>` : ''}
    ${real.length ? `<h3 style="margin-top:18px">Why</h3><ul class="plain bad">${(r.failed_because || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li>Minor notes only, listed below.</li>'}</ul>
    ${(r.needs_to_pass || []).length ? `<h3 style="margin-top:18px">What it would take to pass</h3><ul class="plain">${r.needs_to_pass.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}` : '<p class="sub" style="margin-top:8px">Nothing in this file contradicts itself or shows signs of editing.</p>'}
    <div style="margin-top:18px"><span class="sub small">Risk score ${r.risk ?? 0} of 100${m.available ? `, model probability of tampering ${(m.probability * 100).toFixed(0)}%` : ''}</span><div class="meter"><i style="width:${r.risk ?? 0}%"></i></div></div></div></div>
  ${fs.length ? `<div class="section"><h2>Findings</h2><p class="sub small" style="margin-bottom:10px">Select a finding to see where it is on the page.</p>${fs.map(findingCard).join('')}</div>` : ''}
  ${(r.passed || []).length ? `<div class="section"><h2>Checks passed</h2><div class="sheet pad"><ul class="checks" style="margin-top:0">${r.passed.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div></div>` : ''}
  ${fields.length ? `<div class="section"><h2>What was read from the document</h2><div class="sheet pad"><div class="kv">${fields.map(x => `<span>${x[0]}</span><span class="mono">${esc(x[1])}</span>`).join('')}</div></div></div>` : ''}
  <div class="section"><h2>Analyst decision</h2><div class="sheet pad"><p class="sub small">Record what you concluded after your own review. Your decision overrides the automated result. It is used to train the detection model only if the applicant has consented.</p>
    <label for="notes">Notes</label><textarea id="notes" rows="2">${esc(d.review?.notes || '')}</textarea>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn ghost" data-dec="genuine">Confirm genuine</button><button class="btn ghost" data-dec="fraudulent">Confirm fraudulent</button>
    ${d.review ? '<button class="btn ghost" data-dec="clear">Clear decision</button>' : ''}<button class="btn ghost" id="re">Run checks again</button><button class="btn ghost" id="dl">Download original</button></div></div></div></div></div>`, 'apps');

  // page previews with evidence marks
  const cards = [...root.querySelectorAll('.finding')];
  const select = i => { root.querySelectorAll('.on').forEach(e => e.classList.remove('on')); cards[i]?.classList.add('on'); const ms = root.querySelectorAll(`.mark[data-i="${i}"]`); ms.forEach(e => e.classList.add('on')); return ms[0]; };
  (r.pages || []).forEach(async p => {
    const el = root.querySelector(`.page[data-p="${p.number}"]`);
    try { const res = await api(`/documents/${id}/pages/${p.number}`, { raw: true }); const img = new Image(); img.alt = `Page ${p.number} of ${d.filename}`; img.src = URL.createObjectURL(await res.blob()); el.prepend(img); } catch (e) { }
    fs.forEach((f, i) => (f.evidence || []).filter(e => e.page === p.number).forEach(e => {
      const [x0, y0, x1, y1] = e.bbox, pad = 2, mk = document.createElement('button');
      mk.className = 'mark ' + f.severity; mk.dataset.i = i; mk.title = f.title; mk.setAttribute('aria-label', f.title);
      mk.style.cssText = `left:${(x0 - pad) / p.width * 100}%;top:${(y0 - pad) / p.height * 100}%;width:${(x1 - x0 + 2 * pad) / p.width * 100}%;height:${(y1 - y0 + 2 * pad) / p.height * 100}%`;
      mk.onclick = () => { select(i); cards[i].scrollIntoView({ behavior: 'smooth', block: 'center' }); };
      el.appendChild(mk);
    }));
  });
  cards.forEach((c, i) => c.onclick = () => { const mk = select(i); mk?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); });
  root.querySelectorAll('[data-dec]').forEach(b => b.onclick = async () => { try { await api(`/documents/${id}/review`, { method: 'POST', json: { decision: b.dataset.dec, notes: document.getElementById('notes').value } }); docView(id); } catch (x) { fail(x); } });
  document.getElementById('re').onclick = async () => { await api(`/documents/${id}/reanalyze`, { method: 'POST' }); docView(id); };
  document.getElementById('dl').onclick = async () => { const res = await api(`/documents/${id}/original`, { raw: true }); const a = document.createElement('a'); a.href = URL.createObjectURL(await res.blob()); a.download = d.filename; a.click(); };
}

/* ---------- model ---------- */
async function modelView() {
  const m = await api('/model'), x = m.metrics || {}, pct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
  const rows = Object.entries(x.by_kind || {}).sort();
  const NAME = { clean: 'Original export', browser: 'Printed to PDF from a browser', resaved: 'Re-saved by a PDF viewer', office: 'Made in Excel by a small employer', scan_jpg: 'Photo or scan (JPEG)', scan_pdf: 'Scan inside a PDF', screenshot: 'Screenshot', retype: 'Figures retyped in a PDF editor', whiteout: 'Figures covered and typed over', rebuilt: 'Rebuilt in Word or Canva, taxes left unchanged', rebuilt_clean: 'Rebuilt with provider-style metadata, taxes left unchanged', image_edit: 'Figures painted over in an image', name_swap: "Someone else's stub", generator: 'Self-consistent fake from a generator or template', perfect: 'Self-consistent fake with provider-style metadata' };
  shell(`<div class="head"><div><h1>Detection model</h1><p class="sub" style="max-width:70ch;margin-top:8px">Each document is tested by fixed checks (arithmetic, tax rates, file structure). A gradient-boosted model then weighs that evidence into one probability. It ships trained on synthetic documents and gets better as you confirm real outcomes.</p></div>
    <button class="btn" id="rt" ${m.available ? '' : 'disabled'}>Retrain with consented documents</button></div>
    <div class="sheet cols3"><div class="stat"><span class="sub small">Trained on</span><b>${m.trained_on ? m.trained_on.synthetic + m.trained_on.real_labelled : '—'}</b><span class="sub small">${m.trained_on ? `${m.trained_on.synthetic} synthetic, ${m.trained_on.real_labelled} analyst-confirmed` : 'No model installed'}</span></div>
    <div class="stat"><span class="sub small">Usable for training</span><b>${m.consented_documents}</b><span class="sub small">of ${m.labelled_documents} analyst decisions; the rest lack applicant consent</span></div>
    <div class="stat"><span class="sub small">Model version</span><b style="font-size:22px;padding-top:8px">${esc(m.version || '—')}</b></div></div>
    <div class="section"><h2>Results on the synthetic test set</h2><p class="sub small" style="margin-bottom:10px;max-width:80ch">These numbers describe documents generated by this project's own simulator. They are not a measurement of accuracy on real applicants' documents; that requires real, labelled files.</p>
    <div class="sheet cols3"><div class="stat"><span class="sub small">Genuine documents passed</span><b>${pct(x.genuine_passed)}</b><span class="sub small">${pct(x.genuine_failed)} wrongly failed</span></div>
    <div class="stat"><span class="sub small">Tampered documents caught</span><b>${pct(x.tampered_caught)}</b><span class="sub small">${pct(x.tampered_passed)} passed undetected</span></div>
    <div class="stat"><span class="sub small">Held-out AUC</span><b>${x.holdout_auc ?? '—'}</b><span class="sub small">${x.n ?? 0} documents</span></div></div></div>
    <div class="section"><h2>By kind of document</h2><div class="sheet"><table><thead><tr><th>Kind</th><th>Truth</th><th class="num">Count</th><th class="num">Passed</th><th class="num">Review</th><th class="num">Failed</th></tr></thead><tbody>
    ${rows.map(([k, v]) => { const [t, kind] = k.split(':'); return `<tr><td>${esc(NAME[kind] || kind)}</td><td>${t === 'genuine' ? 'Genuine' : 'Tampered'}</td><td class="num">${v.n}</td><td class="num">${v.pass}</td><td class="num">${v.review}</td><td class="num">${v.fail}</td></tr>`; }).join('')}</tbody></table></div>
    <p class="sub small" style="margin-top:10px;max-width:80ch">A fake that is internally consistent and carries provider-style metadata cannot be caught from the file alone. Catching those takes a second source: matching bank deposits, a payroll connection, or employer verification.</p></div>`, 'model');
  document.getElementById('rt').onclick = async e => { e.target.disabled = true; e.target.textContent = 'Retraining…'; try { await api('/model/retrain', { method: 'POST' }); modelView(); } catch (x) { fail(x); } };
}

/* ---------- applicant page ---------- */
async function applyView(tok) {
  let s; try { s = await api('/public/' + tok); } catch (e) { root.innerHTML = `<div class="solo"><h1>Link not valid</h1><p class="sub" style="margin-top:10px">Ask your leasing office for a new link.</p></div>`; return; }
  const checking = s.documents.some(d => d.state === 'Checking');
  root.innerHTML = `<div class="solo"><p class="brand" style="color:var(--ink)">Rental<i style="color:var(--uv)">Ai</i></p>
    <h1 style="margin-top:28px">Send your income documents</h1>
    <p class="sub" style="margin-top:10px">${esc(s.company)} asked for proof of income for ${esc(s.applicant_name)}${s.property ? ', applying for ' + esc(s.property) : ''}. Upload your two most recent pay stubs and, if you have it, your latest bank statement.</p>
    <div id="up" style="margin-top:24px"></div>
    <label class="consent"><input type="checkbox" id="tc" ${s.training_consent ? 'checked' : ''}><span><b>Optional:</b> allow RentalAi to use my documents to improve its fraud detection. This does not affect my application, and I can untick it at any time.</span></label>
    ${s.requests.length && !checking ? `<div class="sheet pad section"><h2>Still needed</h2><ul class="plain">${s.requests.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>` : ''}
    ${s.documents.length ? `<div class="section"><h2>What you have sent</h2><div class="sheet"><table><tbody>${s.documents.map(d => `<tr><td>${esc(d.filename)}</td><td class="num">${esc(d.state)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
    <p class="sub small section">Your files are used to verify income for this application. They are used to improve fraud detection only if you tick the box above.</p></div>`;
  uploader(document.getElementById('up'), `/public/${tok}/documents`, () => applyView(tok));
  document.getElementById('tc').onchange = e => api(`/public/${tok}/consent`, { method: 'POST', json: { training: e.target.checked } }).catch(() => { e.target.checked = !e.target.checked; });
  if (checking) timer = setTimeout(() => applyView(tok), 1500);
}

async function route() {
  clearTimeout(timer);
  const pub = location.pathname.match(/^\/apply\/([\w-]+)/);
  if (pub) return applyView(pub[1]);
  if (!token) return gate();
  try {
    me = me || await api('/me');
    const h = location.hash, m = h.match(/^#\/(app|doc)\/(\d+)/);
    window.scrollTo(0, 0);
    if (m) return await (m[1] === 'app' ? appView : docView)(m[2]);
    if (h === '#/model') return await modelView();
    await appsView();
  } catch (e) { if (token) { shell(`<p class="err">${esc(e.message)}</p><p style="margin-top:12px"><a href="#/">Back to applications</a></p>`); } }
}
window.onhashchange = route;
route();
