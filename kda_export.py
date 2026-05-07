"""
kda_export.py — Gera relatorio HTML estatico de KDA a partir de mensagens de kill no Discord.

Uso:
    python kda_export.py --start 1501720378601508934 --stop 1501772401929617469 --clans Thunder,Vanguarda,Train
    python kda_export.py --start 1501720378601508934 --stop 1501772401929617469 --clans Thunder,Vanguarda --output guerra_07-05.html
"""

import argparse
import asyncio
import base64
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def find_kda_reports() -> list[dict]:
    result = []
    for f in sorted(Path(".").glob("guerra_*.html"), reverse=True):
        m = re.search(r"guerra_(\d{2}-\d{2})", f.name)
        label = f"KDA {m.group(1).replace('-', '/')}" if m else f"KDA {f.stem}"
        result.append({"file": f.name, "label": label})
    return result

import aiohttp
from dotenv import load_dotenv
from flask import Flask, render_template

load_dotenv()

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
DEFAULT_KDA_CHANNEL_ID = "1035423433040875590"
BRT = timedelta(hours=-3)  # Brasilia Time = UTC-3

KILL_PATTERN = re.compile(
    r"\[(.*?)\].+?\*\*(.+?)\*\* matou .+?\[(.*?)\].+?\*\*(.+?)\*\*"
)

_app = Flask(__name__)


