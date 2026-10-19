// ── Harvey — administration from the browser ──
//
// Everything a person needs to run Harvey without a terminal: signing the AI
// in, connecting the mailbox, the whole configuration as a form, training,
// branding. Loads after app.js and only adds to it; the original tabs keep
// their own code.

// ── Branding (name, logo, accent, default theme) ──

let _branding = null;

function _hexToRgb(hex) {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
  const n = parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function _contrastFor(hex) {
  try {
    const [r, g, b] = _hexToRgb(hex);
    const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    return lum > 0.6 ? '#1E1B17' : '#FBFAF8';
  } catch { return '#FBFAF8'; }
}

function applyAccent(hex) {
  const root = document.documentElement;
  if (hex && /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(hex)) {
    root.style.setProperty('--accent', hex);
    root.style.setProperty('--accent-contrast', _contrastFor(hex));
  } else {
    root.style.removeProperty('--accent');
    root.style.removeProperty('--accent-contrast');
  }
}

function applyBranding(b) {
  _branding = b;
  document.title = (b.app_name || 'Harvey') + ' — Painel';
  const name = document.getElementById('brand-name');
  const tag = document.getElementById('brand-tagline');
  const mark = document.getElementById('brand-mark');
  const logo = document.getElementById('brand-logo');
  if (name) name.textContent = b.app_name || 'Harvey';
  if (tag) tag.textContent = b.tagline || '';
  if (mark) mark.textContent = (b.app_name || 'H').trim().charAt(0).toUpperCase() || 'H';
  if (logo && mark) {
    if (b.logo_url) { logo.src = b.logo_url; logo.style.display = ''; mark.style.display = 'none'; }
    else { logo.style.display = 'none'; mark.style.display = ''; }
  }
  applyAccent(b.accent || '');
  // The server's default theme applies only until this browser picks one.
  let chosen = null;
  try { chosen = localStorage.getItem('harvey-theme'); } catch { /* ignore */ }
  if (!chosen && b.theme_default && THEMES.includes(b.theme_default)) applyTheme(b.theme_default);
}

async function loadBranding() {
  const b = await api('/api/branding');
  if (b) applyBranding(b);
}

// ── Who is signed in (Cloudflare Access) ──

async function loadMe() {
  const me = await api('/api/me');
  const chip = document.getElementById('user-chip');
  if (!chip) return;
  if (me && me.email) {
    document.getElementById('user-email').textContent = me.email;
    chip.style.display = '';
  } else {
    chip.style.display = 'none';
  }
}

// ── Tabs: hand the new ones their loaders, and route by URL hash ──

const _loadOriginalTab = loadCurrentTab;
loadCurrentTab = function () {
  switch (currentTab) {
    case 'brain': loadBrain(); break;
    case 'email': loadEmail(); break;
    case 'config': loadConfig(); break;
    case 'settings': loadSettings(); loadAdminEnv(); break;
    case 'train': loadTrain(); break;
    case 'appearance': loadAppearance(); break;
    default: _loadOriginalTab(); break;
  }
  try { history.replaceState(null, '', '#' + currentTab); } catch { /* ignore */ }
};

function copyText(id) {
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(
    () => showToast('Copiado.', 'success'),
    () => showToast('Não consegui copiar; selecione e copie à mão.', 'error'));
}

function setResult(id, ok, msg, pending) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '<div class="test-result ' + (pending ? 'pending' : ok ? 'success' : 'error') + '">' + escHtml(msg) + '</div>';
}

// ── IA ──

function _tokenSourceLabel(src) {
  return ({
    token: ['Conectado por token (setup-token)', 'verified'],
    login: ['Conectado por login local (claude login)', 'verified'],
    none: ['Não conectado', 'guess'],
  })[src] || [src, 'guess'];
}

async function loadBrain() {
  const box = document.getElementById('brain-status');
  const data = await api('/api/brain/status');
  if (!data) { box.innerHTML = offlineState(); return; }

  const [srcLabel, srcTone] = _tokenSourceLabel(data.token_source);
  const isClaude = data.provider !== 'codex';
  const cli = isClaude ? data.claude_cli : data.codex_cli;
  let html = '<div class="card"><h2>Situação</h2><div class="kv">';
  html += '<div class="k">Provedor</div><div class="v">' + (isClaude ? 'Claude (assinatura)' : 'Codex (plano ChatGPT)') +
          (data.model ? ' <span class="muted">· modelo ' + escHtml(data.model) + '</span>' : '') + '</div>';
  html += '<div class="k">Programa na máquina</div><div class="v">' + (cli
          ? '<span class="email-tag verified">instalado</span> <span class="muted">' + escHtml(cli) + '</span>'
          : '<span class="email-tag guess">não encontrado</span> <span class="muted">o instalador (deploy/install.sh) instala; ou rode o instalador do Claude Code na VM</span>') + '</div>';
  if (isClaude) {
    html += '<div class="k">Login</div><div class="v"><span class="email-tag ' + srcTone + '">' + escHtml(srcLabel) + '</span></div>';
  }
  if (data.paused_reason) {
    html += '<div class="k">Pausado</div><div class="v"><span class="email-tag risky">' + escHtml(data.paused_reason) + '</span></div>';
  }
  html += '</div></div>';

  const q = quotaCardHtml({quota_provider: data.provider, quota: data.quota});
  if (q) html += '<div class="card">' + q +
    '<p class="muted" style="font-size:12px;margin-top:6px">O Harvey para sozinho quando a cota passa do limite que você definiu em <b>Configurações → Uso</b>, pra sobrar espaço pro seu próprio uso do Claude.</p></div>';
  box.innerHTML = html;

  navCount('nav-brain', (isClaude && data.token_source === 'none') ? 1 : 0);
  const rm = document.getElementById('brain-token-remove');
  if (rm) rm.style.display = data.token_source === 'token' ? '' : 'none';
  const tokenCard = document.getElementById('brain-token-card');
  if (tokenCard) tokenCard.style.display = isClaude ? '' : 'none';

  const cfg = await api('/api/config');
  const brain = (cfg && cfg.effective && cfg.effective.brain) || {provider: data.provider, model: data.model, reasoning_effort: ''};
  document.getElementById('brain-provider').value = brain.provider || 'claude';
  document.getElementById('brain-model').value = brain.model || '';
  document.getElementById('brain-effort').value = brain.reasoning_effort || '';
}

