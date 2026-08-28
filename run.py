"""Точка запуска без установки пакета.

    python run.py ingest --source data/raw/Pricelist20260826.xlsx
    python run.py llm                       # проверить провайдеров модели
    python run.py media                     # фотографии с сайта → в базу знаний
    python run.py widget
    python run.py telegram
    python run.py search "мячи для спортивного зала"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Бот-магазин ЭЛТИ-КУДИЦ")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="собрать базу знаний из выгрузки 1С")
    ingest.add_argument("--source", default="data/raw/Pricelist20260826.xlsx")
    ingest.add_argument("--out", default="data/kb")

    norms = sub.add_parser("norms", help="разобрать реестр «пункт приказа 1057 → код 1С»")
    norms.add_argument("--source", default="Baza-Ivan-25-11-25.pdf")
    norms.add_argument("--show-unmatched", type=int, default=10,
                       help="сколько ненайденных кодов показать")

    sub.add_parser("widget", help="поднять веб-виджет и демо-страницу")
    sub.add_parser("telegram", help="запустить Telegram-бота")
    sub.add_parser("llm", help="проверить провайдеров модели по шагам")

    search = sub.add_parser("search", help="проверить поиск из консоли")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)

    media = sub.add_parser("media", help="набрать фотографии товаров с сайта")
    media.add_argument("--listing", action="append", default=[],
                       help="адрес страницы списка (можно указать несколько)")
    media.add_argument("--cards", default="all",
                       help="сколько карточек обойти: число или all (по умолчанию all), "
                            "0 — не обходить")
    media.add_argument("--in-stock", action="store_true",
                       help="только то, что есть в наличии")
    media.add_argument("--sync", action="store_true",
                       help="только перелить накопленное в базу знаний, без обращений к сайту")

    dialogs = sub.add_parser("dialogs", help="показать записанные диалоги")
    dialogs.add_argument("--last", type=int, default=10, help="сколько последних диалогов")
    dialogs.add_argument("--channel", help="telegram | web | max")
    dialogs.add_argument("--export", help="выгрузить в файл .md для работы над промптами")

    args = parser.parse_args()

    if args.command == "ingest":
        import json
        from dataclasses import asdict

        from ingest.build_kb import build

        report = build(Path(args.source), Path(args.out))
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))

    elif args.command == "norms":
        from catalog.repository import load_products
        from ingest.norm_registry import DEFAULT_REGISTRY, build

        known = {p.sku_1c for p in load_products()}
        report = build(Path(args.source), known)
        print(
            f"строк с кодом 1С: {report.lines_with_code}\n"
            f"пунктов приказа:  {report.item_codes}\n"
            f"кодов 1С:         {report.sku_codes}\n"
            f"сошлось с выгрузкой: {report.matched} ({report.match_rate:.1%})\n"
            f"нет в выгрузке:      {len(report.unmatched)}"
        )
        if report.unmatched and args.show_unmatched:
            print("\nнет в текущей выгрузке 1С (снято с продажи или переименовано):")
            for line in report.unmatched[: args.show_unmatched]:
                print("   ", line)
            if len(report.unmatched) > args.show_unmatched:
                print(f"    … ещё {len(report.unmatched) - args.show_unmatched}")
        print(f"\nреестр сохранён: {DEFAULT_REGISTRY}")
        print("Дальше: python run.py ingest --source <выгрузка>.xlsx")

    elif args.command == "widget":
        from web.app import main as run_widget

        run_widget()

    elif args.command == "telegram":
        import asyncio

        from adapters.telegram.bot import main as run_bot
        from adapters.telegram.bot import use_compatible_event_loop

        use_compatible_event_loop()
        asyncio.run(run_bot())

    elif args.command == "llm":
        from agent.diagnostics import report
        from agent.providers import build_router
        from core.config import Settings

        settings = Settings.from_env()
        print(f"LLM_PROVIDER={settings.llm_provider}")
        print(report(build_router(settings).clients))

    elif args.command == "media":
        _collect_media(args)

    elif args.command == "dialogs":
        from core.config import Settings
        from observability.dialog_log import read_dialogs

        settings = Settings.from_env()
        sessions = read_dialogs(Path(settings.dialog_log_path), limit_sessions=args.last)
        if not sessions:
            print(f"Диалогов пока нет: {settings.dialog_log_path}")
            return

        lines = _format_dialogs(sessions, channel=args.channel)
        text = "\n".join(lines)
        if args.export:
            Path(args.export).write_text(text, encoding="utf-8")
            print(f"Выгружено {len(sessions)} диалогов в {args.export}")
        else:
            print(text)

    elif args.command == "search":
        from catalog.repository import load_index
        from catalog.search import SearchQuery
        from core.ui import price_text, stock_text

        index = load_index()
        for hit in index.search(SearchQuery(text=args.query, limit=args.limit)):
            product = hit.product
            print(f"[{hit.reason}] {product.name}")
            print(f"    {price_text(product.price)} · {stock_text(product)}")
            if hit.citation():
                print(f"    {hit.citation()}")


def _collect_media(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Сбор фотографий с сайта и запись их в базу знаний.

    Обход длинный — тысячи карточек по одной в секунду, — поэтому он прерываемый:
    что успели собрать, то и попадает в `products.jsonl`. Повторный запуск
    продолжает с места остановки, потому что уже собранное лежит в кэше.
    """
    from core.app import build_engine
    from core.config import Settings
    from media.sync import sync_to_kb

    settings = Settings.from_env()
    engine = build_engine(settings)

    if args.sync:
        print("Перелито в базу знаний:", sync_to_kb(engine.storage, settings.kb_path))
        return

    products = engine.index.products
    if args.in_stock:
        products = [p for p in products if p.available]

    for url in args.listing:
        saved = engine.media.warm_up_from_listing(url, products)
        print(f"{url}: превью получено для {saved} товаров")

    limit = len(products) if args.cards == "all" else int(args.cards)
    queue = products[:limit]
    if queue:
        _walk_cards(engine.media, queue)

    print("в кэше:", engine.storage.media_stats())
    print("перелито в базу знаний:", sync_to_kb(engine.storage, settings.kb_path))


