#!/usr/bin/env bash
# ============================================================================
# Instalador do Harvey numa VM Ubuntu (testado pra Oracle Cloud Always Free,
# shape Ampere A1 / Ubuntu 24.04, mas serve em qualquer Ubuntu 22.04+ x86 ou ARM).
#
# O que ele faz, em ordem:
#   1. instala Python, git, curl
#   2. clona (ou atualiza) o repositório em ~/harvey e cria o ambiente Python
#   3. instala a CLI do Claude Code (o Harvey pensa através dela)
#   4. instala o cloudflared (o túnel que expõe o painel no seu domínio)
#   5. cria os serviços do sistema (painel + agente) e liga o painel
#   6. se você passou o token do túnel, liga o túnel também
#
# Uso (na VM, como o usuário normal, não root):
#   curl -fsSL https://raw.githubusercontent.com/SEU_USUARIO/harvey/main/deploy/install.sh | bash
# ou, com o túnel já criado no Cloudflare:
#   curl -fsSL .../install.sh | TUNNEL_TOKEN='eyJ...' bash
#
# Variáveis opcionais:
#   HARVEY_REPO   URL do git (padrão: https://github.com/gmahovlic/harvey.git)
#   HARVEY_DIR    pasta de instalação (padrão: ~/harvey)
#   TUNNEL_TOKEN  token do túnel do Cloudflare Zero Trust
# Pode rodar de novo quantas vezes quiser: ele só atualiza o que mudou.
# ============================================================================
set -euo pipefail

HARVEY_REPO="${HARVEY_REPO:-https://github.com/gmahovlic/harvey.git}"
HARVEY_DIR="${HARVEY_DIR:-$HOME/harvey}"
TUNNEL_TOKEN="${TUNNEL_TOKEN:-}"
RUN_USER="$(id -un)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$RUN_USER" != "root" ] || die "Rode como usuário normal (ex.: ubuntu), não como root. O script usa sudo quando precisa."
command -v sudo >/dev/null || die "sudo não encontrado."
command -v apt-get >/dev/null || die "Este instalador é pra Ubuntu/Debian (apt)."

# ── 1. Pacotes do sistema ───────────────────────────────────────────────────
say "Instalando pacotes do sistema"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates lsb-release >/dev/null
PY=python3
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  note "Python do sistema é antigo; tentando python3.12"
  sudo apt-get install -y -qq python3.12 python3.12-venv >/dev/null || die "Preciso de Python 3.11 ou mais novo."
  PY=python3.12
fi
note "Python: $($PY --version)"

# ── 2. Código + ambiente Python ─────────────────────────────────────────────
if [ -d "$HARVEY_DIR/.git" ]; then
  say "Atualizando o Harvey em $HARVEY_DIR"
  git -C "$HARVEY_DIR" pull --ff-only || note "Não consegui atualizar (mudanças locais?). Seguindo com o que está."
else
  say "Baixando o Harvey para $HARVEY_DIR"
  git clone --depth 1 "$HARVEY_REPO" "$HARVEY_DIR"
fi
cd "$HARVEY_DIR"
if [ ! -x .venv/bin/python ]; then
  say "Criando o ambiente Python"
  "$PY" -m venv .venv
fi
say "Instalando dependências (pode levar um ou dois minutos)"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
mkdir -p data
[ -f .env ] || : > .env
chmod 600 .env

# ── 3. CLI do Claude Code ───────────────────────────────────────────────────
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
  say "Claude Code já instalado: $("$HOME/.local/bin/claude" --version 2>/dev/null || claude --version 2>/dev/null || echo ok)"
else
  say "Instalando a CLI do Claude Code"
  curl -fsSL https://claude.ai/install.sh | bash || note "Não consegui instalar o Claude Code agora. Rode depois: curl -fsSL https://claude.ai/install.sh | bash"
fi
grep -q 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

# ── 4. cloudflared ──────────────────────────────────────────────────────────
if command -v cloudflared >/dev/null 2>&1; then
  say "cloudflared já instalado: $(cloudflared --version 2>/dev/null | head -1)"
else
  say "Instalando o cloudflared"
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  if curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null \
     && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
        | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null \
     && sudo apt-get update -qq && sudo apt-get install -y -qq cloudflared >/dev/null; then
    note "ok (via apt)"
  else
    note "apt falhou; baixando o pacote direto do GitHub"
    ARCH="$(dpkg --print-architecture)"   # amd64 ou arm64
    curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
    sudo dpkg -i /tmp/cloudflared.deb >/dev/null
  fi
fi

# ── 5. Serviços do sistema ──────────────────────────────────────────────────
say "Criando os serviços (painel + agente)"
for unit in harvey-dashboard harvey-agent; do
  sed -e "s|__USER__|$RUN_USER|g" -e "s|__HOME__|$HOME|g" "deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
# O painel liga/desliga o agente com systemctl, sem pedir senha.
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl start harvey-agent, /usr/bin/systemctl stop harvey-agent, /usr/bin/systemctl restart harvey-agent\n' "$RUN_USER" \
  | sudo tee /etc/sudoers.d/harvey >/dev/null
sudo chmod 440 /etc/sudoers.d/harvey
sudo systemctl daemon-reload
sudo systemctl enable --now harvey-dashboard >/dev/null
sudo systemctl enable harvey-agent >/dev/null   # liga no boot; o painel decide quando rodar
sleep 2
if curl -fsS -o /dev/null http://127.0.0.1:5555/api/branding; then
  note "painel respondendo em http://127.0.0.1:5555 (só dentro da VM; o túnel expõe)"
else
  note "o painel ainda não respondeu; veja: sudo journalctl -u harvey-dashboard -n 50"
fi

# ── 6. Túnel do Cloudflare ──────────────────────────────────────────────────
if [ -n "$TUNNEL_TOKEN" ]; then
  say "Ligando o túnel do Cloudflare"
  sudo cloudflared service install "$TUNNEL_TOKEN" >/dev/null 2>&1 || sudo cloudflared service install "$TUNNEL_TOKEN"
  sudo systemctl enable --now cloudflared >/dev/null 2>&1 || true
  note "túnel instalado. Confira em: sudo systemctl status cloudflared"
else
  say "Túnel do Cloudflare: ainda não configurado"
  note "Quando tiver o token (Zero Trust → Networks → Tunnels → seu túnel → Install connector), rode:"
  note "  sudo cloudflared service install SEU_TOKEN"
fi

say "Pronto"
note "Próximos passos estão em docs/deploy-oracle.md (ou no painel, aba Ajuda)."
note "Comandos úteis:"
note "  sudo systemctl status harvey-dashboard harvey-agent cloudflared"
note "  sudo journalctl -u harvey-dashboard -f      # log do painel"
note "  tail -f $HARVEY_DIR/data/harvey.log         # log do agente"