async function saveBrainToken() {
  const el = document.getElementById('brain-token');
  const token = el.value.trim();
  if (!token) { showToast('Cole o token primeiro.', 'error'); return; }
  const r = await api('/api/brain/token', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})});
  if (r && r.success) { el.value = ''; showToast('Token salvo. Agora clique em Testar a IA.', 'success'); loadBrain(); }
  else showToast((r && r.message) || 'Não consegui salvar o token.', 'error');
}

async function removeBrainToken() {
  if (!confirm('Remover o token? O Harvey para de conseguir pensar até você colar outro.')) return;
  const r = await api('/api/brain/token', {method: 'DELETE'});
  if (r && r.success) { showToast('Token removido.', 'success'); loadBrain(); }
  else showToast('Não consegui remover.', 'error');
}

async function testBrain() {
  const btn = document.getElementById('brain-test-btn');
  btn.disabled = true;
  setResult('brain-test-result', true, 'Perguntando pra IA… pode levar até um minuto.', true);
  const r = await api('/api/brain/test', {method: 'POST'});
  btn.disabled = false;
  if (!r) { setResult('brain-test-result', false, 'O painel não respondeu.'); return; }
  setResult('brain-test-result', r.success, r.message);
  if (r.success) loadBrain();
}

async function saveBrainProvider() {
  const body = {brain: {
    provider: document.getElementById('brain-provider').value,
    model: document.getElementById('brain-model').value.trim(),
    reasoning_effort: document.getElementById('brain-effort').value,
  }};
  const r = await api('/api/config', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r && r.success) { showToast('Provedor salvo. ' + (r.note || ''), 'success'); loadBrain(); }
  else showToast((r && (r.errors ? r.errors.map(e => e.field + ': ' + e.message).join('; ') : r.message)) || 'Não consegui salvar.', 'error');
}

// ── E-mail ──

const PROVIDERS = [
  {key: 'gmail', name: 'Gmail / Google Workspace', blurb: 'Recomendado. Conecta com um clique depois de criar a credencial no Google Cloud.'},
  {key: 'microsoft', name: 'Microsoft 365 / Outlook', blurb: 'Pra caixas no Exchange Online. Conecta com um clique depois de registrar o app.'},
  {key: 'smtp', name: 'SMTP + IMAP', blurb: 'Qualquer caixa comum com usuário e senha (Fastmail, AgentMail, senha de app).'},
  {key: 'instantly', name: 'Instantly', blurb: 'Plataforma de e-mail frio. Legado; precisa do plano Growth pra API.'},
];

let _emailStatus = null;

