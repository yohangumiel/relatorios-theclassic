import json
import os
import time
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory


DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
DEFAULT_CHANNEL_ID = "766493626016071691"

PERIODS = {
    "7d":  ("Ultima semana (Sugestoes)",   timedelta(days=7)),
    "30d": ("Ultimos 30 dias (Sugestoes)", timedelta(days=30)),
}

TOP_N_BY_PERIOD = {
    "7d":  15,
    "30d": 15,
}

load_dotenv()
app = Flask(__name__)


class DashboardError(Exception):
    pass


@dataclass
class CacheEntry:
    created_at: float
    payload: dict[str, Any]


_CACHE: dict[str, CacheEntry] = {}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def parse_emoji_list(name: str, default: str) -> set[str]:
    raw_value = os.getenv(name, default)
    if "ð" in raw_value:
        raw_value = default
    return {item.strip() for item in raw_value.split(",") if item.strip()}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def snowflake_from_datetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    timestamp_ms = int(dt.timestamp() * 1000)
    return (timestamp_ms - DISCORD_EPOCH_MS) << 22


def period_cutoff(period_key: str) -> tuple[str, datetime | None, int | None]:
    label, delta = PERIODS.get(period_key, PERIODS["30d"])
    if delta is None:
        return label, None, None

    cutoff = datetime.now(timezone.utc) - delta
    return label, cutoff, snowflake_from_datetime(cutoff)


def build_headers() -> dict[str, str]:
    token = (
        os.getenv("DISCORD_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )
    if not token:
        raise DashboardError("Defina DISCORD_TOKEN ou DISCORD_BOT_TOKEN nas variaveis de ambiente.")

    return {
        "Authorization": token,
        "User-Agent": "suggestions-dashboard/1.0",
    }


def emoji_key(emoji: dict[str, Any]) -> str:
    name = emoji.get("name") or ""
    emoji_id = emoji.get("id")
    if emoji_id:
        return f"{name}:{emoji_id}"
    return name


def reaction_count(message: dict[str, Any], accepted_emojis: set[str]) -> int:
    total = 0
    for reaction in message.get("reactions", []) or []:
        key = emoji_key(reaction.get("emoji", {}))
        if key in accepted_emojis:
            total += int(reaction.get("count", 0))
    return total


def message_link(message: dict[str, Any], channel_id: str) -> str | None:
    guild_id = message.get("guild_id")
    if not guild_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message['id']}"


def fetch_channel_messages(
    channel_id: str,
    page_limit: int,
    after_snowflake: int | None,
    max_messages: int,
) -> list[dict[str, Any]]:
    headers = build_headers()
    messages: list[dict[str, Any]] = []
    before: str | None = None

    while True:
        params: dict[str, Any] = {"limit": min(max(page_limit, 1), 100)}
        if before:
            params["before"] = before

        try:
            response = requests.get(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
                headers=headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            detail = exc.response.text if exc.response is not None else str(exc)
            raise DashboardError(
                f"Discord API retornou HTTP {status}. Verifique token, acesso ao canal "
                f"e permissao de ler historico. Detalhe: {detail}"
            ) from exc
        except requests.RequestException as exc:
            raise DashboardError(f"Falha na requisicao ao Discord: {exc}") from exc

        batch = response.json()
        if not batch:
            break

        for message in batch:
            message_id = int(message["id"])
            if after_snowflake is not None and message_id <= after_snowflake:
                continue
            messages.append(message)
            if len(messages) >= max_messages:
                return messages

        oldest_id = int(batch[-1]["id"])
        if after_snowflake is not None and oldest_id <= after_snowflake:
            break
        if len(batch) < min(max(page_limit, 1), 100):
            break

        before = batch[-1]["id"]

    return messages


def normalize_message(
    message: dict[str, Any],
    channel_id: str,
    positive_emojis: set[str],
    negative_emojis: set[str],
) -> dict[str, Any]:
    created_at = datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))
    likes = reaction_count(message, positive_emojis)
    dislikes = reaction_count(message, negative_emojis)
    score = likes - dislikes

    author = message.get("author") or {}
    return {
        "id": message["id"],
        "author": author.get("global_name") or author.get("username") or "desconhecido",
        "author_username": author.get("username", ""),
        "created_at": created_at.isoformat(),
        "created_at_display": created_at.strftime("%d/%m/%Y %H:%M"),
        "content": (message.get("content") or "").strip(),
        "likes": likes,
        "dislikes": dislikes,
        "score": score,
        "jump_url": message_link(message, channel_id),
        "attachments": [
            {
                "filename": attachment.get("filename"),
                "url": attachment.get("url"),
            }
            for attachment in (message.get("attachments") or [])
        ],
    }


