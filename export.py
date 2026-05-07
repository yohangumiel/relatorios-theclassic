"""
export.py — Gera um relatorio HTML estatico do dashboard de sugestoes.

Uso:
    python export.py --period 30d --feedback
    python export.py --period 90d --topics --output relatorio_mensal.html

O arquivo gerado e completamente auto-contido (sem servidor, sem API keys expostas).
Pode ser hospedado no GitHub Pages, Netlify, Cloudflare Pages ou enviado por e-mail.
"""

import argparse
import asyncio
import base64
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import render_template

from app import (
    PERIODS,
    TOP_N_BY_PERIOD,
    app,
    aggregate_topic_summary,
    call_deepseek_admin_feedback,
    call_deepseek_topics_async,
    empty_summary,
    load_suggestions,
    sort_suggestions,
)


def make_kda_reports(kda_files: list[str]) -> list[dict]:
    """Converte lista de nomes de arquivo em dicts para o template."""
    result = []
    for name in kda_files:
        m = re.search(r"guerra_(\d{2}-\d{2})", name)
        label = f"KDA {m.group(1).replace('-', '/')}" if m else f"KDA {Path(name).stem}"
        result.append({"file": Path(name).name, "label": label})
    return result


def encode_logo() -> str:
    logo_path = Path(__file__).parent / "logo_theclassic.png"
    if not logo_path.exists():
        return ""
    with open(logo_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f"data:image/png;base64,{data}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta relatorio estatico HTML do dashboard de sugestoes."
    )
    parser.add_argument(
        "--period",
        default="30d",
        choices=list(PERIODS.keys()),
        help="Periodo a buscar (padrao: 30d)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_periods",
        help="Gerar relatorios para todos os periodos (ignora --period e --output)",
    )
    parser.add_argument(
        "--sort",
        default="top",
        choices=["top", "new", "controversial"],
        help="Ordenacao das sugestoes (padrao: top)",
    )
    parser.add_argument(
        "--feedback",
        action="store_true",
        help="Gerar Feedback Adm com IA (requer DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--topics",
        action="store_true",
        help="Classificar topicos com IA (requer DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--output",
        default="relatorio.html",
        help="Nome do arquivo de saida (padrao: relatorio.html, ignorado com --all)",
    )
    parser.add_argument(
        "--kda",
        action="append",
        default=[],
        metavar="ARQUIVO",
        help="Arquivo KDA a incluir no nav (ex: guerra_07-05.html). Pode repetir.",
    )
    return parser.parse_args()


def step(n: int, total: int, msg: str) -> None:
    print(f"  [{n}/{total}] {msg}")