async function loadEmail() {
  await loadSettings();   // the original tab's loader fills the Gmail/SMTP/Instantly fields
  const [st, env] = await Promise.all([api('/api/email/status'), api('/api/admin/env')]);
  _emailStatus = st;
  if (!st) { document.getElementById('email-providers').innerHTML = offlineState(); return; }

  const grid = document.getElementById('email-providers');
  grid.innerHTML = PROVIDERS.map(p => {
    const active = p.key === st.provider;
    const connected = (p.key === 'gmail' && st.gmail_connected) || (p.key === 'microsoft' && st.microsoft_connected);
    return '<div class="prov-card' + (active ? ' selected' : '') + '" onclick="setProvider(\'' + p.key + '\')">' +
      '<div class="prov-head"><h3>' + escHtml(p.name) + '</h3>' +
        (active ? '<span class="badge t-good">em uso</span>' : '') + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      (connected ? '<div class="row"><span class="k">Login</span><span class="v">conectado</span></div>' : '') +
      '<div style="margin-top:auto"><button class="btn btn-sm ' + (active ? 'btn-secondary' : 'btn-primary') + '" ' +
        (active ? 'disabled' : '') + '>' + (active ? 'Selecionado' : 'Usar este') + '</button></div>' +
    '</div>';
  }).join('');

  document.getElementById('gmail-redirect-uri').textContent = st.gmail_redirect_uri;
  document.getElementById('microsoft-redirect-uri').textContent = st.microsoft_redirect_uri;

  const g = document.getElementById('gmail-status');
  if (g) {
    if (st.gmail_connected) { g.className = 'email-tag verified'; g.textContent = 'conectado'; }
    else { g.className = 'email-tag guess'; g.textContent = 'não conectado'; }
  }
  document.getElementById('gmail-disconnect').style.display = st.gmail_connected ? '' : 'none';
  const m = document.getElementById('microsoft-status');
  m.className = 'email-tag ' + (st.microsoft_connected ? 'verified' : 'guess');
  m.textContent = st.microsoft_connected ? 'conectado' : 'não conectado';
  document.getElementById('microsoft-disconnect').style.display = st.microsoft_connected ? '' : 'none';

  if (env) {
    document.getElementById('m365-client-id').value = env.M365_CLIENT_ID || '';
    document.getElementById('m365-tenant-id').value = env.M365_TENANT_ID || '';
  }

  // Only the chosen provider's block is open; the others fold.
  for (const p of PROVIDERS) {
    const card = document.getElementById('email-' + p.key);
    if (card) card.classList.toggle('folded', p.key !== st.provider);
  }

  const ready = (st.provider === 'gmail' && st.gmail_connected) || (st.provider === 'microsoft' && st.microsoft_connected) ||
                st.provider === 'smtp' || st.provider === 'instantly';
  navCount('nav-email', ready ? 0 : 1);
}

async function setProvider(key) {
  if (_emailStatus && _emailStatus.provider === key) return;
  const r = await api('/api/email/provider', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: key})});
  if (r && r.success) { showToast('Provedor de e-mail: ' + key + '. Configure o bloco abaixo.', 'success'); loadEmail(); }
  else showToast((r && r.message) || 'Não consegui trocar o provedor.', 'error');
}

async function connectGmail() {
  const r = await api('/api/gmail/auth/url');
  if (!r || !r.success) { showToast((r && r.message) || 'Salve as credenciais do Google primeiro.', 'error'); return; }
  window.location.href = r.url;
}

async function connectMicrosoft() {
  const r = await api('/api/microsoft/auth/url');
  if (!r || !r.success) { showToast((r && r.message) || 'Salve o Client ID da Microsoft primeiro.', 'error'); return; }
  window.location.href = r.url;
}

async function disconnectMail(which) {
  if (!confirm('Desconectar a caixa? O Harvey para de enviar e de ler respostas até conectar de novo.')) return;
  const r = await api('/api/' + which + '/disconnect', {method: 'POST'});
  if (r && r.success) { showToast('Desconectado.', 'success'); loadEmail(); }
  else showToast('Não consegui desconectar.', 'error');
}

function saveMicrosoft() {
  const payload = {
    M365_CLIENT_ID: document.getElementById('m365-client-id').value.trim(),
    M365_TENANT_ID: document.getElementById('m365-tenant-id').value.trim(),
  };
  saveAdminEnv(payload, 'Dados da Microsoft salvos. Agora clique em Conectar com Microsoft.');
}

async function testEmail() {
  setResult('email-test-result', true, 'Testando a conexão…', true);
  const r = await api('/api/email/test', {method: 'POST'});
  if (!r) { setResult('email-test-result', false, 'O painel não respondeu.'); return; }
  setResult('email-test-result', r.success, r.message);
}

// ── Chaves (extra .env keys) ──

async function loadAdminEnv() {
  const env = await api('/api/admin/env');
  if (!env) return;
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Salvo — digite um novo valor para alterar' : base;
  };
  const tag = (id, ok) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? 'definida' : 'não definida';
  };
  document.getElementById('dfs-login').value = env.DATAFORSEO_LOGIN || '';
  document.getElementById('dfs-sandbox').checked = !!(env.DATAFORSEO_SANDBOX || '').trim();
  savedPh('dfs-pass', env.DATAFORSEO_PASSWORD_set, 'a senha gerada na página da API, não a da conta');
  savedPh('serper-key', env.SERPER_API_KEY_set, '2.500 buscas grátis sem cartão');
  savedPh('precleared-key', env.PRECLEARED_API_KEY_set, 'opcional');
  tag('dfs-status', env.DATAFORSEO_PASSWORD_set);
  tag('serper-status', env.SERPER_API_KEY_set);
  tag('precleared-status', env.PRECLEARED_API_KEY_set);
  document.getElementById('cf-team').value = (env.CF_ACCESS_TEAM_DOMAIN || '').replace('.cloudflareaccess.com', '');
  document.getElementById('cf-aud').value = env.CF_ACCESS_AUD || '';
  document.getElementById('cf-emails').value = env.CF_ACCESS_ALLOWED_EMAILS || '';
  document.getElementById('m365-client-id').value = env.M365_CLIENT_ID || '';
  document.getElementById('m365-tenant-id').value = env.M365_TENANT_ID || '';
}

