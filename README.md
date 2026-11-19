# claude-telegram

A personal Telegram bot that drives [Claude Code](https://docs.claude.com/en/docs/claude-code) sessions
**headlessly from your phone**. It runs on your own Linux machine (long polling, no inbound port needed),
launches `claude -p ... --resume` for the session you pick, streams progress back to the chat, and lets the
session itself talk to you (notify, ask, send files) through a small MCP server.

> Not affiliated with, endorsed by, or sponsored by Anthropic or Telegram. "Claude" and "Claude Code" are
> trademarks of Anthropic; "Telegram" is a trademark of Telegram FZ-LLC.

> **WARNING: sessions run with `--dangerously-skip-permissions`.** Whoever controls the bot controls the
> machine it runs on (Bash, file edits, network, everything the Unix user can do, with no permission
> prompts). Use it **only for a single trusted owner, on your own machine**. Never add it to a group, never
> share the token, and turn on the TOTP lock (below) if the phone could ever be in someone else's hands.

## Features

- **Multi-account usage**: 5-hour and 7-day usage per Claude account (percent, reset time, severity), the
  active account, and buttons to switch. Switching keeps the same session.
- **Projects and sessions**: pick a project (your home, optional workspace list, optional `projects.txt`),
  list the sessions that are open right now in Claude Code plus the ones started by the bot, resume
  recent ones, start a new one, stop a run, read the last messages of a session.
- **Voice notes**: send a voice note or audio; it is transcribed with OpenAI (`gpt-4o-mini-transcribe`,
  fallback `whisper-1`, Portuguese) and treated as a text prompt.
- **Files both ways**: photos/documents/videos/audio you send are saved to a private inbox and attached to
  the prompt; a session can send a file back to you as a document (with a deny-list for secrets).
- **MCP tools for the session**: `telegram_notificar` (notify), `telegram_perguntar` (ask with inline
  buttons or free-text/voice reply; blocks until answered) and `telegram_enviar_arquivo` (send a file).
  Headless runs cannot use `AskUserQuestion`, so the bot appends a system prompt that routes decisions
  through `telegram_perguntar`.
- **Telegram Mini App panel**: accounts and usage, projects, sessions, history, live view of the running
  session, open sessions ("Now" tab), start/stop/switch. **View and control only: the panel never sends
  prompts.**
- **TOTP lock**: optional RFC 6238 authenticator lock; the bot starts locked and unlocks for a configurable
  number of hours.
- **systemd watchdog**: `Type=notify` user service with `WatchdogSec`, restart on failure.
- **Pinned shortcuts**: pin up to four sessions as keyboard buttons and route a message to one of them
  with `2: your text`.

## Architecture

```
 phone (Telegram app)
    |  Bot API (HTTPS, long polling: outbound only)
    v
 +--------------------------- your Linux machine -----------------------------+
 |  systemd --user: claude-telegram.service (python -m bot.main)              |
 |                                                                            |
 |   bot/handlers  --spawns-->  claude -p --resume ...  (child process)       |
 |      |   ^                        |  stream-json events                    |
 |      |   |  progress edits        +--> MCP stdio: mcp_server/telegram_mcp  |
 |      |   |                                      |  X-Bot-Secret            |
 |      |   +--------- 127.0.0.1:BOT_LOCAL_PORT <-+  (/notify /ask /send_file)|
 |      |                                                                     |
 |      +--> 127.0.0.1:WEBAPP_PORT (Mini App: GET / and /api/*, initData HMAC)|
 +------------------------------------|-----------------------------------------+
                                       | optional: cloudflared tunnel
                                       v
                          https://bot.example.com  <-- Telegram Mini App button
```

State and secrets live in `~/.config/claude-telegram/` (`env`, `state.json`, `local.secret`, `local.port`,
optional `projects.txt`); received files go to `~/.cache/claude-telegram/inbox/`.

## Security model

- **Owner binding.** `/start` asks for a shared Telegram contact. Only a contact that is the sender's own
  and whose phone number equals `ALLOWED_PHONE` is accepted; the Telegram `user_id` is then stored as the
  owner. Everything else is ignored silently: strangers, groups, other users' contacts.
  Only private chats with the bound owner are served.
- **Mini App.** Every `/api/*` request must carry `X-Tg-Init-Data` (`Telegram.WebApp.initData`), validated
  with HMAC-SHA256 as in the Telegram docs (key derived from the bot token, constant-time compare). The
  user id must be the owner and `auth_date` at most 24 h old. Failures return a bare 401. Only `GET /`
  (static HTML, no data) is public. No CORS, strict CSP, body limit 64 KB, 120 requests/min per IP.
- **TOTP lock.** With `TOTP_SECRET` set the bot starts locked; every message, button, voice note and file
  is refused until `/destravar <code>`. Five wrong codes in a row lock it for 15 minutes.
  The Mini App answers 423 while locked.
- **Child environment.** The `claude` child does not inherit `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`,
  `TOTP_SECRET` or the local API secret. This limits accidental leaks; it is **not** isolation: the env
  file is readable by the same Unix user the session runs as.
- **Local API.** The MCP server talks to the bot over `127.0.0.1` only, authenticated with a random secret
  (`X-Bot-Secret`, file `local.secret`, mode 0600).
- **Files.** Sendable paths must be absolute, inside your home, regular files up to 50 MB, and outside a
  deny-list (`~/.ssh`, `~/.gnupg`, `~/.cloudflared`, `~/.aws`, the bot's own state dir, Claude
  credentials and transcripts, `.env*`...).
- The bot never refreshes OAuth tokens, only reads them for the usage query, and never logs tokens.

## Requirements

- Linux with `systemd --user` (and, to survive logout, `loginctl enable-linger`)
- Python 3.12+
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed at `~/.local/bin/claude` and
  logged in (one config dir per account)
- A Telegram bot token and an OpenAI API key (voice only)
- Optional: `cloudflared` + a Cloudflare-managed domain (Mini App panel), `qrencode` (TOTP QR in the terminal)

## Setup

1. **Clone** into `~/claude-telegram` (the systemd unit expects that path; edit
   `deploy/claude-telegram.service` if you use another).
2. **Create the bot** with [@BotFather](https://t.me/BotFather): `/newbot`, copy the token. Then
   `/setjoingroups` -> **Disable**, so nobody can add the bot to a group.
3. **Write the env file** `~/.config/claude-telegram/env`, mode `0600`:

   ```
   TELEGRAM_BOT_TOKEN=123456789:replace-with-your-token
   OPENAI_API_KEY=replace-with-your-key
   ALLOWED_PHONE=5511999999999
   ```

   `ALLOWED_PHONE` is your own number, digits only, with country code. Optional keys: `WEBAPP_URL` (public
   https URL of the panel), `WEBAPP_PORT` (default `28766`), `BOT_LOCAL_PORT` (default `28765`),
   `TOTP_SECRET` (written by `setup-totp.sh`), `TOTP_UNLOCK_HOURS` (default `12`).
4. **Install the service**: `scripts/install.sh` (creates `.venv`, installs dependencies, generates
   `deploy/mcp.json`, enables `claude-telegram.service`; idempotent, no sudo). Check with
   `systemctl --user status claude-telegram` and `journalctl --user -u claude-telegram -f`.
   Restarting the service also kills the `claude` runs it started.
5. **Link your phone**: in Telegram send `/start` to your bot and share your contact.
6. **TOTP lock (recommended)**: run `scripts/setup-totp.sh` in a terminal **outside Claude Code** (it
   prints the secret/QR and refuses to run inside Claude Code), scan it with an authenticator app, then
   `systemctl --user restart claude-telegram`. Use `/destravar <code>` in the chat.
7. **MCP for sessions you open yourself** (optional): sessions launched by the bot already get
   `--mcp-config deploy/mcp.json`. To also register the MCP in your Claude accounts run
   `scripts/register-mcp.sh` (it edits each account's Claude config through `claude mcp add`: read the
   script first).
8. **Mini App panel** (optional): create your own Cloudflare tunnel and a hostname, e.g.
   `cloudflared tunnel create tg-bot`, route `bot.example.com` to it in DNS, and write
   `~/.cloudflared/tg-bot.yml` pointing the hostname at `http://127.0.0.1:28766`. Then run
   `scripts/install-tunnel.sh` (enables the `cloudflared-tg-bot` user service and adds `WEBAPP_URL` to the
   env file) and restart the bot so it registers the "Painel" menu button. Edit the hostname in
   `scripts/install-tunnel.sh` first. Never run `cloudflared tunnel run` without `--config` if you have
   other tunnels on the same machine.

Run by hand without systemd: `.venv/bin/python -m bot.main`. Tests: `.venv/bin/pytest -q`.

### Accounts and projects

- **Accounts** are Claude Code config dirs (`CLAUDE_CONFIG_DIR`). The list comes from `claude_profiles`
  (`name`, `config_dir`) in `~/.horizon/config.yaml`, an optional file from the author's workspace tool; if
  it is missing the defaults in `bot/usage.py` apply (`claude` = `~/.claude`, plus `work`, `personal`,
  `client` = `~/.claude-work`, `~/.claude-personal`, `~/.claude-client`). Edit `_DEFAULT_PROFILES` there to
  match your setup.
- **Projects** offered by `/projetos`: your home, then `workspaces[].cwd` from the same optional
  `~/.horizon/config.yaml`, then one absolute path per line from `~/.config/claude-telegram/projects.txt`.
- **Usage numbers** come from Anthropic's OAuth usage endpoint using each account's existing credentials
  (read-only; the bot never refreshes tokens), with an optional cache file from the same workspace tool.

## Commands

| Command | What it does |
|---|---|
| `/start` | Link: share your contact; only the owner's own contact with the allowed phone is accepted |
| `/contas` | 5h/7d usage per account, state, buttons to pick the active account |
| `/conta <name>` | Pick the account; with an active session, the same session continues on the new account |
| `/projetos` | Buttons with your projects; choosing one sets the active project |
| `/sessoes` | Sessions open now plus the bot's own; buttons to switch, "Retomar" (resume recent) and "Nova" |
| `/nova` | New session in the active project and account |
| `/parar` | Stop the active session's run (SIGTERM, then SIGKILL after 5 s) |
| `/status` | Project, account, session, running runs, uptime, linger, sleep mode, TOTP state |
| `/historico [n]` | Last messages of the active session (default 6, max 30) |
| `/menu` | Show the persistent keyboard |
| `/silencio [off\|final\|total]` | Quiet mode, saved in `state.json`. `final`: no progress, no echoes or intermediate notices, only final results and anything that needs you (asks, terminal dialogs, limit prompts, errors, lock); `total`: only what needs you, the rest is queued (last 50). Non-urgent messages are sent without notification |
| `/resumo` | Send the queued digest (one card per item, oldest first) and clear it |
| `/destravar <code>` | Unlock with the 6-digit TOTP code (only if `TOTP_SECRET` is set) |
| `/trancar` | Lock now (also the "Trancar" keyboard button); does not interrupt running runs |
| `/fixar [n]` | Pin the active session as keyboard shortcut 1-4 |
| `/desafixar <n>` | Remove a pinned shortcut |
| `/encerrar` | End the active session |

The persistent keyboard has Contas, Sessoes, Nova, Projetos, Historico, Parar, Status and Trancar buttons.
Free text, voice notes and files become the prompt of the active session (`claude -p --resume`). One run
per session: a message sent while the session is busy gets a notice, no queue. Progress is one message
edited every ~5 s; the final answer comes in parts of up to 4096 characters. Run timeout: 30 min.
Prefix a message with `2: text` to send it to pinned shortcut 2 without switching the active session.

## Limitations

- Sharing sessions between accounts relies on `projects/` being symlinked between the Claude config dirs
  (`~/.claude-work/projects` -> `~/.claude/projects`, ...). That is the author's setup; without it, switching
  accounts copies the session transcript to the target account instead.
- Headless runs cannot use `AskUserQuestion`; decisions are bridged through the `telegram_perguntar` MCP
  tool via the appended system prompt.
- Voice notes need an OpenAI API key.
- One bot per machine; another PC needs another bot and token.
- The machine must be awake: on battery it may suspend; with an encrypted disk a full power-off needs the
  passphrase at boot. `/status` shows linger and sleep mode (the bot changes none of it).
- Message texts of the bot are in Portuguese.

## Roadmap

- Accounts without any external config file (a plain accounts list in the state dir).
- Localized (English) bot messages.

## License

MIT, see `LICENSE`. Copyright (c) claude-telegram contributors. Provided as is, without warranty. Not
affiliated with Anthropic or Telegram.

---

## Português

Bot pessoal de Telegram que conduz sessões do [Claude Code](https://docs.claude.com/en/docs/claude-code)
**sem interface, pelo celular**. Roda na sua própria máquina Linux (long polling, sem porta de entrada),
dispara `claude -p ... --resume` na sessão escolhida, devolve o progresso no chat e deixa a própria sessão
falar com você (avisar, perguntar, enviar arquivos) por um servidor MCP pequeno.

> Sem vínculo com a Anthropic nem com o Telegram. "Claude" e "Claude Code" são marcas da Anthropic;
> "Telegram" é marca da Telegram FZ-LLC.

> **AVISO: as sessões rodam com `--dangerously-skip-permissions`.** Quem controla o bot controla a máquina
> (Bash, edição de arquivos, rede, tudo o que o usuário Unix pode, sem pedidos de permissão). Use **só para
> um único dono de confiança, na sua própria máquina**. Nunca coloque em grupo, nunca compartilhe o token e
> ligue a trava TOTP se o celular puder parar na mão de outra pessoa.

### Recursos

- **Várias contas**: uso de 5 h e 7 dias por conta Claude, conta ativa e botões para trocar; a troca
  mantém a sessão.
- **Projetos e sessões**: escolha o projeto, veja as sessões abertas agora no Claude Code e as do bot,
  retome as recentes, crie nova, pare uma execução, leia as últimas mensagens.
- **Notas de voz**: transcritas pela OpenAI (`gpt-4o-mini-transcribe`, fallback `whisper-1`, português) e
  tratadas como texto.
- **Arquivos nos dois sentidos**: o que você manda vai para uma caixa de entrada privada e é anexado ao
  prompt; a sessão pode devolver um arquivo como documento (com lista de bloqueio de segredos).
- **Ferramentas MCP para a sessão**: `telegram_notificar`, `telegram_perguntar` (botões ou resposta em
  texto/voz; bloqueia até responder) e `telegram_enviar_arquivo`. Execuções sem interface não têm
  `AskUserQuestion`; um system prompt anexado manda as decisões para `telegram_perguntar`.
- **Painel (Mini App do Telegram)**: contas e uso, projetos, sessões, histórico, acompanhamento ao vivo,
  aba "Agora", iniciar/parar/trocar. **Só visualiza e controla: o painel nunca envia prompts.**
- **Trava TOTP** (RFC 6238) opcional e **watchdog do systemd**.
- **Atalhos fixados**: até quatro sessões como botões no teclado; `2: texto` envia para o atalho 2.

### Arquitetura

O diagrama ASCII está na seção em inglês acima: o bot (serviço `systemd --user`) faz long polling na Bot
API (só tráfego de saída), lança o `claude` filho, que usa o MCP stdio; o MCP fala com o bot por
`127.0.0.1:BOT_LOCAL_PORT` com um segredo; o painel é servido em `127.0.0.1:WEBAPP_PORT` e exposto, se você
quiser, por um túnel `cloudflared` próprio.

### Modelo de segurança

- **Vínculo do dono**: `/start` pede o contato compartilhado; só vale o contato do próprio remetente com o
  telefone igual a `ALLOWED_PHONE`; o `user_id` vira o dono. Todo o resto (estranhos, grupos, contatos de
  outras pessoas) é ignorado em silêncio. Só chat privado com o dono.
- **Painel**: toda rota `/api/*` exige `X-Tg-Init-Data` validado por HMAC-SHA256 (chave derivada do token),
  `user.id` do dono e `auth_date` de no máximo 24 h; falha = 401 sem detalhe. Só `GET /` é público.
  Sem CORS, CSP restritiva, corpo até 64 KB, 120 req/min por IP.
- **Trava TOTP**: com `TOTP_SECRET` o bot inicia trancado; 5 códigos errados seguidos bloqueiam por 15 min.
- **Ambiente do filho**: o `claude` filho não herda token do bot, chave da OpenAI, `TOTP_SECRET` nem o
  segredo da API local. Reduz vazamento acidental; **não é isolamento** (mesmo usuário Unix).
- **API local**: só em `127.0.0.1`, com segredo aleatório (`X-Bot-Secret`, arquivo 0600).
- **Arquivos**: caminho absoluto dentro da home, arquivo comum até 50 MB, fora da lista de bloqueio
  (`~/.ssh`, `~/.gnupg`, `~/.cloudflared`, credenciais do Claude, `.env*`...).

### Requisitos

Linux com `systemd --user` (e `loginctl enable-linger` para sobreviver ao logout); Python 3.12+; Claude
Code CLI em `~/.local/bin/claude`, logado; token de bot do Telegram; chave da OpenAI (só voz). Opcionais:
`cloudflared` e um domínio na Cloudflare (painel), `qrencode` (QR do TOTP no terminal).

### Instalação

1. Clone em `~/claude-telegram` (a unit do systemd assume esse caminho).
2. No [@BotFather](https://t.me/BotFather): `/newbot`, copie o token e rode `/setjoingroups` ->
   **Disable** (ninguém adiciona o bot em grupos).
3. Crie `~/.config/claude-telegram/env` (modo `0600`) com `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` e
   `ALLOWED_PHONE` (seu número, só dígitos, com DDI). Opcionais: `WEBAPP_URL`, `WEBAPP_PORT` (28766),
   `BOT_LOCAL_PORT` (28765), `TOTP_SECRET`, `TOTP_UNLOCK_HOURS` (12).
4. `scripts/install.sh` (cria o `.venv`, instala dependências, gera `deploy/mcp.json`, liga o serviço).
5. No Telegram, `/start` e compartilhe seu contato.
6. TOTP (recomendado): `scripts/setup-totp.sh` num terminal **fora do Claude Code**, escaneie no
   autenticador e reinicie o serviço; use `/destravar <código>`.
7. MCP nas suas próprias sessões (opcional): `scripts/register-mcp.sh` (leia antes: ele edita a config de
   cada conta via `claude mcp add`).
8. Painel (opcional): crie seu próprio túnel Cloudflare e hostname, escreva `~/.cloudflared/tg-bot.yml`
   apontando para `http://127.0.0.1:28766`, ajuste o hostname em `scripts/install-tunnel.sh` e rode-o;
   reinicie o bot.

### Comandos

`/start`, `/contas`, `/conta <nome>`, `/projetos`, `/sessoes`, `/nova`, `/parar`, `/status`,
`/historico [n]`, `/menu`, `/destravar <código>`, `/trancar`, `/fixar [n]`, `/desafixar <n>`, `/encerrar`.
Há também um teclado fixo e, qualquer texto, voz ou arquivo vira prompt da sessão ativa.
`2: texto` envia a mensagem ao atalho fixado 2.

### Limitações

- Compartilhar sessões entre contas depende de `projects/` ser symlink entre as pastas de config do Claude
  (é o setup do autor); sem isso, a troca de conta copia o transcrito para a conta de destino.
- Execuções sem interface não usam `AskUserQuestion` (ponte via `telegram_perguntar`).
- Voz exige chave da OpenAI. Um bot por máquina. As mensagens do bot estão em português.
- A máquina precisa estar ligada e acordada; o bot não altera energia nem linger.

### Roadmap

- Contas sem arquivo de configuração externo.
- Mensagens do bot em inglês.

### Licença

MIT (veja `LICENSE`). Sem garantia. Sem vínculo com a Anthropic nem com o Telegram.
