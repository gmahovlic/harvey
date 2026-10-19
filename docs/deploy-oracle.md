# Harvey na nuvem de graça: Oracle Cloud + Cloudflare

Este guia coloca o Harvey rodando 24 h numa VM gratuita da Oracle, com o painel
acessível em um subdomínio seu (ex.: `harvey.seudominio.com.br`), protegido por
login (Google ou código por e-mail) e usando a **sua assinatura do Claude**, sem
API key e sem custo extra.

Tempo total: cerca de 40 minutos, a maior parte esperando a Oracle.

O que você vai precisar:

- Um domínio cujo DNS já está na Cloudflare (plano gratuito serve).
- Uma assinatura Claude Pro ou Max e o Claude Code instalado no seu computador.
- Um cartão de crédito pra abrir a conta da Oracle (ela não cobra no Always Free,
  mas exige o cartão pra validar).

As seções são independentes: se já tem a VM, pule pra 2; se já tem o túnel, pule pra 4.

---

## 1. Criar a VM gratuita na Oracle

1. Crie a conta em <https://www.oracle.com/cloud/free/>. Escolha a **home region**
   mais perto (São Paulo, `sa-saopaulo-1`, costuma ter vaga). A região não muda depois.
2. No console, menu ☰ → **Compute → Instances → Create instance**.
3. Preencha:
   - **Name**: `harvey`
   - **Image and shape → Change image**: *Canonical Ubuntu 24.04* (o aarch64, se perguntar)
   - **Change shape**: *Ampere* → `VM.Standard.A1.Flex`, **2 OCPUs e 8 GB** (ou menos; cabe no gratuito até 4 OCPU/24 GB)
   - **Networking**: deixe criar a VCN e sub-rede padrão, com **IP público**.
   - **Add SSH keys**: clique em *Generate a key pair for me* e **baixe a chave privada**
     (`ssh-key-….key`). Guarde: sem ela não entra na VM.
4. **Create**. Se der "Out of capacity", tente outro *availability domain* ou
   volte em uma hora; é comum nessa shape gratuita.
5. Quando ficar **Running**, copie o **Public IP address**.

Conectar (no seu computador, no terminal/PowerShell, na pasta onde a chave foi salva):

```bash
chmod 600 ssh-key-*.key          # só no Mac/Linux
ssh -i ssh-key-*.key ubuntu@SEU_IP_PUBLICO
```

Não precisa abrir nenhuma porta no firewall da Oracle: o túnel do Cloudflare é
uma conexão de **saída**, e o painel só escuta dentro da VM.

---

## 2. Instalar o Harvey na VM

Dentro da VM (depois do `ssh`), cole:

```bash
curl -fsSL https://raw.githubusercontent.com/gmahovlic/harvey/main/deploy/install.sh | bash
```

> Se o seu fork estiver em outro usuário/nome, troque a URL. Dá também pra passar
> `HARVEY_REPO=https://github.com/voce/harvey.git` antes do `bash`.

O instalador leva uns 3 minutos e, no final, mostra "Pronto". Ele:

- instala Python, Claude Code e cloudflared;
- cria dois serviços: `harvey-dashboard` (painel, já ligado) e `harvey-agent`
  (o ciclo de vendas, que você liga pelo painel);
- deixa o painel ouvindo só em `127.0.0.1:5555` dentro da VM.

Pra ver se está vivo: `sudo systemctl status harvey-dashboard`.

---

## 3. Expor o painel no seu domínio (Cloudflare Tunnel)

1. Acesse <https://one.dash.cloudflare.com/> (Zero Trust). Na primeira vez ele pede
   um nome de time, ex.: `seutime`. **Anote**: isso vira `seutime.cloudflareaccess.com`,
   o "domínio do time" que o painel pede depois. Escolha o plano **Free**.
2. **Networks → Tunnels → Create a tunnel → Cloudflared**. Nome: `harvey`. Save.
3. Na tela *Install and run a connector*, escolha **Debian / 64-bit (ou arm64-bit)** e
   copie **só o token** do comando mostrado (o trecho longo depois de `service install`).
4. Na VM, rode:

   ```bash
   sudo cloudflared service install COLE_O_TOKEN_AQUI
   ```

   O conector deve aparecer como **Connected** na página da Cloudflare em alguns segundos.
5. Aba **Public Hostname → Add a public hostname**:
   - **Subdomain**: `harvey` · **Domain**: seu domínio
   - **Service**: Type `HTTP`, URL `localhost:5555`
   - Save. A Cloudflare cria o registro DNS sozinha.

Abra `https://harvey.seudominio.com.br`: o painel já deve aparecer. **Ainda sem login**:
a próxima seção resolve isso. Não pare aqui.

---

## 4. Restringir o acesso (Cloudflare Access)

Objetivo: só `voce@exemplo.com.br` e `socio@exemplo.com.br` entram, com
login Google ou código por e-mail.