async function saveAdminEnv(payload, okMsg) {
  const r = await api('/api/admin/env', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  if (r && r.success) { showToast(okMsg, 'success'); loadAdminEnv(); }
  else showToast((r && r.message) || 'Não consegui salvar.', 'error');
}

function saveDiscoveryKeys() {
  const payload = {
    DATAFORSEO_LOGIN: document.getElementById('dfs-login').value.trim(),
    DATAFORSEO_SANDBOX: document.getElementById('dfs-sandbox').checked ? '1' : '',
  };
  const p = document.getElementById('dfs-pass').value;
  const s = document.getElementById('serper-key').value;
  if (p) payload.DATAFORSEO_PASSWORD = p;
  if (s) payload.SERPER_API_KEY = s;
  saveAdminEnv(payload, 'Chaves de descoberta salvas.');
}

function savePrecleared() {
  const k = document.getElementById('precleared-key').value;
  if (!k) { showToast('Cole a chave primeiro.', 'error'); return; }
  saveAdminEnv({PRECLEARED_API_KEY: k}, 'Chave Precleared salva.');
}

function saveAccess() {
  const emails = document.getElementById('cf-emails').value.split(',').map(e => e.trim().toLowerCase()).filter(Boolean);
  const bad = emails.find(e => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e));
  if (bad) { showToast('E-mail inválido: ' + bad, 'error'); return; }
  const team = document.getElementById('cf-team').value.trim().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  const aud = document.getElementById('cf-aud').value.trim();
  if ((team && !aud) || (!team && aud)) { showToast('Preencha o domínio do time E o AUD, ou deixe os dois em branco.', 'error'); return; }
  if (team && aud && !emails.length && !confirm('Sem e-mails na lista, qualquer pessoa aprovada na política do Cloudflare Access entra. Continuar?')) return;
  saveAdminEnv({CF_ACCESS_TEAM_DOMAIN: team, CF_ACCESS_AUD: aud, CF_ACCESS_ALLOWED_EMAILS: emails.join(',')},
               team ? 'Acesso restrito salvo. Vale a partir da próxima requisição.' : 'Restrição de acesso desligada no painel (o Cloudflare Access continua valendo).');
}

// ── Configurações: harvey.yaml as a form ──
//
// Declarative: every field says where it lives in the YAML, how to edit it,
// and — the important part — what it means in plain words.

