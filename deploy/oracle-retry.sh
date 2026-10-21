#!/usr/bin/env bash
# ============================================================================
# Tenta criar a VM gratuita (Ampere A1) na Oracle Cloud até conseguir vaga.
#
# Feito pra rodar no Cloud Shell da própria Oracle (ícone ">_" no topo do
# console), que já vem com a CLI "oci" autenticada. Não precisa instalar nada.
# O Cloud Shell cai com 20 min sem teclado e tem teto de 24 h; pra rodar sem
# babá, instale a CLI no seu PC (Mac/Linux/WSL):
#   bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
#   oci setup config      # pede o OCID do usuário e do tenancy (Identity → My profile)
#   e suba a chave pública que ele gerar em My profile → API keys → Add API key
# Depois é o mesmo:  bash oracle-retry.sh
#
#   curl -fsSL https://raw.githubusercontent.com/gmahovlic/harvey/main/deploy/oracle-retry.sh -o oracle-retry.sh
#   bash oracle-retry.sh
#
# Pré-requisito: a VCN com sub-rede PÚBLICA já criada (Networking → Virtual
# cloud networks → Start VCN Wizard → "Create VCN with Internet Connectivity").
#
# O que ele faz: descobre sozinho compartimento, availability domain, sub-rede
# pública e a imagem Ubuntu 24.04 ARM mais nova; gera um par de chaves SSH
# (~/.ssh/harvey.key) se você não passar uma; e aí tenta criar a instância a
# cada INTERVAL segundos até a Oracle aceitar. "Out of host capacity" é o
# normal: ele só avisa e tenta de novo. Qualquer outro erro ele mostra e para.
#
# Variáveis opcionais (ex.: OCPUS=1 MEM=6 bash oracle-retry.sh):
#   OCPUS      núcleos (padrão 2; o gratuito vai até 4 somando todas as VMs A1)
#   MEM        memória em GB (padrão 8; até 24 no gratuito)
#   NAME       nome da instância (padrão harvey)
#   INTERVAL   segundos entre tentativas (padrão 75; menos que isso a Oracle bloqueia)
#   SSH_PUB    sua chave pública SSH (conteúdo "ssh-rsa AAAA…"); se vazio, gera uma
#   VCN_NAME   nome da VCN (padrão: procura a sub-rede com "public" no nome)
#   DEPOIS     cole o IP mostrado no final em: ssh -i ~/.ssh/harvey.key ubuntu@IP
# ============================================================================
set -u

OCPUS="${OCPUS:-2}"
MEM="${MEM:-8}"
NAME="${NAME:-harvey}"
INTERVAL="${INTERVAL:-75}"
SHAPE="VM.Standard.A1.Flex"
COMPARTMENT="${COMPARTMENT:-${OCI_TENANCY:-}}"
# Fora do Cloud Shell (CLI instalada no seu PC), o OCID do tenancy está em ~/.oci/config.
if [ -z "$COMPARTMENT" ] && [ -f "$HOME/.oci/config" ]; then
  COMPARTMENT="$(sed -n 's/^tenancy *= *//p' "$HOME/.oci/config" | head -1)"
fi

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }

command -v oci >/dev/null || die "A CLI 'oci' não está disponível. Rode isto no Cloud Shell da Oracle (ícone >_ no topo do console)."
[ -n "$COMPARTMENT" ] || die "Não achei o compartimento. No Cloud Shell a variável OCI_TENANCY vem preenchida; fora dele, passe COMPARTMENT=ocid1.tenancy...."

say "Descobrindo a região, o availability domain e a rede"
note "compartimento: $COMPARTMENT"
AD_OUT="$(oci iam availability-domain list --compartment-id "$COMPARTMENT" --query 'data[0].name' --raw-output 2>&1)"
AD_RC=$?
if [ $AD_RC -ne 0 ] || [ -z "$AD_OUT" ] || [ "$AD_OUT" = "null" ]; then
  printf '%s\n' "$AD_OUT" | tail -15
  die "Não consegui listar os availability domains (a CLI respondeu acima). Se falar em NotAuthenticated/401: no Cloud Shell, feche e abra o shell de novo; no PC, confira o 'oci setup config' e a chave de API em My profile → API keys."
fi
AD="$(printf '%s' "$AD_OUT" | tail -1)"
note "availability domain: $AD"

# As consultas JMESPath com aspas internas ("display-name") quebram em alguns
# terminais (Git Bash no Windows, por exemplo), então a filtragem é feita aqui
# com awk sobre o JSON bruto. Nos objetos da CLI os campos vêm em ordem
# alfabética: display-name antes de id, que vem antes de prohibit-public-ip-on-vnic.
SUBNETS_JSON="$(oci network subnet list --compartment-id "$COMPARTMENT" --all 2>/dev/null)"
pick_subnet() {
  printf '%s\n' "$SUBNETS_JSON" | awk -F'"' -v want="${1:-}" '
    /"display-name"/ { n = $4 }
    /^ *"id":/        { i = $4 }
    /"prohibit-public-ip-on-vnic": *false/ {
      if (want == "" || index(n, want) > 0) { print i "\t" n; exit }
    }'
}
PICK="$(pick_subnet "${VCN_NAME:-}")"
SUBNET="${PICK%%	*}"
SUBNET_NAME="${PICK#*	}"
[ -n "$SUBNET" ] || die "Não achei uma sub-rede pública. Crie a VCN pelo wizard (Create VCN with Internet Connectivity) e rode de novo."
note "sub-rede pública: $SUBNET_NAME"