def sort_suggestions(items: list[dict[str, Any]], sort_key: str) -> list[dict[str, Any]]:
    if sort_key == "new":
        return sorted(items, key=lambda item: item["created_at"], reverse=True)
    if sort_key == "controversial":
        return sorted(
            items,
            key=lambda item: (min(item["likes"], item["dislikes"]), item["score"]),
            reverse=True,
        )
    return sorted(
        items,
        key=lambda item: (item["score"], item["likes"], item["created_at"]),
        reverse=True,
    )


def vote_chart(summary: dict[str, int]) -> dict[str, Any]:
    likes = int(summary.get("total_likes", 0))
    dislikes = int(summary.get("total_dislikes", 0))
    total = likes + dislikes
    if total <= 0:
        return {
            "likes": likes,
            "dislikes": dislikes,
            "likes_pct": 0,
            "dislikes_pct": 0,
            "total": 0,
        }

    return {
        "likes": likes,
        "dislikes": dislikes,
        "likes_pct": round((likes / total) * 100, 1),
        "dislikes_pct": round((dislikes / total) * 100, 1),
        "total": total,
    }


def raw_message_preview(raw_messages: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    preview = []
    for message in raw_messages[:limit]:
        author = message.get("author") or {}
        preview.append(
            {
                "id": message.get("id"),
                "timestamp": message.get("timestamp"),
                "type": message.get("type"),
                "author": {
                    "id": author.get("id"),
                    "username": author.get("username"),
                    "global_name": author.get("global_name"),
                    "bot": author.get("bot", False),
                },
                "content": message.get("content"),
                "reactions": message.get("reactions", []),
                "attachments": message.get("attachments", []),
            }
        )
    return preview


def compact_suggestions_for_llm(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_items = env_int("DEEPSEEK_MAX_SUGGESTIONS", 80)
    max_content_chars = env_int("DEEPSEEK_MAX_CONTENT_CHARS", 350)

    sorted_items = sorted(
        suggestions,
        key=lambda item: (item["likes"] + item["dislikes"], abs(item["score"]), item["created_at"]),
        reverse=True,
    )

    compact = []
    for item in sorted_items[:max_items]:
        compact.append(
            {
                "id": item["id"],
                "author": item["author"],
                "created_at": item["created_at"],
                "likes": item["likes"],
                "dislikes": item["dislikes"],
                "score": item["score"],
                "content": item["content"][:max_content_chars],
            }
        )
    return compact


def compact_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_content_chars = env_int("DEEPSEEK_MAX_CONTENT_CHARS", 350)
    return [
        {
            "id": item["id"],
            "likes": item["likes"],
            "dislikes": item["dislikes"],
            "score": item["score"],
            "content": item["content"][:max_content_chars],
        }
        for item in items
    ]


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fallback_keywords(text: str) -> list[str]:
    words = []
    current = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))

    stopwords = {
        "para", "com", "que", "uma", "por", "das", "dos", "de", "do", "da", "em",
        "no", "na", "nos", "nas", "ser", "ter", "mais", "menos", "isso", "esse",
        "essa", "aqui", "como", "quando", "onde", "porque", "pra", "pro", "the",
        "and", "you", "your", "for", "this", "that", "have", "has",
    }
    filtered = [word for word in words if len(word) >= 4 and word not in stopwords]
    return filtered[:3]


def fallback_label(item: dict[str, Any]) -> dict[str, str]:
    keywords = fallback_keywords(item["content"])
    topic = " ".join(keywords[:2]) if keywords else "sem topico"
    return {
        "topic": topic,
        "category": "Geral",
        "sentiment": "neutro",
    }


def empty_summary() -> dict[str, Any]:
    return {
        "available": False,
        "mode": None,
        "analyzed_count": 0,
        "summary": "",
        "expectations_summary": "",
        "themes": [],
        "topic_summary": [],
        "action_items": [],
        "keywords_by_id": {},
        "labels_by_id": {},
        "feedback": [],
        "conclusao": "",
        "error": None,
    }