def _walk_cards(media, queue: list) -> None:  # noqa: ANN001 — media/service.py
    """Обход карточек с честным прогрессом.

    Скорость плавает на три порядка: то, что уже в кэше, идёт мгновенно, а одна
    недоступная страница стоит минуту. Поэтому прогресс печатается по времени,
    а не по числу товаров — иначе после быстрого куска наступает тишина на час,
    и обход выглядит зависшим.
    """
    import time

    from core.ui import plural
    from media.service import MediaService

    # Сайт может быть недоступен целиком. Молча перебирать оставшиеся тысячи
    # позиций по минуте на каждую — сутки впустую, поэтому останавливаемся.
    give_up_after = 3
    report_every = 15.0  # секунд

    started = last_report = time.monotonic()
    done = failed = 0
    stopped = ""
    announced = False

    try:
        for number, product in enumerate(queue, 1):
            if media.images_for(product):
                done += 1
            else:
                failed += 1

            if isinstance(media, MediaService) and media.consecutive_errors >= give_up_after:
                pages = plural(give_up_after, "страница", "страницы", "страниц")
                stopped = (
                    f"\nСайт не отвечает: {give_up_after} {pages} подряд не загрузились.\n"
                    "Обход остановлен, собранное сохранено. Продолжить можно той же\n"
                    "командой оттуда, где vdm.ru открывается."
                )
                break

            now = time.monotonic()
            fetches = getattr(media, "fetches", 0)
            if fetches and not announced:
                # Первое обращение к сайту стоит отметить сразу: до него обход
                # летит по кэшу, и без этой строки переход на медленный режим
                # выглядит как зависание.
                announced = True
                print(
                    f"  {number - 1} карточек взято из кэша, дальше загрузка с сайта",
                    flush=True,
                )
            if now - last_report >= report_every or number == len(queue):
                last_report = now
                line = f"  {number}/{len(queue)} · с фото {done} · без {failed}"
                # Оценку строим по реальным загрузкам: чтение кэша к оставшейся
                # работе отношения не имеет.
                if fetches:
                    per_fetch = (now - started) / fetches
                    left = (len(queue) - number) * per_fetch
                    line += f" · загружено {fetches} · осталось ~{_duration(left)}"
                else:
                    line += " · всё из кэша"
                print(line, flush=True)
    except KeyboardInterrupt:
        stopped = "\nОстановлено. Собранное сохраняем."

    if stopped:
        print(stopped)
    print(f"карточек обработано: {done + failed}, с фото: {done}, без: {failed}")


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} с"
    if seconds < 5400:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds / 3600:.1f} ч"


def _format_dialogs(sessions: dict, channel: str | None) -> list[str]:
    """Диалоги в читаемом виде: реплика пользователя, что ответил бот, что предложил."""
    lines: list[str] = []
    for session, turns in sessions.items():
        if channel and turns[0]["channel"] != channel:
            continue
        head = f"## {session} · {turns[0]['channel']} · {turns[0]['ts'][:16]} · реплик: {len(turns)}"
        lines += ["", head, ""]
        for turn in turns:
            marker = "→" if turn["kind"] == "text" else "⌨"
            lines.append(f"{marker} {turn['in']}   [{turn['mode']}, {turn['latency_ms']} мс]")
            for out in turn["out"]:
                if out["type"] == "text":
                    lines.append(f"   бот: {out['text']}")
                elif out["type"] == "list":
                    lines.append(f"   бот: {out['title']}")
                    for item in out["items"]:
                        norm = f" · {item['norm']}" if item.get("norm") else ""
                        lines.append(f"      - {item['name']} · {item['price']} ₽{norm}")
                elif out["type"] == "card":
                    lines.append(f"   бот: карточка {out['name']} · {out['price']} ₽")
                elif out["type"] == "order":
                    lines.append(f"   бот: заказ на {out['total']} ₽, позиций {out['positions']}")
            lines.append("")
    return lines


if __name__ == "__main__":
    main()