const CONFIG_SECTIONS = [
  {id: 'persona', title: 'Quem assina os e-mails', intro: 'A identidade que aparece pros prospects. Use uma pessoa real da empresa: cold email de "robô" não funciona.', fields: [
    {path: 'persona.name', label: 'Nome', type: 'text', help: 'Nome de quem assina. Ex.: "Ana Souza".'},
    {path: 'persona.role', label: 'Cargo', type: 'text', help: 'Como essa pessoa se apresenta. Ex.: "Consultora comercial".'},
    {path: 'persona.company', label: 'Empresa', type: 'text', help: 'O nome da sua empresa, do jeito que aparece no e-mail.'},
    {path: 'persona.email', label: 'E-mail de envio', type: 'text', help: 'O endereço da caixa conectada na aba E-mail. Precisa bater com ela.'},
    {path: 'persona.linkedin', label: 'LinkedIn', type: 'text', help: 'Link do perfil, pra assinatura. Pode deixar em branco.'},
    {path: 'persona.tone', label: 'Tom de voz', type: 'text', help: 'Três ou quatro palavras que descrevem como escrever. Ex.: "direto, consultivo, sem formalidade".'},
  ]},
  {id: 'product', title: 'O que você vende', intro: 'O treinamento preenche isso sozinho a partir do seu site. Revise: é daqui que saem os argumentos dos e-mails.', fields: [
    {path: 'product.name', label: 'Nome do produto ou serviço', type: 'text'},
    {path: 'product.description', label: 'Descrição em uma ou duas frases', type: 'textarea', help: 'O que faz e pra quem. Sem jargão.'},
    {path: 'product.pricing', label: 'Preço', type: 'text', help: 'Como você fala de preço. Ex.: "a partir de R$ 490/mês" ou "sob consulta".'},
    {path: 'product.key_benefits', label: 'Principais benefícios', type: 'list', help: 'Um por linha. Resultados concretos, não funcionalidades. Ex.: "Reduz o tempo de fechamento em 30%".'},
    {path: 'product.objection_responses', label: 'Objeções e respostas', type: 'map', help: 'Objeção à esquerda, sua resposta à direita. Ex.: "muito caro" → "Entendo. A maioria dos clientes recupera o valor em 30 dias…".', keyLabel: 'Objeção', valueLabel: 'Sua resposta'},
  ]},
  {id: 'offer', title: 'Oferta e como fechar', intro: 'O Harvey nunca abre com oferta. Isso aqui entra só depois que a pessoa demonstra interesse.', fields: [
    {path: 'product.offer.primary', label: 'Oferta principal', type: 'text', help: 'Ex.: "Assinatura mensal a partir de R$ 490".'},
    {path: 'product.offer.entry', label: 'Porta de entrada', type: 'text', help: 'Algo de baixo compromisso. Ex.: "14 dias grátis", "diagnóstico gratuito".'},
    {path: 'product.offer.goal', label: 'Objetivo do contato', type: 'select', options: [['book_call', 'Marcar uma conversa'], ['start_trial', 'Começar um teste'], ['get_reply', 'Só conseguir uma resposta']]},
    {path: 'product.offer.booking_method', label: 'Como marcar a reunião', type: 'select', options: [['calendar_link', 'Mandar link de agenda'], ['suggest_times', 'Sugerir horários'], ['ask_preference', 'Perguntar a preferência']]},
    {path: 'product.offer.booking_url', label: 'Link da agenda', type: 'text', help: 'Calendly, Google Agenda etc. Só se escolheu "mandar link".'},
    {path: 'product.offer.meeting_duration', label: 'Duração da reunião', type: 'text', help: 'Ex.: "15 minutos".'},
    {path: 'product.offer.meeting_owner', label: 'Quem faz a reunião', type: 'text', help: 'Nome ou cargo. Ex.: "Ana" ou "nosso time comercial".'},
  ]},
  {id: 'icp', title: 'Cliente ideal', intro: 'Quem o Harvey deve procurar. Quanto mais específico, melhor a lista e mais relevante o e-mail.', fields: [
    {path: 'icp.industries', label: 'Setores', type: 'list', help: 'Um por linha. Ex.: "Clínicas odontológicas", "Escritórios de contabilidade".'},
    {path: 'icp.company_size', label: 'Porte da empresa', type: 'text', help: 'Ex.: "5 a 50 funcionários".'},
    {path: 'icp.titles', label: 'Cargos de quem decide', type: 'list', help: 'Um por linha. Ex.: "Sócio", "Diretor comercial", "Dono".'},
    {path: 'icp.geography', label: 'Cidades ou regiões', type: 'list', help: 'Uma por linha. Ex.: "Curitiba, PR". A aba Descobrir usa isso pra buscar.'},
    {path: 'icp.hiring_signals', label: 'Vagas que indicam compra', type: 'list', help: 'Opcional. Cargos que, se a empresa está contratando, mostram que ela precisa de você agora. Ex.: "Analista de marketing".'},
  ]},
  {id: 'email_rules', title: 'Canal de e-mail: regras de envio', intro: 'Os freios. Comece conservador: 10 a 20 por dia numa caixa nova, com aprovação ligada.', fields: [
    {path: 'channels.email.enabled', label: 'Enviar e-mails', type: 'bool', help: 'Desligado, o Harvey só prospecta e escreve; nada sai.'},
    {path: 'channels.email.max_daily_sends', label: 'Máximo de e-mails por dia', type: 'number', min: 0, max: 500, help: 'Teto, não meta. 50 é o padrão; numa caixa nova use 10 a 20 e suba devagar.'},
    {path: 'channels.email.require_approval', label: 'Cada e-mail espera minha aprovação', type: 'bool', help: 'Ligado (recomendado), todo e-mail para na Caixa de saída até você aprovar. Desligue só quando confiar no que ele escreve.'},
    {path: 'channels.email.send_to_risky', label: 'Enviar pra domínios catch-all', type: 'bool', help: 'Domínios que aceitam qualquer endereço. Desligado é o mais seguro: evita bounces.'},
    {path: 'channels.email.max_bounce_rate', label: 'Pausar tudo se os bounces passarem de', type: 'percent01', help: 'Fração dos envios. 0,05 = 5%. Acima disso o Harvey pausa sozinho e avisa em Hoje. 0 desliga a trava.'},
  ]},
  {id: 'linkedin_rules', title: 'Canal LinkedIn', intro: 'Desligado por padrão. Automação viola os termos do LinkedIn e pode bloquear a conta.', fields: [
    {path: 'channels.linkedin.enabled', label: 'Usar LinkedIn', type: 'bool'},
    {path: 'channels.linkedin.max_daily_connections', label: 'Máximo de convites por dia', type: 'number', min: 0, max: 100},
    {path: 'channels.linkedin.max_daily_messages', label: 'Máximo de mensagens por dia', type: 'number', min: 0, max: 100},
  ]},
  {id: 'usage', title: 'Uso da IA e horários', intro: 'Quanto da sua assinatura o Harvey pode gastar e quando ele deve ficar quieto.', fields: [
    {path: 'usage.max_daily_claude_percent', label: 'Limite da cota da assinatura (%)', type: 'slider', min: 5, max: 100, step: 5, help: 'O Harvey lê a cota real da sua assinatura (a mesma barra da aba IA) e para de pensar quando ela passa desse número, pra sobrar pro seu uso pessoal do Claude. 80% é o padrão. Vale pra Claude e pra Codex.'},
    {path: 'usage.heartbeat_interval_minutes', label: 'Acordar a cada (minutos)', type: 'number', min: 1, max: 240, help: 'De quanto em quanto tempo ele checa se tem trabalho. 15 é o padrão; menos gasta mais cota.'},
    {path: 'usage.quiet_hours.start', label: 'Silêncio começa às', type: 'time', help: 'Nesse intervalo ele não faz nada. Formato 24h.'},
    {path: 'usage.quiet_hours.end', label: 'Silêncio termina às', type: 'time'},
    {path: 'usage.quiet_hours.timezone', label: 'Fuso horário', type: 'timezone', help: 'Ex.: America/Sao_Paulo.'},
  ]},
  {id: 'brain', title: 'IA', intro: 'Também editável na aba IA.', fields: [
    {path: 'brain.provider', label: 'Provedor', type: 'select', options: [['claude', 'Claude (assinatura)'], ['codex', 'Codex (ChatGPT)']]},
    {path: 'brain.model', label: 'Modelo', type: 'text', help: 'Em branco usa o padrão do provedor.'},
    {path: 'brain.reasoning_effort', label: 'Esforço de raciocínio (Codex)', type: 'select', options: [['', 'padrão'], ['minimal', 'minimal'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh']]},
  ]},
  {id: 'compliance', title: 'Conformidade', intro: 'Checagem jurídica por destinatário via Precleared antes do primeiro e-mail frio. Opcional.', fields: [
    {path: 'compliance.precleared.enabled', label: 'Checar cada destinatário no Precleared', type: 'bool', help: 'Precisa informar o país abaixo (GB ou US). Chave opcional na aba Chaves.'},
    {path: 'compliance.precleared.sender_country', label: 'País da sua empresa', type: 'select', options: [['', '— não informado'], ['US', 'Estados Unidos (US)'], ['GB', 'Reino Unido (GB)']], help: 'O Precleared só cobre esses dois por enquanto.'},
    {path: 'compliance.precleared.address_source', label: 'De onde vêm os endereços', type: 'select', options: [['unknown', 'Não sei (padrão honesto)'], ['published', 'Publicados no site da empresa'], ['scraped', 'Coletados automaticamente'], ['bought', 'Comprados'], ['given', 'Fornecidos pela pessoa']]},
  ]},
];

let _configData = null;

function _get(obj, path) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj);
}

function _set(obj, path, value) {
  const keys = path.split('.');
  let o = obj;
  for (const k of keys.slice(0, -1)) { if (typeof o[k] !== 'object' || o[k] === null) o[k] = {}; o = o[k]; }
  o[keys[keys.length - 1]] = value;
}

function _fid(path) { return 'cfg-' + path.replace(/\./g, '-'); }

function _renderField(f, value) {
  const id = _fid(f.path);
  let input = '';
  const v = value === undefined || value === null ? '' : value;
  switch (f.type) {
    case 'textarea':
      input = '<textarea class="form-input" id="' + id + '" rows="3">' + escHtml(v) + '</textarea>'; break;
    case 'list':
      input = '<textarea class="form-input" id="' + id + '" rows="4" placeholder="um por linha">' + escHtml((Array.isArray(v) ? v : []).join('\n')) + '</textarea>'; break;
    case 'map': {
      const rows = Object.entries(v && typeof v === 'object' ? v : {});
      input = '<div class="map-rows" id="' + id + '">' +
        rows.map(([k, val]) => _mapRow(k, val, f)).join('') + '</div>' +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="addMapRow(\'' + id + '\')">+ Adicionar</button>';
      break;
    }
    case 'bool':
      input = '<label class="check-row"><input type="checkbox" id="' + id + '"' + (v ? ' checked' : '') + '> ' + escHtml(f.help || '') + '</label>'; break;
    case 'number':
      input = '<input class="form-input" type="number" id="' + id + '" value="' + escHtml(v) + '"' +
        (f.min !== undefined ? ' min="' + f.min + '"' : '') + (f.max !== undefined ? ' max="' + f.max + '"' : '') + ' style="max-width:160px">'; break;
    case 'percent01':
      input = '<input class="form-input" type="number" id="' + id + '" value="' + escHtml(v) + '" min="0" max="1" step="0.01" style="max-width:160px">'; break;
    case 'slider':
      input = '<div class="slider-row"><input type="range" id="' + id + '" value="' + escHtml(v || f.min) + '" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '" oninput="document.getElementById(\'' + id + '-out\').textContent = this.value + \'%\'">' +
        '<output id="' + id + '-out" class="slider-out">' + escHtml(v || f.min) + '%</output></div>'; break;
    case 'select':
      input = '<select class="form-input" id="' + id + '" style="max-width:320px">' +
        f.options.map(([val, label]) => '<option value="' + escHtml(val) + '"' + (String(v) === String(val) ? ' selected' : '') + '>' + escHtml(label) + '</option>').join('') + '</select>'; break;
    case 'time':
      input = '<input class="form-input" type="time" id="' + id + '" value="' + escHtml(v) + '" style="max-width:160px">'; break;
    case 'timezone': {
      const tzs = (_configData && _configData.timezones) || [];
      input = '<input class="form-input" id="' + id + '" list="tz-list" value="' + escHtml(v) + '" style="max-width:320px">' +
        '<datalist id="tz-list">' + tzs.map(t => '<option value="' + escHtml(t) + '">').join('') + '</datalist>';
      break;
    }
    default:
      input = '<input class="form-input" type="text" id="' + id + '" value="' + escHtml(v) + '">';
  }
  const help = f.type === 'bool' ? '' : (f.help ? '<div class="field-help">' + escHtml(f.help) + '</div>' : '');
  return '<div class="form-group cfg-field" data-path="' + f.path + '">' +
    (f.type === 'bool' ? '<div class="form-label">' + escHtml(f.label) + '</div>' : '<label class="form-label" for="' + id + '">' + escHtml(f.label) + '</label>') +
    input + help + '<div class="field-error" id="' + id + '-err"></div></div>';
}

function _mapRow(k, v, f) {
  return '<div class="map-row">' +
    '<input class="form-input" placeholder="' + escHtml(f.keyLabel || 'chave') + '" value="' + escHtml(k) + '" data-k>' +
    '<input class="form-input" placeholder="' + escHtml(f.valueLabel || 'valor') + '" value="' + escHtml(v) + '" data-v>' +
    '<button type="button" class="btn btn-secondary btn-sm" onclick="this.parentNode.remove()" title="remover">×</button></div>';
}

function addMapRow(id) {
  const box = document.getElementById(id);
  const f = CONFIG_SECTIONS.flatMap(s => s.fields).find(x => _fid(x.path) === id) || {};
  box.insertAdjacentHTML('beforeend', _mapRow('', '', f));
  const last = box.lastElementChild.querySelector('input');
  if (last) last.focus();
}

async function loadConfig() {
  const data = await api('/api/config');
  const form = document.getElementById('config-form');
  if (!data) { form.innerHTML = offlineState(); return; }
  _configData = data;
  const src = data.effective || data.raw || {};

  const banner = document.getElementById('config-banner');
  if (data.errors && data.errors.length) {
    banner.innerHTML = '<div class="test-result error">O arquivo atual tem campos inválidos: ' +
      data.errors.map(e => escHtml(e.field + ' — ' + e.message)).join('; ') + '. Corrija abaixo e salve.</div>';
  } else if (data.is_template) {
    banner.innerHTML = '<div class="test-result pending">Ainda está com os valores de exemplo. Use a aba <b>Treinar</b> pra preencher a partir do seu site, ou edite aqui.</div>';
  } else {
    banner.innerHTML = '';
  }

  document.getElementById('config-nav').innerHTML = CONFIG_SECTIONS.map(s =>
    '<a href="#config" onclick="document.getElementById(\'cfg-sec-' + s.id + '\').scrollIntoView({behavior:\'smooth\',block:\'start\'});return false">' + escHtml(s.title) + '</a>').join('');

  form.innerHTML = CONFIG_SECTIONS.map(s =>
    '<div class="card cfg-section" id="cfg-sec-' + s.id + '"><h2>' + escHtml(s.title) + '</h2>' +
    (s.intro ? '<p class="lede">' + escHtml(s.intro) + '</p>' : '') +
    s.fields.map(f => _renderField(f, _get(src, f.path))).join('') + '</div>').join('');

  document.getElementById('config-save-note').textContent = 'Grava em ' + (data.writes_to || 'harvey.local.yaml') + '.';
}

function _collectConfig() {
  const out = {};
  for (const s of CONFIG_SECTIONS) for (const f of s.fields) {
    const el = document.getElementById(_fid(f.path));
    if (!el) continue;
    let v;
    switch (f.type) {
      case 'list': v = el.value.split('\n').map(x => x.trim()).filter(Boolean); break;
      case 'map': {
        v = {};
        el.querySelectorAll('.map-row').forEach(row => {
          const k = row.querySelector('[data-k]').value.trim();
          const val = row.querySelector('[data-v]').value.trim();
          if (k) v[k] = val;
        });
        break;
      }
      case 'bool': v = !!el.checked; break;
      case 'number': case 'slider': v = el.value === '' ? 0 : Number(el.value); break;
      case 'percent01': v = el.value === '' ? 0 : Number(el.value); break;
      default: v = el.value.trim();
    }
    _set(out, f.path, v);
  }
  return out;
}

async function saveConfig() {
  document.querySelectorAll('.field-error').forEach(e => { e.textContent = ''; });
  const body = _collectConfig();
  const r = await api('/api/config', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r && r.success) {
    showToast('Configurações salvas. ' + (r.note || ''), 'success');
    loadConfig();
    return;
  }
  if (r && r.errors) {
    let first = null;
    for (const e of r.errors) {
      const el = document.getElementById(_fid(e.field) + '-err');
      if (el) { el.textContent = e.message; first = first || el; }
    }
    if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
    showToast('Alguns campos estão inválidos: ' + r.errors.map(e => e.field).join(', '), 'error');
  } else {
    showToast((r && r.message) || 'Não consegui salvar.', 'error');
  }
}

// ── Treinar ──

let _trainPoll = null;

async function loadTrain() {
  const st = await api('/api/train/status');
  const box = document.getElementById('train-status');
  const log = document.getElementById('train-log');
  if (!st) { box.innerHTML = offlineState(); return; }
  document.getElementById('train-run').disabled = st.running;
  document.getElementById('train-stop').style.display = st.running ? '' : 'none';
  if (st.running) {
    box.innerHTML = '<div class="test-result pending">Treinando… lendo o site e escrevendo o conhecimento do produto. Pode levar alguns minutos.</div>';
  } else if (st.exit_code === 0) {
    box.innerHTML = '<div class="test-result success">Treinamento concluído. Revise em Configurações.</div>';
  } else if (st.exit_code !== null && st.exit_code !== undefined) {
    box.innerHTML = '<div class="test-result error">O treinamento terminou com erro (código ' + st.exit_code + '). Veja o registro abaixo.</div>';
  } else {
    box.innerHTML = st.trained
      ? '<div class="test-result success">O Harvey já tem conhecimento do produto. Treine de novo se o site mudou.</div>'
      : '';
  }
  if (st.lines && st.lines.length) {
    log.textContent = st.lines.join('\n');
    log.scrollTop = log.scrollHeight;
  }
  clearTimeout(_trainPoll);
  if (st.running && currentTab === 'train') _trainPoll = setTimeout(loadTrain, 3000);
}

async function runTrain() {
  const url = document.getElementById('train-url').value.trim();
  if (!url) { showToast('Cole o endereço do site primeiro.', 'error'); return; }
  const max_pages = Number(document.getElementById('train-pages').value) || 100;
  const r = await api('/api/train/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url, max_pages})});
  if (r && r.success) { showToast('Treinamento iniciado.', 'success'); loadTrain(); }
  else showToast((r && r.message) || 'Não consegui iniciar.', 'error');
}

async function stopTrain() {
  const r = await api('/api/train/stop', {method: 'POST'});
  if (r && r.success) { showToast('Treinamento interrompido.', 'success'); loadTrain(); }
}

// ── Aparência ──

async function loadAppearance() {
  const b = await api('/api/branding');
  if (!b) return;
  document.getElementById('brand-name-input').value = b.app_name || '';
  document.getElementById('brand-tagline-input').value = b.tagline || '';
  document.getElementById('theme-default').value = b.theme_default || 'auto';
  const accent = b.accent || '';
  document.getElementById('accent-hex').value = accent;
  document.getElementById('accent-color').value = accent || getComputedStyle(document.documentElement).getPropertyValue('--ink').trim() || '#1E1B17';
  const prev = document.getElementById('logo-preview');
  if (b.logo_url) prev.innerHTML = '<img src="' + escHtml(b.logo_url) + '" alt="logo">';
  else prev.innerHTML = '<span class="muted">sem logo</span>';
  document.getElementById('logo-remove').style.display = b.logo_url ? '' : 'none';
}

function previewAccent(v) {
  const hex = (v || '').trim();
  if (/^#[0-9a-fA-F]{6}$/.test(hex)) {
    document.getElementById('accent-color').value = hex;
    document.getElementById('accent-hex').value = hex;
    applyAccent(hex);
  }
}

function clearAccent() {
  document.getElementById('accent-hex').value = '';
  applyAccent('');
}

async function saveAppearance() {
  const body = {
    app_name: document.getElementById('brand-name-input').value.trim(),
    tagline: document.getElementById('brand-tagline-input').value.trim(),
    theme_default: document.getElementById('theme-default').value,
    accent: document.getElementById('accent-hex').value.trim(),
  };
  const r = await api('/api/branding', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r && r.success) { showToast('Aparência salva.', 'success'); await loadBranding(); loadAppearance(); }
  else showToast((r && r.message) || 'Não consegui salvar.', 'error');
}

function uploadLogo(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) { showToast('A imagem precisa ter até 2 MB.', 'error'); input.value = ''; return; }
  const reader = new FileReader();
  reader.onload = async () => {
    const data = String(reader.result).split(',')[1] || '';
    const r = await api('/api/branding/logo', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mime: file.type, data})});
    input.value = '';
    if (r && r.success) { showToast('Logo enviada.', 'success'); await loadBranding(); loadAppearance(); }
    else showToast((r && r.message) || 'Não consegui enviar a logo.', 'error');
  };
  reader.readAsDataURL(file);
}

