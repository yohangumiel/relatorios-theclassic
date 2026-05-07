# Discord Suggestions Dashboard

Aplicacao web simples para ler o historico de um canal de sugestoes, contar reacoes positivas e negativas e exibir tudo em uma pagina HTML e em JSON.

## Estrutura

Esta app fica isolada nesta pasta:

- `discord_suggestions_dashboard/`

Isso evita misturar a aplicacao web com notebooks, planilhas e scripts antigos do workspace.

## O que ela faz

- Busca mensagens do canal `766493626016071691` por padrao.
- Conta `👍` e `👎` usando o campo `reactions` retornado pela API do Discord.
- Mostra score (`likes - dislikes`), autor, data, anexos e link da mensagem.
- Expoe uma API em `/api/suggestions`.

## Variaveis de ambiente

- `DISCORD_BOT_TOKEN`: token usado no header `Authorization`.
- `DISCORD_SUGGESTIONS_CHANNEL_ID`: id do canal de sugestoes.
- `POSITIVE_EMOJIS`: emojis que contam como aprovacao, separados por virgula.
- `NEGATIVE_EMOJIS`: emojis que contam como reprovacao, separados por virgula.
- `CACHE_TTL_SECONDS`: cache em memoria para evitar chamadas a cada refresh.
- `DISCORD_FETCH_PAGE_SIZE`: tamanho de pagina da API do Discord.
- `INCLUDE_BOT_MESSAGES`: inclui mensagens de bots se `true`.
- `PORT`: porta da aplicacao.
- `DEEPSEEK_API_KEY`: chave da API DeepSeek para gerar resumo.
- `DEEPSEEK_MODEL`: modelo usado no resumo. Padrao: `deepseek-chat`.
- `DEEPSEEK_MAX_SUGGESTIONS`: maximo de sugestoes enviadas ao resumo para controlar custo/contexto.

## Rodando localmente

```bash
pip install -r requirements.txt
```

No PowerShell:

```powershell
$env:DISCORD_BOT_TOKEN="SEU_TOKEN"
$env:DISCORD_SUGGESTIONS_CHANNEL_ID="766493626016071691"
python app.py
```

Depois abra:

- `http://localhost:5000`
- `http://localhost:5000/api/suggestions`

## Docker

Build:

```bash
docker build -t discord-suggestions-dashboard .
```

Run:

```bash
docker run --rm -p 5000:5000 -e DISCORD_BOT_TOKEN=SEU_TOKEN -e DISCORD_SUGGESTIONS_CHANNEL_ID=766493626016071691 discord-suggestions-dashboard
```

Depois abra:

- `http://localhost:5000`

## Permissoes do bot

O bot precisa pelo menos:

- `View Channel`
- `Read Message History`

Se o conteudo vier vazio, confira tambem se o bot esta com acesso a conteudo de mensagens no Discord Developer Portal.

## Deploy facil

A forma mais simples para compartilhar por link e subir em `Render` ou `Railway`.

### Render

1. Suba esta pasta em um repositorio Git.
2. Crie um novo `Web Service`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn --bind 0.0.0.0:$PORT app:app`
5. Configure `DISCORD_BOT_TOKEN` e, se quiser, `DISCORD_SUGGESTIONS_CHANNEL_ID`.

### Railway

1. Crie um novo projeto a partir do repositorio.
2. Configure as mesmas variaveis de ambiente.
3. Comando de start: `gunicorn --bind 0.0.0.0:$PORT app:app`

## Observacoes

- O token fica apenas no backend. Nao coloque token em HTML ou JavaScript do navegador.
- O cache atual e em memoria. Se voce quiser historico incremental e persistente, o proximo passo natural e salvar em SQLite ou Postgres.