say "Escolhendo a imagem Ubuntu 24.04 (ARM)"
IMAGES_JSON="$(oci compute image list --compartment-id "$COMPARTMENT" --shape "$SHAPE" \
  --sort-by TIMECREATED --sort-order DESC --all 2>/dev/null)"
PICK="$(printf '%s\n' "$IMAGES_JSON" | awk -F'"' '
  /"display-name"/ { n = $4; want = (n ~ /Canonical-Ubuntu-24\.04-aarch64/ && n !~ /Minimal/) }
  want && /^ *"id":/ { print $4 "\t" n; exit }')"
IMAGE="${PICK%%	*}"
IMAGE_NAME="${PICK#*	}"
if [ -z "$IMAGE" ]; then
  note "Imagens disponíveis pra $SHAPE (pra eu entender o que a Oracle devolveu):"
  printf '%s\n' "$IMAGES_JSON" | awk -F'"' '/"display-name"/ { print "      " $4 }' | head -30
  die "Não achei a imagem Canonical Ubuntu 24.04 ARM. Me mande a lista acima."
fi
note "imagem: $IMAGE_NAME"

say "Chave SSH"
if [ -n "${SSH_PUB:-}" ]; then
  PUB="$SSH_PUB"
  note "usando a chave pública que você passou"
else
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  if [ ! -f "$HOME/.ssh/harvey.key" ]; then
    ssh-keygen -q -t rsa -b 4096 -N "" -f "$HOME/.ssh/harvey.key" -C "harvey" \
      || die "Não consegui gerar a chave SSH."   # RSA: o Cloud Shell é FIPS e recusa ed25519
    note "gerei um par novo em ~/.ssh/harvey.key"
  fi
  PUB="$(cat "$HOME/.ssh/harvey.key.pub" 2>/dev/null)"
  note "IMPORTANTE: baixe ~/.ssh/harvey.key pro seu computador (menu ≡ do Cloud Shell → Download)."
fi
case "$PUB" in
  ssh-rsa\ *|ssh-ed25519\ *|ecdsa-sha2-*) ;;
  *) die "Chave SSH pública inválida ou vazia. Sem ela a VM nasceria inacessível, então parei. Apague ~/.ssh/harvey.key* e rode de novo, ou passe SSH_PUB=\"ssh-rsa AAAA...\"." ;;
esac

say "Tentando criar '$NAME' ($SHAPE, $OCPUS OCPU, ${MEM} GB) a cada ${INTERVAL}s. Ctrl+C pra parar."
note "'Out of host capacity' é a Oracle sem vaga; o script continua sozinho. Deixe esta aba aberta."
attempt=0
auth_fail=0
while :; do
  attempt=$((attempt + 1))
  out="$(oci compute instance launch \
    --compartment-id "$COMPARTMENT" \
    --availability-domain "$AD" \
    --shape "$SHAPE" \
    --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEM}" \
    --image-id "$IMAGE" \
    --subnet-id "$SUBNET" \
    --assign-public-ip true \
    --display-name "$NAME" \
    --metadata "{\"ssh_authorized_keys\": \"$PUB\"}" 2>&1)"
  if printf '%s' "$out" | grep -q '"lifecycle-state"'; then
    INSTANCE_ID="$(printf '%s' "$out" | sed -n 's/.*"id": "\(ocid1\.instance[^"]*\)".*/\1/p' | head -1)"
    say "CONSEGUIU na tentativa $attempt! Instância criada: $INSTANCE_ID"
    note "esperando ficar RUNNING e pegar o IP público..."
    for _ in $(seq 1 40); do
      sleep 10
      IP="$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" --query 'data[0]."public-ip"' --raw-output 2>/dev/null)"
      if [ -n "$IP" ] && [ "$IP" != "null" ]; then
        say "IP público: $IP"
        note "No seu computador:  ssh -i harvey.key ubuntu@$IP"
        note "Depois, dentro da VM:  curl -fsSL https://raw.githubusercontent.com/gmahovlic/harvey/main/deploy/install.sh | bash"
        exit 0
      fi
    done
    note "A instância existe mas o IP ainda não apareceu; veja em Compute → Instances → $NAME."
    exit 0
  fi
  if printf '%s' "$out" | grep -qiE 'Out of host capacity|InternalError|TooManyRequests|429|500|503'; then
    auth_fail=0
    printf '%s  tentativa %d: sem vaga ainda\n' "$(date '+%d/%m %H:%M:%S')" "$attempt"
  elif printf '%s' "$out" | grep -q 'NotAuthenticated'; then
    # O token do Cloud Shell vence e é renovado sozinho quando você mexe no
    # terminal; um 401 isolado não é motivo pra parar.
    auth_fail=$((auth_fail + 1))
    printf '%s  tentativa %d: a Oracle recusou o login (token vencido?). Clique no terminal e aperte Enter; tentando de novo (%d/5)\n' "$(date '+%d/%m %H:%M:%S')" "$attempt" "$auth_fail"
    if [ "$auth_fail" -ge 5 ]; then
      die "Cinco recusas de login seguidas. No Cloud Shell: feche o painel do shell e abra de novo, depois rode o script. No PC: confira a chave de API em My profile → API keys."
    fi
  else
    say "A Oracle respondeu com um erro diferente de falta de vaga. Parando pra você ver:"
    printf '%s\n' "$out" | tail -20
    exit 1
  fi
  sleep "$INTERVAL"
done