def norm_clan(s: str) -> str:
    """Normaliza nome de clan: remove acentos e converte para uppercase."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.strip().upper()


def snowflake_to_dt(snowflake: int) -> datetime:
    ms = (snowflake >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def kill_from_message(msg: dict) -> str | None:
    """Retorna uma string resumida do primeiro kill encontrado na mensagem."""
    for embed in (msg.get("embeds") or []):
        for text in [embed.get("description") or ""] + [f.get("value", "") for f in (embed.get("fields") or [])]:
            for line in re.split(r"[\r\n]+", text):
                m = KILL_PATTERN.search(line)
                if m:
                    return f"[{m.group(1)}] {m.group(2)}  ->  [{m.group(3)}] {m.group(4)}"
    return None


async def fetch_boundary_messages(
    channel_id: str, start_id: int, stop_id: int
) -> tuple[dict | None, dict | None]:
    """Busca a primeira mensagem (apos start) e a ultima (antes de stop)."""
    headers = build_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        # primeira mensagem do range
        async with session.get(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            params={"limit": "1", "after": str(start_id - 1)},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            first_batch = await r.json() if r.status < 400 else []
        first_msg = first_batch[0] if first_batch else None

        # ultima mensagem do range
        async with session.get(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            params={"limit": "1", "before": str(stop_id)},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            last_batch = await r.json() if r.status < 400 else []
        last_msg = last_batch[0] if last_batch else None

    return first_msg, last_msg


def encode_logo() -> str:
    logo_path = Path(__file__).parent / "logo_theclassic.png"
    if not logo_path.exists():
        return ""
    with open(logo_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f"data:image/png;base64,{data}"


def build_headers() -> dict[str, str]:
    token = (
        os.getenv("DISCORD_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("Defina DISCORD_TOKEN ou DISCORD_BOT_TOKEN no .env.")
    return {"Authorization": token, "User-Agent": "kda-dashboard/1.0"}


def _register_kill(
    players: dict, clans: dict,
    killer_clan: str, killer: str,
    victim_clan: str, victim: str,
) -> None:
    players.setdefault(killer, {"clan": killer_clan, "K": 0, "D": 0})
    players.setdefault(victim, {"clan": victim_clan, "K": 0, "D": 0})
    players[killer]["K"] += 1
    players[victim]["D"] += 1

    for clan_name in (killer_clan, victim_clan):
        if clan_name not in clans:
            clans[clan_name] = {"K": 0, "D": 0, "members": set()}
    clans[killer_clan]["K"] += 1
    clans[killer_clan]["members"].add(killer)
    clans[victim_clan]["D"] += 1
    clans[victim_clan]["members"].add(victim)


def _parse_msg_kills(
    msg: dict, players: dict, clans: dict,
    confrontations: dict, timeline_kills: list,
    war_clans: set[str] | None = None,
) -> int:
    """Extrai kills de uma mensagem e atualiza players/clans. Retorna qtd de kills."""
    count = 0
    texts: list[str] = []

    for embed in (msg.get("embeds") or []):
        if embed.get("description"):
            texts.append(embed["description"])
        for field in (embed.get("fields") or []):
            if field.get("value"):
                texts.append(field["value"])
    if msg.get("content"):
        texts.append(msg["content"])

    ts: datetime | None = None
    ts_str = msg.get("timestamp", "")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            pass

    for text in texts:
        for line in re.split(r"[\r\n]+", text):
            m = KILL_PATTERN.search(line)
            if not m:
                continue
            killer_clan_raw, killer, victim_clan_raw, victim = m.groups()
            killer_clan = norm_clan(killer_clan_raw)
            victim_clan = norm_clan(victim_clan_raw)
            if war_clans and not (killer_clan in war_clans and victim_clan in war_clans):
                continue
            _register_kill(players, clans, killer_clan, killer, victim_clan, victim)
            confrontations[(killer, victim)] = confrontations.get((killer, victim), 0) + 1
            if ts:
                timeline_kills.append((ts, killer_clan))
            count += 1
    return count


async def fetch_and_parse(
    channel_id: str, start_id: int, stop_id: int,
    war_clans: set[str] | None = None,
) -> tuple[dict, dict, int, int, dict, list]:
    """Busca mensagens e parseia kills. Retorna (players, clans, kills, msgs, confrontations, timeline_kills)."""
    headers = build_headers()
    players: dict[str, dict] = {}
    clans: dict[str, dict] = {}
    confrontations: dict[tuple, int] = {}
    timeline_kills: list = []
    after = str(start_id)
    page = 0
    total_kills = 0
    total_msgs = 0

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            params = {"limit": "100", "after": after}
            async with session.get(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 429:
                    data = await response.json()
                    retry = float(data.get("retry_after", 1))
                    print(f"\n  Rate limited. Aguardando {retry}s...")
                    await asyncio.sleep(retry)
                    continue
                if response.status >= 400:
                    text = await response.text()
                    raise RuntimeError(f"Discord API HTTP {response.status}: {text}")
                batch = await response.json()

            if not batch:
                break

            page += 1
            reached_stop = False
            for msg in reversed(batch):
                if int(msg["id"]) >= stop_id:
                    reached_stop = True
                    break
                total_msgs += 1
                total_kills += _parse_msg_kills(
                    msg, players, clans, confrontations, timeline_kills, war_clans
                )

            print(
                f"\r        pagina {page} — {total_msgs} msgs — {total_kills} kills...",
                end="", flush=True,
            )

            if reached_stop or len(batch) < 100:
                break

            after = batch[0]["id"]

    for p in players.values():
        p["kd_ratio"] = round(p["K"] / max(p["D"], 1), 2)
    for c in clans.values():
        c["member_count"] = len(c["members"])
        del c["members"]
        c["kd_ratio"] = round(c["K"] / max(c["D"], 1), 2)

    print(f"\r        {total_msgs} msgs processadas — {total_kills} kills encontrados        ")
    return players, clans, total_kills, total_msgs, confrontations, timeline_kills


def build_leaderboard(players: dict) -> list[dict]:
    return sorted(
        [{"name": name, **stats} for name, stats in players.items()],
        key=lambda r: (r["K"], r["kd_ratio"]),
        reverse=True,
    )


def build_clan_leaderboard(clans: dict) -> list[dict]:
    return sorted(
        [{"clan": name, **stats} for name, stats in clans.items()],
        key=lambda r: (r["K"], r["kd_ratio"]),
        reverse=True,
    )


def build_top_confrontations(confrontations: dict, players: dict, top_n: int = 15) -> list[dict]:
    result = []
    for (killer, victim), count in confrontations.items():
        result.append({
            "killer": killer,
            "killer_clan": players.get(killer, {}).get("clan", "?"),
            "victim": victim,
            "victim_clan": players.get(victim, {}).get("clan", "?"),
            "count": count,
        })
    return sorted(result, key=lambda r: r["count"], reverse=True)[:top_n]


def build_timeline_chart(timeline_kills: list, bucket_minutes: int = 5) -> str:
    if not timeline_kills:
        return json.dumps({"labels": [], "datasets": []})

    buckets: dict = defaultdict(lambda: defaultdict(int))
    for ts_utc, killer_clan in timeline_kills:
        ts = ts_utc + BRT  # converte para horário de Brasília
        total_min = ts.hour * 60 + ts.minute
        snapped = (total_min // bucket_minutes) * bucket_minutes
        bucket_ts = ts.replace(
            hour=snapped // 60, minute=snapped % 60, second=0, microsecond=0
        )
        buckets[bucket_ts][killer_clan] += 1

    sorted_times = sorted(buckets.keys())
    labels = [t.strftime("%H:%M") for t in sorted_times]
    all_clans = sorted({clan for bk in buckets.values() for clan in bk})

    palette = [
        "#4fc3f7", "#ef5350", "#66bb6a", "#ffa726",
        "#ab47bc", "#26c6da", "#d4e157", "#ff7043",
        "#ec407a", "#78909c",
    ]
    datasets = []
    for i, clan in enumerate(all_clans):
        color = palette[i % len(palette)]
        datasets.append({
            "label": clan,
            "data": [buckets[t].get(clan, 0) for t in sorted_times],
            "borderColor": color,
            "backgroundColor": color + "33",
            "fill": False,
            "tension": 0.3,
            "pointRadius": 3,
        })

    return json.dumps({"labels": labels, "datasets": datasets})


async def fetch_first_page(channel_id: str, start_id: int) -> list[dict]:
    headers = build_headers()
    params = {"limit": "100", "after": str(start_id)}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Discord API HTTP {response.status}: {text}")
            return await response.json()


def run_preview(channel_id: str, start_id: int) -> None:
    import json as _json
    print()
    print("  Buscando primeira pagina do canal...")
    batch = asyncio.run(fetch_first_page(channel_id, start_id))

    if not batch:
        print("  Nenhuma mensagem encontrada apos o start_id. Verifique o canal e o ID.")
        return

    print(f"  {len(batch)} mensagens retornadas.\n")

    # Inspeciona primeiras 3 mensagens em detalhe
    for i, msg in enumerate(batch[:3]):
        author = (msg.get("author") or {})
        name = author.get("global_name") or author.get("username") or "?"
        embeds = msg.get("embeds") or []
        content = (msg.get("content") or "")
        print(f"  ── Msg {i+1}  id={msg.get('id')}  autor={name} ──")
        print(f"     content  : {repr(content[:120])}")
        print(f"     embeds   : {len(embeds)} embed(s)")
        for j, emb in enumerate(embeds[:3]):
            desc = (emb.get("description") or "")
            lines = [l for l in re.split(r"[\r\n]+", desc) if l.strip()]
            kills_in_emb = sum(1 for l in lines if KILL_PATTERN.search(l))
            print(f"       embed[{j}] linhas={len(lines)} kills={kills_in_emb}  preview: {desc[:100]}")
        if len(embeds) > 3:
            print(f"       ... +{len(embeds)-3} embeds")
        print()

    # Conta kills encontrados e mostra um exemplo
    total_kills = 0
    example: str | None = None
    for msg in batch:
        for embed in (msg.get("embeds") or []):
            for text in [embed.get("description") or ""] + [f.get("value", "") for f in (embed.get("fields") or [])]:
                for line in re.split(r"[\r\n]+", text):
                    m = KILL_PATTERN.search(line)
                    if m:
                        total_kills += 1
                        if example is None:
                            example = f"  killer: [{m.group(1)}] {m.group(2)}  |  victim: [{m.group(3)}] {m.group(4)}"

    print(f"  Kills detectados na 1a pagina : {total_kills}")
    if example:
        print(f"  Exemplo de kill capturado     :")
        print(f"    {example}")
    else:
        print("  AVISO: nenhum kill detectado. Verifique canal e formato.")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta relatorio KDA estatico a partir de mensagens do Discord."
    )
    parser.add_argument("--start", required=True, type=int, help="ID da mensagem inicial")
    parser.add_argument("--stop", required=True, type=int, help="ID da mensagem final (exclusivo)")
    parser.add_argument(
        "--channel",
        default=None,
        help=f"ID do canal (padrao: env KDA_CHANNEL_ID ou {DEFAULT_KDA_CHANNEL_ID})",
    )
    parser.add_argument(
        "--output",
        default="kda_report.html",
        help="Arquivo de saida (padrao: kda_report.html)",
    )
    parser.add_argument(
        "--clans",
        default=None,
        help="Clans da guerra separados por virgula (ex: Thunder,Vanguarda,Train). Conta apenas kills entre esses clans.",
    )
    parser.add_argument(
        "--bucket",
        type=int,
        default=1,
        metavar="MINUTOS",
        help="Intervalo do grafico em minutos (padrao: 1)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Mostra as primeiras mensagens do canal e sai (sem gerar relatorio)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    channel_id = (
        args.channel
        or os.getenv("KDA_CHANNEL_ID", "").strip()
        or DEFAULT_KDA_CHANNEL_ID
    )

    war_clans: set[str] | None = None
    if args.clans:
        war_clans = {norm_clan(c) for c in args.clans.split(",")}

    print()
    print("=" * 52)
    print("  The Classic PW — KDA Report")
    print("=" * 52)
    print(f"  Canal    : {channel_id}")
    if war_clans:
        print(f"  Clans    : {', '.join(sorted(war_clans))}")

    if args.preview:
        print(f"  Start ID : {args.start}")
        print(f"  Modo     : preview (primeira pagina)")
        run_preview(channel_id, args.start)
        return
    dt_start = snowflake_to_dt(args.start) + BRT
    dt_stop  = snowflake_to_dt(args.stop) + BRT
    print(f"  Periodo  : {dt_start.strftime('%d/%m/%Y %H:%M')} -> {dt_stop.strftime('%d/%m/%Y %H:%M')} BRT")
    print()

    print("  Verificando limites do periodo...")
    first_msg, last_msg = asyncio.run(fetch_boundary_messages(channel_id, args.start, args.stop))
    if first_msg:
        ts = first_msg.get("timestamp", "")[:16].replace("T", " ")
        kill = kill_from_message(first_msg) or "(sem kill detectado)"
        print(f"  Inicio   : {ts}  |  {kill}")
    if last_msg:
        ts = last_msg.get("timestamp", "")[:16].replace("T", " ")
        kill = kill_from_message(last_msg) or "(sem kill detectado)"
        print(f"  Fim      : {ts}  |  {kill}")
    print()

    print("  [1/2] Buscando e processando kills...")
    players, clans, total_kills, total_msgs, confrontations, timeline_kills = asyncio.run(
        fetch_and_parse(channel_id, args.start, args.stop, war_clans)
    )
    print(f"        {total_kills} kills | {len(players)} jogadores | {len(clans)} clans")

    leaderboard = build_leaderboard(players)
    clan_leaderboard = build_clan_leaderboard(clans)
    top_killers = leaderboard[:5]
    top_deaths = sorted(leaderboard, key=lambda r: (r["D"], -r["K"]), reverse=True)[:5]
    top_survivors = sorted(
        [p for p in leaderboard if p["K"] > 0 or p["D"] > 0],
        key=lambda r: (r["D"], -r["K"]),
    )[:10]
    top_confrontations = build_top_confrontations(confrontations, players)
    timeline_chart_json = build_timeline_chart(timeline_kills, bucket_minutes=args.bucket)

    print("  [2/2] Gerando HTML...")
    output_path = Path(args.output)
    generated_at = datetime.now(timezone.utc) + BRT

    kda_reports = find_kda_reports()
    # garante que o arquivo atual aparece na lista (caso ainda não exista em disco)
    current_file = output_path.name
    if not any(k["file"] == current_file for k in kda_reports):
        m = re.search(r"guerra_(\d{2}-\d{2})", current_file)
        label = f"KDA {m.group(1).replace('-', '/')}" if m else f"KDA {output_path.stem}"
        kda_reports.insert(0, {"file": current_file, "label": label})

    with _app.app_context():
        html = render_template(
            "kda_report.html",
            bucket_minutes=args.bucket,
            leaderboard=leaderboard,
            clan_leaderboard=clan_leaderboard,
            top_killers=top_killers,
            top_deaths=top_deaths,
            top_survivors=top_survivors,
            top_confrontations=top_confrontations,
            timeline_chart_json=timeline_chart_json,
            total_kills=total_kills,
            total_players=len(players),
            total_clans=len(clans),
            start_id=args.start,
            stop_id=args.stop,
            channel_id=channel_id,
            generated_at=generated_at,
            logo_data_url=encode_logo(),
            kda_reports=kda_reports,
            current_file=current_file,
        )

    output_path.write_text(html, encoding="utf-8")
    size_kb = round(output_path.stat().st_size / 1024, 1)
    print(f"  [OK] {output_path.name}  ({size_kb} KB)")
    print(f"  Arquivo : {output_path.resolve()}")
    print(f"  Gerado em: {generated_at.strftime('%d/%m/%Y %H:%M')} BRT")
    print()


if __name__ == "__main__":
    main()