1. Em Zero Trust, **Settings → Authentication → Login methods**. O **One-time PIN**
   (código por e-mail) já vem ligado. Pra ter o botão "Entrar com Google", clique em
   **Add new → Google** e siga o passo a passo deles (cria um OAuth client no Google
   Cloud e cola Client ID/Secret). Opcional: o PIN por e-mail já cumpre o requisito.
2. **Access → Applications → Add an application → Self-hosted**.
   - **Application name**: `Harvey`
   - **Session duration**: 1 semana (ou o que preferir)
   - **Public hostname**: subdomain `harvey`, domain o seu.
   - **Identity providers**: marque Google e One-time PIN.
   - Next.
3. **Add a policy**:
   - **Policy name**: `Pessoas autorizadas` · **Action**: Allow
   - **Include → Selector: Emails** → cole os dois e-mails, um por linha:
     `voce@exemplo.com.br` e `socio@exemplo.com.br`
   - Next → Add application.
4. Abra o aplicativo criado (**Access → Applications → Harvey → Overview**) e copie o
   **Application Audience (AUD) Tag** (uma string longa).

Teste numa janela anônima: `https://harvey.seudominio.com.br` deve mostrar a tela de
login da Cloudflare. Entre com um dos e-mails.

### 4b. A segunda tranca, dentro do painel

Pra que o painel confira por conta própria quem entrou (mesmo que alguém mexa na
política do Cloudflare), vá em **Chaves → Quem pode entrar neste painel** e preencha:

- **Domínio do time**: o nome escolhido no passo 3.1 (ex.: `seutime`)
- **Audience (AUD)**: o tag copiado no passo 4.4
- **E-mails autorizados**: `voce@exemplo.com.br, socio@exemplo.com.br`

Salve. A partir daí toda requisição sem um login válido da Cloudflare recebe `403`,
e o e-mail de quem entrou aparece no topo do painel, com um link "sair".

---

## 5. Conectar a sua assinatura do Claude

A VM não tem navegador, então o login é por um token de um ano gerado **no seu
computador**:

1. No seu computador, no terminal: `claude setup-token`. Autorize no navegador.
2. Copie a linha que começa com `sk-ant-oat…`.
3. No painel, aba **IA**: cole, **Salvar token**, depois **Testar a IA**. Deve responder
   "A IA respondeu em X s".

A aba IA também mostra a cota da assinatura (janela de 5 h e semanal). O limite de
quanto o Harvey pode gastar fica em **Configurações → Uso da IA e horários**
(padrão 80%).

---

## 6. Caixa de e-mail

Aba **E-mail**. O caminho recomendado é Gmail/Workspace numa caixa em um domínio
secundário:

1. Expanda "Como criar o Client ID e o Client Secret" e siga os 5 passos. No passo da
   URI de redirecionamento, use o botão **copiar** (vai ser
   `https://harvey.seudominio.com.br/api/gmail/callback`).
2. Cole Client ID e Secret, **Salvar credenciais**, depois **Conectar com Google**.
3. Volta pro painel com "Caixa conectada: gmail". Clique em **Testar conexão**.

Depois, em **Chaves**, cadastre pelo menos uma chave de verificação de e-mail (Reoon é
grátis). Sem isso os endereços nunca são enviados.

---

## 7. Ensinar o produto e ligar

1. **Treinar**: cole o site da sua empresa e aguarde. Revise em **Configurações**.
2. **Sinais**: clique em "Confirmar todos os grátis".
3. **Descobrir**: escolha OpenStreetMap, estime, rode.
4. **Controles → Iniciar Harvey**. Ele passa a acordar a cada 15 min. Tudo que ele
   escrever para em **Caixa de saída** esperando sua aprovação.

---

## Manutenção

| Quero… | Faço… |
|---|---|
| Atualizar o Harvey | na VM: `cd ~/harvey && git pull && .venv/bin/pip install -e . && sudo systemctl restart harvey-dashboard harvey-agent` |
| Ver o log do painel | `sudo journalctl -u harvey-dashboard -f` |
| Ver o log do agente | `tail -f ~/harvey/data/harvey.log` (ou aba Controles) |
| Trocar o token do Claude | aba IA, cole o novo |
| Fazer backup | copie `~/harvey/data/`, `~/harvey/.env` e `~/harvey/harvey.local.yaml` |
| Desligar tudo | `sudo systemctl stop harvey-agent harvey-dashboard cloudflared` |

### Se der errado

- **Painel não abre pelo domínio**: `sudo systemctl status cloudflared`; na Cloudflare, o
  túnel precisa estar *Healthy* e o hostname apontando pra `localhost:5555`.
- **403 "não está na lista"**: o e-mail que você usou no login não está em
  **Chaves → Quem pode entrar**. Entre com outro e-mail ou edite a lista (pelo próprio
  painel, com um e-mail autorizado, ou editando `~/harvey/.env` na VM).
- **"Conectar com Google" dá erro de redirect_uri**: a URI no Google Cloud precisa ser
  exatamente a que o painel mostra, com `https://`.
- **A IA não responde**: `~/.local/bin/claude --version` na VM mostra a CLI? Token
  colado inteiro? A assinatura está ativa? O log do agente diz o motivo.