async function removeLogo() {
  const r = await api('/api/branding/logo', {method: 'DELETE'});
  if (r && r.success) { showToast('Logo removida.', 'success'); await loadBranding(); loadAppearance(); }
}

// ── Boot: branding first (so nothing flashes), then the tab the URL names ──

(async function bootAdmin() {
  await loadBranding();
  loadMe();

  // OAuth callbacks come back as /?ok=gmail#email or /?error=...#email
  const params = new URLSearchParams(window.location.search);
  if (params.get('ok')) showToast('Caixa conectada: ' + params.get('ok') + '.', 'success');
  if (params.get('error')) showToast(params.get('error'), 'error');
  if (params.get('ok') || params.get('error')) {
    try { history.replaceState(null, '', window.location.pathname + window.location.hash); } catch { /* ignore */ }
  }

  const hash = (window.location.hash || '').replace('#', '');
  if (hash && hash !== 'today' && document.getElementById(hash) && document.getElementById(hash).classList.contains('section')) {
    goTab(hash);
  }

  // Quiet background checks so the nav shows what still needs attention.
  const [brain, email] = await Promise.all([api('/api/brain/status'), api('/api/email/status')]);
  if (brain) navCount('nav-brain', (brain.provider !== 'codex' && brain.token_source === 'none') ? 1 : 0);
  if (email) {
    const ready = (email.provider === 'gmail' && email.gmail_connected) || (email.provider === 'microsoft' && email.microsoft_connected) ||
                  email.provider === 'smtp' || email.provider === 'instantly';
    navCount('nav-email', ready ? 0 : 1);
  }
})();
