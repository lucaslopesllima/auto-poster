# linkedinAuto

Automação de posts no perfil pessoal do LinkedIn via API oficial.

## Estrutura do projeto

```
linkedinAuto/
├── README.md             # este arquivo
├── requirements.txt      # dependências Python
├── .env.example          # template de credenciais (copie para .env)
├── .gitignore            # ignora .env, banco SQLite, etc.
│
├── auth.py               # fluxo OAuth: gera access_token e URN
├── linkedin_client.py    # wrapper da API LinkedIn (postar texto/imagem)
├── db.py                 # fila de posts em SQLite
├── cli.py                # CLI para adicionar/listar/publicar posts
├── scheduler.py          # loop que publica posts agendados
├── gui.py                # app desktop em PySide6 (Qt)
├── news.py               # busca de notícias (NewsAPI + Google News RSS)
├── composer.py           # gera o texto do post via OpenAI gpt-4o-mini
│
├── posts/                # arquivos de exemplo com conteúdo dos posts
│   └── exemplo.md
│
└── data/                 # criado em runtime
    └── queue.db          # banco SQLite com a fila
```

## Pré-requisitos

1. App criado em https://developer.linkedin.com/ com os produtos aprovados:
   - **Sign In with LinkedIn using OpenID Connect**
   - **Share on LinkedIn**
2. Em **Auth → Authorized redirect URLs**, adicione: `http://localhost:8000/callback`
3. Python 3.10+ instalado.

## Instalação

```bash
cd ~/Desktop/linkedinAuto
python -m venv .venv
python -m venv .venvpython -m venv .venvpython -m venv .venv
pip install -r requirements.txt
cp .env.example .env
```

Edite `.env` e preencha `LINKEDIN_CLIENT_ID` e `LINKEDIN_CLIENT_SECRET`.

## Uso passo a passo

### 1. Autenticar (gera token + URN)

```bash
python auth.py
```

- Abre o browser, você faz login e autoriza.
- O script captura o `code`, troca por `access_token` e busca seu `URN`.
- Salva ambos em `.env` automaticamente.
- O token vale **60 dias** — rode este comando de novo quando expirar.

### 2. Adicionar um post à fila

```bash
# post imediato
python cli.py add "Meu primeiro post automático 🚀"

# post agendado (ISO 8601 ou 'YYYY-MM-DD HH:MM')
python cli.py add "Post da manhã" --at "2026-05-17 09:00"

# post com imagem
python cli.py add "Veja esse gráfico" --image ./grafico.png

# importar de um arquivo markdown
python cli.py add --file posts/exemplo.md --at "2026-05-18 10:00"

# recorrência: todo dia às 09:00
python cli.py add "Bom dia LinkedIn" --daily 09:00

# recorrência: a cada 60 minutos
python cli.py add "Heartbeat" --every 60
```

### Recorrência

Cada post pode ter um dos dois marcadores (exclusivos):

- `repeat_daily_at` (`--daily HH:MM`) — após publicar, clona o registro com `scheduled_at` na próxima ocorrência do mesmo horário.
- `repeat_minutes` (`--every N`) — após publicar, clona com `scheduled_at = agora + N min`.

Clonagem acontece em `db.mark_posted`, então CLI, scheduler e GUI usam o mesmo caminho. O registro original fica como `posted` (histórico preservado); o próximo disparo é uma nova linha.

### 3. Listar a fila

```bash
python cli.py list
python cli.py list --pending     # só os ainda não publicados
```

### 4. Publicar agora

```bash
python cli.py run-now             # publica todos os pendentes vencidos
python cli.py run-now --id 3      # publica um post específico
```

### 5. Rodar o agendador em loop

```bash
python scheduler.py               # verifica a fila a cada 60s
python scheduler.py --interval 300
```

Para rodar de forma persistente, use cron ou systemd (veja seção abaixo).

## Interface gráfica (desktop)

App desktop em **PySide6 (Qt)** expõe todas as features (auth, add, list, delete, publish, scheduler) numa janela única. Sem Node, sem browser embutido — Python puro.

### Rodar

```bash
pip install -r requirements.txt   # já inclui PySide6
python gui.py
```

### Abas

- **Novo post** — texto livre, importar de `posts/*.md`, anexar imagem, agendar ou publicar agora.
- **Pesquisar** — busca notícias (NewsAPI + fallback Google News RSS), gera draft via OpenAI gpt-4o-mini, e também hospeda os **jobs de pesquisa recorrente** (gera draft em horário fixo).
- **Fila** — listar, filtrar pendentes, publicar, editar, aprovar rascunhos, apagar, abrir post publicado.
- **Configurações** — edita `.env` (campos do LinkedIn, OpenAI, NewsAPI). Inclui botão **Autenticar agora** que dispara o fluxo OAuth de `auth.py`.

O **scheduler** roda em background automaticamente enquanto a janela está aberta (intervalo 60s). Estado mostrado na barra de status no rodapé.

### Fluxo de pesquisa → draft → publicação

1. Aba **Pesquisar**: digite o tópico → "Buscar" (preview-only) → "Gerar post".
2. App busca top N notícias e gera o texto via OpenAI; mostra notícias + draft editável.
3. Edite o texto se necessário → "Salvar como rascunho".
4. Aba **Fila**: rascunho aparece com status `rascunho`. Clique **Aprovar**.
5. Depois de aprovado, scheduler (ou "Publicar vencidos") publica.

### Jobs automáticos (geração agendada)

Aba **Pesquisar** → seção "Jobs de pesquisa recorrente" → **Novo job**. Cada job tem:

- `Tópico` — argumento do `news.fetch_news`
- `Gerar às` — horário diário (HH:MM, local) em que o job dispara fetch + compose
- `Publicar às` — horário diário do `scheduled_at` no post gerado (ou "imediato após gerar")
- `Máx notícias` — quantos artigos alimentam o LLM
- `Auto-aprovar` — se marcado, post sai sem `is_draft`; o scheduler publica direto. Se desmarcado, fica como rascunho aguardando aprovação na Fila.
- `Habilitado` — flag liga/desliga sem apagar

O scheduler está sempre rodando enquanto a janela está aberta. Cada job roda no máximo uma vez por dia (controlado por `last_run_at`).

Variáveis de ambiente necessárias:

```ini
OPENAI_API_KEY=sk-...           # obrigatório para gerar o draft
OPENAI_MODEL=gpt-4o-mini        # opcional
COMPOSER_LANGUAGE=pt-BR         # opcional
NEWSAPI_KEY=                    # opcional; se vazio, usa Google News RSS
```

A janela compartilha o mesmo `data/queue.db` e `.env` da CLI — pode misturar uso.

## Agendamento via cron

Edite o crontab com `crontab -e` e adicione:

```cron
# verifica a fila a cada 5 minutos
*/5 * * * * cd /home/lucaslopes/Desktop/linkedinAuto && /home/lucaslopes/Desktop/linkedinAuto/.venv/bin/python cli.py run-now >> /tmp/linkedinAuto.log 2>&1
```

## Renovação do token

O LinkedIn não fornece refresh token padrão para perfis pessoais. A cada ~55 dias:

```bash
python auth.py
```

E o `.env` é atualizado automaticamente.

## Notas de segurança

- **Nunca** commite o arquivo `.env` (já está no `.gitignore`).
- O `access_token` dá permissão para postar no seu perfil — trate como senha.
- Se vazar, revogue o app em https://www.linkedin.com/psettings/permitted-services e gere outro.
# auto-poster