def parse_llm_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def deepseek_request_body(messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    return {
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        "messages": messages,
        "temperature": env_float("DEEPSEEK_TEMPERATURE", 0.2),
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }


def parse_deepseek_choice(data: dict[str, Any]) -> dict[str, Any]:
    choice = data["choices"][0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise DashboardError(
            "DeepSeek cortou a resposta por limite de tokens. Reduza o tamanho dos batches "
            "ou aumente DEEPSEEK_BATCH_MAX_TOKENS."
        )
    content = choice["message"].get("content") or ""
    if not content.strip():
        raise DashboardError("DeepSeek retornou conteudo vazio. Tente novamente.")
    try:
        return parse_llm_json(content)
    except json.JSONDecodeError as exc:
        raise DashboardError("DeepSeek retornou JSON invalido. Reduza o tamanho dos batches.") from exc


def build_topic_messages(batch: list[dict[str, Any]]) -> list[dict[str, str]]:
    expected_json = (
        "{"
        "\"suggestion_labels\":[{\"id\":\"message_id\","
        "\"topic\":\"melhorar arena\",\"category\":\"Gameplay\","
        "\"sentiment\":\"critico\"}]"
        "}"
    )
    payload = {"suggestions": compact_batch(batch)}
    return [
        {
            "role": "system",
            "content": (
                "Voce classifica sugestoes de comunidade. Responda somente json valido. "
                "Cada topico deve ser concreto e acionavel, no formato verbo + objeto."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Classifique cada sugestao. EXAMPLE JSON OUTPUT: {expected_json}. "
                "Categorias permitidas: Gameplay, Eventos, Comunidade, Monetizacao, Bugs, "
                "Qualidade de vida, Geral. Sentimentos permitidos: positivo, neutro, critico. "
                "Bons topicos: 'melhorar pareamento arena', 'rever benefício parcerias', 'aumentar recompensas trial', "
                "'corrigir travamentos constantes', 'balancear matchmaking arena'. "
                "Topicos ruins: 'arena', 'jogo', 'sugestao', 'melhoria'. "
                f"Dados: {json.dumps(payload, ensure_ascii=False)}"
            ),
        },
    ]


async def call_deepseek_topic_batch(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    batch: list[dict[str, Any]],
    batch_index: int,
) -> list[dict[str, str]]:
    endpoint = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
    max_tokens = env_int("DEEPSEEK_BATCH_MAX_TOKENS", 1200)

    async with semaphore:
        async with session.post(
            endpoint,
            json=deepseek_request_body(build_topic_messages(batch), max_tokens),
            timeout=aiohttp.ClientTimeout(total=90),
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise DashboardError(f"DeepSeek batch {batch_index} HTTP {response.status}. Detalhe: {text}")
            data = json.loads(text)
            parsed = parse_deepseek_choice(data)
            labels = parsed.get("suggestion_labels", [])
            if not isinstance(labels, list):
                return []
            return labels


def aggregate_topic_summary(
    labels_by_id: dict[str, dict[str, str]],
    suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in suggestions}
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}

    for suggestion_id, label in labels_by_id.items():
        suggestion = by_id.get(suggestion_id)
        if not suggestion:
            continue
        key = (label.get("topic", "sem topico"), label.get("category", "Geral"))
        row = aggregate.setdefault(
            key,
            {
                "topic": key[0],
                "category": key[1],
                "count": 0,
                "total_likes": 0,
                "total_dislikes": 0,
                "_raw": [],
            },
        )
        row["count"] += 1
        row["total_likes"] += suggestion["likes"]
        row["total_dislikes"] += suggestion["dislikes"]
        row["_raw"].append(suggestion)

    result = []
    for row in sorted(
        aggregate.values(),
        key=lambda r: (r["count"], r["total_likes"] + r["total_dislikes"]),
        reverse=True,
    )[:12]:
        top_examples = sorted(row["_raw"], key=lambda s: s["score"], reverse=True)[:3]
        result.append({
            "topic": row["topic"],
            "category": row["category"],
            "count": row["count"],
            "total_likes": row["total_likes"],
            "total_dislikes": row["total_dislikes"],
            "examples": [
                {
                    "id": s["id"],
                    "author": s["author"],
                    "content": s["content"],
                    "likes": s["likes"],
                    "dislikes": s["dislikes"],
                    "score": s["score"],
                    "jump_url": s.get("jump_url"),
                    "created_at_display": s.get("created_at_display", ""),
                }
                for s in top_examples
            ],
        })
    return result


async def call_deepseek_topics_async(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DashboardError("Defina DEEPSEEK_API_KEY no .env para usar a analise com LLM.")

    max_items = env_int("DEEPSEEK_MAX_SUGGESTIONS", 80)
    batch_size = env_int("DEEPSEEK_BATCH_SIZE", 20)
    concurrency = env_int("DEEPSEEK_CONCURRENCY", 3)

    ordered = sorted(
        suggestions,
        key=lambda item: (item["likes"] + item["dislikes"], abs(item["score"]), item["created_at"]),
        reverse=True,
    )[:max_items]
    batches = chunked(ordered, max(batch_size, 1))
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [
            call_deepseek_topic_batch(session, semaphore, batch, index + 1)
            for index, batch in enumerate(batches)
        ]
        results = await asyncio.gather(*tasks)

    labels_by_id = {
        item["id"]: fallback_label(item)
        for item in suggestions
    }
    analyzed_count = 0
    for labels in results:
        for row in labels:
            suggestion_id = str(row.get("id", ""))
            if not suggestion_id:
                continue
            labels_by_id[suggestion_id] = {
                "topic": str(row.get("topic") or "sem topico"),
                "category": str(row.get("category") or "Geral"),
                "sentiment": str(row.get("sentiment") or "neutro"),
            }
            analyzed_count += 1

    return {
        "available": True,
        "mode": "topics",
        "analyzed_count": analyzed_count,
        "summary": "",
        "expectations_summary": "",
        "themes": [],
        "topic_summary": aggregate_topic_summary(labels_by_id, suggestions),
        "action_items": [],
        "keywords_by_id": {},
        "labels_by_id": labels_by_id,
        "error": None,
    }


def call_deepseek_analysis(
    suggestions: list[dict[str, Any]],
    period: dict[str, Any],
    chart: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DashboardError("Defina DEEPSEEK_API_KEY no .env para usar a analise com LLM.")

    compact = compact_suggestions_for_llm(suggestions)
    if not compact:
        return empty_summary()

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
    endpoint = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
    max_tokens = env_int("DEEPSEEK_MAX_TOKENS", 4000)
    temperature = env_float("DEEPSEEK_TEMPERATURE", 0.2)

    prompt_payload = {
        "period": period,
        "votes": chart,
        "suggestions": compact,
    }
    if mode == "topics":
        task = (
            "Classifique as sugestoes em topicos acionaveis. Retorne JSON somente com "
            "topic_summary e suggestion_labels. Nao retorne resumo, temas, acoes nem keywords."
        )
        expected_json = (
            "{"
            "\"topic_summary\":[{\"topic\":\"melhorar arena\",\"category\":\"Gameplay\","
            "\"count\":2,\"total_likes\":18,\"total_dislikes\":3}],"
            "\"suggestion_labels\":[{\"id\":\"message_id\","
            "\"topic\":\"melhorar arena\",\"category\":\"Gameplay\","
            "\"sentiment\":\"critico\"}]"
            "}"
        )
    else:
        task = (
            "Gere um resumo executivo do que o servidor esta pedindo. Retorne JSON somente com "
            "summary, expectations_summary, themes e action_items. "
            "Use numeros do periodo: quantidade de sugestoes, likes, dislikes e temas recorrentes. "
            "Exemplo de estilo: 'Desde o corte do periodo, ha 2 sugestoes sobre arena, "
            "somando 18 likes e 3 dislikes, pedindo matchmaking mais rapido e recompensas melhores.'"
        )
        expected_json = (
            "{"
            "\"summary\":\"resumo geral em 3-5 frases\","
            "\"expectations_summary\":\"o que o servidor parece esperar com numeros\","
            "\"themes\":[{\"name\":\"arena\",\"description\":\"pedidos sobre matchmaking\","
            "\"suggestion_count\":2,\"total_likes\":18,\"total_dislikes\":3,"
            "\"examples\":[\"id1\",\"id2\"]}],"
            "\"action_items\":[\"avaliar matchmaking da arena\"]"
            "}"
        )

    messages = [
        {
            "role": "system",
            "content": (
                "Voce analisa sugestoes de comunidade de um servidor Discord. "
                "Responda somente json valido. Agrupe pedidos parecidos, considere votos, "
                "e escreva em portugues claro e direto. "
                "Topicos devem ser concretos e acionaveis, sempre no formato verbo + objeto."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Tarefa: {task} "
                f"EXAMPLE JSON OUTPUT: {expected_json}. "
                "Bons topicos: 'melhorar arena', 'rever parcerias', 'aumentar recompensas', "
                "'corrigir travamentos', 'balancear matchmaking'. "
                "Topicos ruins: 'arena', 'jogo', 'sugestao', 'melhoria'. "
                "Limite a resposta a no maximo 6 itens agregados. "
                "Nao use markdown. Nao inclua texto fora do JSON. "
                f"Dados: {json.dumps(prompt_payload, ensure_ascii=False)}"
            ),
        },
    ]

    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = exc.response.text if exc.response is not None else str(exc)
        raise DashboardError(f"DeepSeek retornou HTTP {status}. Detalhe: {detail}") from exc
    except requests.RequestException as exc:
        raise DashboardError(f"Falha ao chamar DeepSeek: {exc}") from exc

    data = response.json()
    choice = data["choices"][0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise DashboardError(
            "DeepSeek cortou a resposta por limite de tokens. Reduza DEEPSEEK_MAX_SUGGESTIONS "
            "ou aumente DEEPSEEK_MAX_TOKENS."
        )
    content = choice["message"].get("content") or ""
    if not content.strip():
        raise DashboardError("DeepSeek retornou conteudo vazio. Tente novamente ou reduza o periodo.")
    try:
        parsed = parse_llm_json(content)
    except json.JSONDecodeError as exc:
        raise DashboardError(
            "DeepSeek retornou JSON invalido. Reduza DEEPSEEK_MAX_SUGGESTIONS "
            "para 40 ou 60, ou tente novamente."
        ) from exc

    keywords_by_id = {
        item["id"]: fallback_keywords(item["content"])
        for item in suggestions
    }
    labels_by_id = {
        item["id"]: fallback_label(item)
        for item in suggestions
    }
    for row in parsed.get("keywords_by_suggestion", []):
        suggestion_id = str(row.get("id", ""))
        keywords = row.get("keywords", [])
        if suggestion_id and isinstance(keywords, list):
            keywords_by_id[suggestion_id] = [str(keyword) for keyword in keywords[:3]]

    for row in parsed.get("suggestion_labels", []):
        suggestion_id = str(row.get("id", ""))
        if not suggestion_id:
            continue
        labels_by_id[suggestion_id] = {
            "topic": str(row.get("topic") or labels_by_id.get(suggestion_id, {}).get("topic") or "sem topico"),
            "category": str(row.get("category") or "Geral"),
            "sentiment": str(row.get("sentiment") or "neutro"),
        }

    return {
        "available": True,
        "mode": mode,
        "analyzed_count": len(compact),
        "summary": parsed.get("summary", ""),
        "expectations_summary": parsed.get("expectations_summary", ""),
        "themes": parsed.get("themes", []),
        "topic_summary": parsed.get("topic_summary", []),
        "action_items": parsed.get("action_items", []),
        "keywords_by_id": keywords_by_id,
        "labels_by_id": labels_by_id,
        "error": None,
    }


def call_deepseek_admin_feedback(
    suggestions: list[dict[str, Any]],
    period_key: str,
    period_label: str,
) -> dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DashboardError("Defina DEEPSEEK_API_KEY no .env para usar a analise com LLM.")

    n = TOP_N_BY_PERIOD.get(period_key, 10)
    top = sorted(
        suggestions,
        key=lambda x: (x["score"], x["likes"]),
        reverse=True,
    )[:n]

    if not top:
        return empty_summary()

    compact = [
        {
            "id": item["id"],
            "likes": item["likes"],
            "dislikes": item["dislikes"],
            "score": item["score"],
            "content": item["content"],
        }
        for item in top
    ]

    expected_json = (
        '{"feedback":['
        '{"rank":1,"titulo":"Melhorar matchmaking da arena",'
        '"resumo":"Jogadores pedem partidas mais equilibradas e menor espera",'
        '"n_apoios":23,"n_contra":5,"categoria":"Gameplay","prioridade":"alta"},'
        '{"rank":2,"titulo":"Fila nao competitiva na arena",'
        '"resumo":"Muitos jogadores querem uma modalidade casual sem ranking",'
        '"n_apoios":19,"n_contra":2,"categoria":"Gameplay","prioridade":"alta"},'
        '{"rank":3,"titulo":"Mais eventos sazonais",'
        '"resumo":"Comunidade quer eventos temporarios com recompensas exclusivas",'
        '"n_apoios":16,"n_contra":1,"categoria":"Eventos","prioridade":"media"}'
        '],'
        '"conclusao":'
        '"ARENA PvP (2 sugestoes agrupadas — 42 apoios combinados / 7 contra):\\n'
        'Os jogadores estao pedindo dois ajustes distintos mas relacionados na arena: matchmaking mais equilibrado e a criacao de uma fila nao competitiva (casual). No matchmaking, o relato e de partidas desequilibradas onde times com equipamentos e niveis muito diferentes se enfrentam, gerando frustracao e abandono de partidas. Na fila casual, o pedido e ter um espaco para jogar arena sem impacto no ranking, o que hoje impede jogadores menos equipados de participar do modo PvP.\\n'
        'No Perfect World, a arena e o principal modo PvP organizado do jogo. O sistema atual de matchmaking nao considera adequadamente a diferenca de gearscore entre jogadores, resultando em partidas onde um lado domina completamente. A ausencia de modo casual faz com que jogadores iniciantes ou casuais evitem a arena por completo, reduzindo o pool de jogadores e aumentando o tempo de espera na fila.\\n'
        'Com 42 apoios combinados e score positivo em ambas as sugestoes (18 e 17), arena e claramente o tema mais critico do periodo. A recorrencia do pedido em duas sugestoes distintas reforca que nao e um caso isolado — e uma dor real de uma parcela significativa da base ativa.\\n'
        '\\n'
        'EVENTOS SAZONAIS (1 sugestao — 16 apoios / 1 contra):\\n'
        'A comunidade quer mais eventos temporarios com recompensas exclusivas ligadas a datas comemorativas ou temas especiais.\\n'
        'Perfect World tem historico de eventos sazonais (Ano Novo Chines, Halloween, etc.), mas o servidor atual tem pouca frequencia desses conteudos. Eventos sazonais aumentam o login diario, criam razoes para jogadores inativos voltarem e geram momentos de experiencia coletiva na comunidade.\\n'
        'Com 16 apoios e quase nenhuma rejeicao, e um pedido de baixo esforco com alto retorno de engajamento. Nao e urgente como arena, mas e de facil implementacao e impacto positivo garantido.\\n'
        '\\n'
        'VISAO GERAL E PRIORIDADES:\\n'
        'O periodo revela um servidor com base ativa engajada, mas com pontos de atrito claros no PvP que estao afastando jogadores casuais. Arena concentra mais de 70% dos votos do periodo, sinalizando que e o gargalo principal de satisfacao. Se o matchmaking e a fila casual nao forem endereçados, o risco e perda de jogadores intermediarios que sao exatamente o publico que sustenta a longevidade do servidor. Eventos sazonais sao a segunda prioridade: baixo custo, alto retorno de engajamento e boa receptividade da comunidade. Recomendacao: (1) fila casual arena, (2) ajuste de matchmaking por gearscore, (3) calendario de eventos sazonais trimestral."}'
    )

    messages = [
        {
            "role": "system",
            "content": (
                "Voce e um consultor que analisa sugestoes de comunidade de um servidor de Perfect World. "
                "O destinatario do relatorio e o gestor do servidor, que ja conhece profundamente o jogo. "
                "Nunca explique como as mecanicas do jogo funcionam — isso e obvio para o leitor. "
                "Foque em: o que especificamente esta sendo reclamado ou pedido, "
                "com que frequencia o tema aparece, e qual o impacto real para o servidor. "
                "Responda somente JSON valido em portugues brasileiro."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Analise as TOP {n} sugestoes mais votadas do periodo '{period_label}'. "
                "Gere um relatorio de feedback para o administrador do servidor. "
                f"EXAMPLE JSON OUTPUT: {expected_json}. "
                "Para cada sugestao no array 'feedback' inclua: "
                "rank (posicao, 1=mais votada), "
                "titulo (nome claro em ate 8 palavras), "
                "resumo (o que foi pedido em 1-2 frases diretas), "
                "n_apoios (numero exato de likes), "
                "n_contra (numero exato de dislikes), "
                "categoria (Gameplay/Eventos/Comunidade/Monetizacao/Bugs/Qualidade de vida/Geral), "
                "prioridade (alta se score acima de 10, media se score entre 4 e 10, baixa se score abaixo de 4). "
                "O campo 'conclusao' e um relatorio executivo tematico para o gestor do servidor. "
                "NAO explique como o jogo funciona — o gestor ja sabe. "
                "NAO liste item por item. Agrupe as sugestoes do array 'feedback' por tema. "
                "Para cada bloco tematico escreva no formato: "
                "'[TEMA EM MAIUSCULO] ([X sugestoes agrupadas] — [N] apoios / [M] contra):\n"
                "[O que especificamente esta sendo reclamado ou pedido, com detalhes concretos das sugestoes. Cite os pedidos exatos.]\n"
                "[Frequencia e volume: quantas sugestoes distintas tocam nesse tema, quao concentrado e o apoio, se e padrao recorrente ou ponto novo.]\n"
                "[Urgencia e impacto: o que acontece se nao for atendido — risco de evasao, insatisfacao crescente, impacto na retencao. Cite numeros.]'. "
                "Separe blocos com linha em branco. "
                "Finalize com 'VISAO GERAL E PRIORIDADES' em 4-5 frases: "
                "o que o periodo revela sobre o estado do servidor, quais sao os pontos criticos vs. desejos secundarios, "
                "e a ordem de acao recomendada com justificativa. "
                "Pode ser longo. Use numeros reais. Nao use markdown. Nao inclua texto fora do JSON. "
                f"Dados: {json.dumps({'sugestoes': compact}, ensure_ascii=False)}"
            ),
        },
    ]

    endpoint = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions").strip()
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
    max_tokens = env_int("DEEPSEEK_MAX_TOKENS", 4000)
    temperature = env_float("DEEPSEEK_TEMPERATURE", 0.2)

    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = exc.response.text if exc.response is not None else str(exc)
        raise DashboardError(f"DeepSeek retornou HTTP {status}. Detalhe: {detail}") from exc
    except requests.RequestException as exc:
        raise DashboardError(f"Falha ao chamar DeepSeek: {exc}") from exc

    data = response.json()
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise DashboardError("DeepSeek cortou a resposta por limite de tokens.")
    content = choice["message"].get("content") or ""
    if not content.strip():
        raise DashboardError("DeepSeek retornou conteudo vazio.")
    try:
        parsed = parse_llm_json(content)
    except json.JSONDecodeError as exc:
        raise DashboardError("DeepSeek retornou JSON invalido.") from exc

    return {
        "available": True,
        "mode": "feedback",
        "analyzed_count": len(top),
        "summary": parsed.get("conclusao", ""),
        "expectations_summary": "",
        "themes": [],
        "topic_summary": [],
        "action_items": [],
        "keywords_by_id": {},
        "labels_by_id": {},
        "feedback": parsed.get("feedback", []),
        "conclusao": parsed.get("conclusao", ""),
        "error": None,
    }


def load_suggestions(period_key: str, force_refresh: bool = False) -> dict[str, Any]:
    channel_id = os.getenv("DISCORD_SUGGESTIONS_CHANNEL_ID", DEFAULT_CHANNEL_ID).strip()
    page_limit = env_int("DISCORD_FETCH_PAGE_SIZE", 100)
    max_messages = env_int("DISCORD_MAX_MESSAGES", 1000)
    cache_ttl = env_int("CACHE_TTL_SECONDS", 120)
    include_bots = env_bool("INCLUDE_BOT_MESSAGES", False)
    positive_emojis = parse_emoji_list("POSITIVE_EMOJIS", "\U0001F44D")
    negative_emojis = parse_emoji_list("NEGATIVE_EMOJIS", "\U0001F44E")
    period_label, cutoff_datetime, after_snowflake = period_cutoff(period_key)

    cache_key = (
        f"{channel_id}:{period_key}:{page_limit}:{max_messages}:"
        f"{','.join(sorted(positive_emojis))}:{','.join(sorted(negative_emojis))}:"
        f"{include_bots}"
    )
    cache_entry = _CACHE.get(cache_key)
    if cache_entry and not force_refresh and (time.time() - cache_entry.created_at) < cache_ttl:
        return cache_entry.payload

    raw_messages = fetch_channel_messages(
        channel_id=channel_id,
        page_limit=page_limit,
        after_snowflake=after_snowflake,
        max_messages=max_messages,
    )

    suggestions = []
    for message in raw_messages:
        if message.get("type") not in (0, 19):
            continue
        if not include_bots and (message.get("author") or {}).get("bot"):
            continue
        if not (message.get("content") or "").strip() and not message.get("attachments"):
            continue

        suggestions.append(
            normalize_message(
                message=message,
                channel_id=channel_id,
                positive_emojis=positive_emojis,
                negative_emojis=negative_emojis,
            )
        )

    raw_preview = raw_message_preview(raw_messages)
    payload = {
        "channel_id": channel_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "period": {
            "key": period_key,
            "label": period_label,
            "cutoff": cutoff_datetime.isoformat() if cutoff_datetime else None,
            "after_snowflake": str(after_snowflake) if after_snowflake else None,
            "max_messages": max_messages,
            "raw_messages": len(raw_messages),
        },
        "summary": {
            "total_messages": len(suggestions),
            "total_likes": sum(item["likes"] for item in suggestions),
            "total_dislikes": sum(item["dislikes"] for item in suggestions),
        },
        "suggestions": suggestions,
        "raw_preview": raw_preview,
        "raw_preview_json": json.dumps(raw_preview, ensure_ascii=False, indent=2),
    }
    payload["vote_chart"] = vote_chart(payload["summary"])

    _CACHE[cache_key] = CacheEntry(created_at=time.time(), payload=payload)
    return payload


@app.get("/")
def index():
    sort_by = request.args.get("sort", "top")
    period_key = request.args.get("period")
    force_refresh = request.args.get("refresh") == "1"
    topics = request.args.get("topics") == "1"
    summarize = request.args.get("summarize") == "1"
    feedback = request.args.get("feedback") == "1"
    error_message = None
    summary_error = None

    payload = {
        "channel_id": os.getenv("DISCORD_SUGGESTIONS_CHANNEL_ID", DEFAULT_CHANNEL_ID).strip(),
        "fetched_at": None,
        "period": None,
        "summary": {"total_messages": 0, "total_likes": 0, "total_dislikes": 0},
        "suggestions": [],
        "raw_preview": [],
        "raw_preview_json": "[]",
        "vote_chart": {"likes": 0, "dislikes": 0, "likes_pct": 0, "dislikes_pct": 0, "total": 0},
    }
    llm_summary = empty_summary()
    suggestions: list[dict[str, Any]] = []
    top_positive: list[dict[str, Any]] = []
    top_negative: list[dict[str, Any]] = []

    try:
        if period_key:
            payload = load_suggestions(period_key=period_key, force_refresh=force_refresh)
            suggestions = sort_suggestions(payload["suggestions"], sort_by)
            llm_summary = empty_summary()
            if topics or summarize or feedback:
                try:
                    if topics:
                        llm_summary = asyncio.run(call_deepseek_topics_async(payload["suggestions"]))
                    elif feedback:
                        llm_summary = call_deepseek_admin_feedback(
                            payload["suggestions"],
                            period_key,
                            payload["period"]["label"],
                        )
                    else:
                        llm_summary = call_deepseek_analysis(
                            payload["suggestions"],
                            payload["period"],
                            payload["vote_chart"],
                            "summary",
                        )
                except DashboardError as exc:
                    summary_error = str(exc)
                    app.logger.exception("Falha ao gerar resumo com DeepSeek.")
                except Exception as exc:
                    summary_error = f"Erro inesperado na analise: {exc}"
                    app.logger.exception("Erro inesperado ao gerar analise com DeepSeek.")
            top_positive = suggestions[:5]
            top_negative = sorted(
                payload["suggestions"],
                key=lambda item: (item["dislikes"], item["created_at"]),
                reverse=True,
            )[:5]
    except DashboardError as exc:
        error_message = str(exc)
        app.logger.exception("Falha ao carregar painel de sugestoes.")
    except Exception as exc:
        error_message = f"Erro inesperado: {exc}"
        app.logger.exception("Erro inesperado no painel de sugestoes.")

    return render_template(
        "index.html",
        payload=payload,
        suggestions=suggestions,
        sort_by=sort_by,
        period_key=period_key,
        periods=PERIODS,
        top_positive=top_positive,
        top_negative=top_negative,
        error_message=error_message,
        summary_error=summary_error,
        topics=topics,
        summarize=summarize,
        feedback=feedback,
        llm_summary=llm_summary,
    )


@app.get("/api/suggestions")
def api_suggestions():
    sort_by = request.args.get("sort", "top")
    period_key = request.args.get("period", "30d")
    force_refresh = request.args.get("refresh") == "1"
    try:
        payload = load_suggestions(period_key=period_key, force_refresh=force_refresh)
        api_payload = {
            **payload,
            "suggestions": sort_suggestions(payload["suggestions"], sort_by),
        }
        return jsonify(api_payload)
    except DashboardError as exc:
        app.logger.exception("Falha na API de sugestoes.")
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        app.logger.exception("Erro inesperado na API de sugestoes.")
        return jsonify({"error": f"Erro inesperado: {exc}"}), 500


@app.get("/logo")
def serve_logo():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "logo_theclassic.png")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=env_int("PORT", 5000), debug=False)