def generate_one(
    period_key: str,
    sort_by: str,
    use_feedback: bool,
    use_topics: bool,
    output_path: Path,
    logo_data_url: str,
    generated_at: datetime,
    kda_reports: list | None = None,
    step_prefix: str = "",
) -> None:
    use_ai = use_feedback or use_topics
    total_steps = 3 if use_ai else 2
    pfx = step_prefix

    print(f"{pfx}[1/{total_steps}] Buscando sugestoes do periodo '{period_key}'...")
    payload = load_suggestions(period_key=period_key, force_refresh=True)
    count = payload["summary"]["total_messages"]
    print(f"{pfx}       {count} sugestoes encontradas.")

    suggestions = sort_suggestions(payload["suggestions"], sort_by)
    top_positive = suggestions[:5]
    top_negative = sorted(
        payload["suggestions"],
        key=lambda s: (s["dislikes"], s["created_at"]),
        reverse=True,
    )[:5]

    llm_summary = empty_summary()

    if use_ai:
        if use_feedback:
            print(f"{pfx}[2/{total_steps}] Gerando Feedback Adm com IA...")
            try:
                llm_summary = call_deepseek_admin_feedback(
                    payload["suggestions"],
                    period_key,
                    payload["period"]["label"],
                )
                print(f"{pfx}       {llm_summary.get('analyzed_count', 0)} sugestoes no feedback.")
            except Exception as exc:
                print(f"{pfx}AVISO: Falha no Feedback Adm: {exc}")

            print(f"{pfx}       Classificando topicos...")
            try:
                topics_result = asyncio.run(
                    call_deepseek_topics_async(payload["suggestions"])
                )
                llm_summary["topic_summary"] = topics_result.get("topic_summary", [])
                llm_summary["labels_by_id"] = topics_result.get("labels_by_id", {})
                llm_summary["mode"] = "full"
                print(f"{pfx}       {topics_result.get('analyzed_count', 0)} sugestoes classificadas por topico.")
            except Exception as exc:
                print(f"{pfx}AVISO: Falha na classificacao de topicos: {exc}")
        else:
            print(f"{pfx}[2/{total_steps}] Classificando topicos com IA...")
            try:
                llm_summary = asyncio.run(
                    call_deepseek_topics_async(payload["suggestions"])
                )
                print(f"{pfx}       {llm_summary.get('analyzed_count', 0)} sugestoes analisadas pela IA.")
            except Exception as exc:
                print(f"{pfx}AVISO: Falha na analise com IA: {exc}")
                print(f"{pfx}       O relatorio sera gerado sem analise.")

    print(f"{pfx}[{total_steps}/{total_steps}] Gerando HTML...")

    template_name = "export_dash.html"

    with app.app_context():
        html = render_template(
            template_name,
            payload=payload,
            suggestions=suggestions,
            sort_by=sort_by,
            period_key=period_key,
            periods=PERIODS,
            top_positive=top_positive,
            top_negative=top_negative,
            llm_summary=llm_summary,
            generated_at=generated_at,
            logo_data_url=logo_data_url,
            kda_reports=kda_reports or [],
        )

    output_path.write_text(html, encoding="utf-8")
    size_kb = round(output_path.stat().st_size / 1024, 1)
    ai_label = f"sim ({llm_summary['mode']})" if llm_summary["available"] else "nao"
    print(f"{pfx}[OK] {output_path.name}  ({size_kb} KB) · {count} sugestoes · IA: {ai_label}")


def main() -> None:
    args = parse_args()

    print()
    print("=" * 52)
    print("  The Classic PW — Exportador de Relatorio")
    print("=" * 52)
    print()

    logo_data_url = encode_logo()
    generated_at = datetime.now(timezone.utc)
    kda_reports = make_kda_reports(args.kda)
    if kda_reports:
        print(f"  KDA linkado: {', '.join(k['label'] for k in kda_reports)}")

    if args.all_periods:
        periods_to_run = list(PERIODS.keys())
        print(f"  Modo: todos os periodos ({', '.join(periods_to_run)})")
        print(f"  Gerado em: {generated_at.strftime('%d/%m/%Y %H:%M')} UTC")
        print()
        errors = []
        for period_key in periods_to_run:
            output_path = Path(f"relatorio_{period_key}.html")
            print(f"  -- {period_key} --------------------------")
            try:
                generate_one(
                    period_key=period_key,
                    sort_by=args.sort,
                    use_feedback=args.feedback,
                    use_topics=args.topics,
                    output_path=output_path,
                    logo_data_url=logo_data_url,
                    generated_at=generated_at,
                    kda_reports=kda_reports,
                    step_prefix="  ",
                )
            except Exception as exc:
                print(f"  ERRO no periodo {period_key}: {exc}")
                errors.append(period_key)
            print()

        print("=" * 52)
        if errors:
            print(f"  Concluido com erros nos periodos: {', '.join(errors)}")
        else:
            print("  Todos os relatorios gerados com sucesso!")
        print(f"  Arquivos: {', '.join(f'relatorio_{p}.html' for p in periods_to_run)}")
        print()

    else:
        output_path = Path(args.output)
        try:
            generate_one(
                period_key=args.period,
                sort_by=args.sort,
                use_feedback=args.feedback,
                use_topics=args.topics,
                output_path=output_path,
                logo_data_url=logo_data_url,
                generated_at=generated_at,
                kda_reports=kda_reports,
            )
        except Exception as exc:
            print(f"\n  ERRO: {exc}")
            sys.exit(1)
        print(f"  Arquivo : {output_path.resolve()}")
        print(f"  Gerado em: {generated_at.strftime('%d/%m/%Y %H:%M')} UTC")
        print()


if __name__ == "__main__":
    main()
