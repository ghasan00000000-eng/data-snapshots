#!/usr/bin/env python3
"""
vpn_channels.py — поиск Telegram-каналов VPN-сервисов, у которых просмотры идут ЕЖЕДНЕВНО.

Что делает:
  1. Находит кандидатов: поиск Telegram по ключевым словам + (опционально) «похожие каналы»
     от тех, кто уже похож на VPN-сервис + ваш список затравок (seeds.txt).
  2. Для каждого канала берёт посты за последние N дней (по умолчанию 30) и считает,
     в скольких днях был хотя бы один пост с >= THRESHOLD просмотров (по умолчанию 200).
     Один вирусный пост раз в месяц такой проверки не проходит.
  3. Пишет CSV (открывается в Excel / Google Sheets), отсортированный по «ежедневности».

Установка:   pip install telethon
Ключи:       https://my.telegram.org -> API development tools -> api_id и api_hash
             (лучше использовать отдельный аккаунт, а не основной)

Запуск:
  export TG_API_ID=123456 TG_API_HASH=abcdef...        # Windows: set вместо export
  python vpn_channels.py run --expand 1
  python vpn_channels.py run --seeds seeds.txt --threshold 200 --window 30 --min-days-share 0.7
  python vpn_channels.py selftest                       # проверка логики метрик без Telegram

Как читать метрики — см. функцию compute_metrics ниже.
"""
import argparse
import asyncio
import csv
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# 1. Метрики (чистые функции — не требуют Telegram, покрыты selftest)
# ----------------------------------------------------------------------------


def compute_metrics(posts, now, window_days=30, threshold=200, tz=timezone.utc, min_age_hours=24):
    """
    posts: список (datetime с tz, views:int) — посты канала.
    now:   текущий момент (datetime с tz).

    Окно = window_days ПОЛНЫХ дней, заканчивающихся вчера (сегодняшний день неполный и не считается).

    days_ok        — сколько дней окна, в которые САМЫЙ просматриваемый пост дня набрал >= threshold.
                     Это нижняя оценка «сколько разных людей зашло в этот день»: один и тот же человек
                     не может прочитать пост, которого не открывал, поэтому уникальных читателей
                     за день не меньше, чем просмотров у лучшего поста. Это и есть «минимум 200 в день».
    days_ok_share  — days_ok / window_days (0..1). Главная метрика «ежедневности».
    median_views   — медиана просмотров поста (только посты старше min_age_hours, чтобы просмотры устоялись).
    share_posts_ge — доля постов с >= threshold просмотров.
    avg_daily_sum  — суммарные просмотры всех постов окна / window_days (верхняя оценка).
    spike_ratio    — max / median: большое значение = канал живёт разовыми вспышками.
    """
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=window_days)

    by_day = {}
    mature = []
    for d, v in posts:
        if v is None:
            continue
        day = d.astimezone(tz).date()
        if day < first_day or day >= today:
            continue
        by_day.setdefault(day, []).append(v)
        if now - d >= timedelta(hours=min_age_hours):
            mature.append(v)

    all_views = [v for lst in by_day.values() for v in lst]
    med = statistics.median(mature) if mature else 0
    days_ok = sum(1 for lst in by_day.values() if max(lst) >= threshold)
    return {
        "posts_in_window": len(all_views),
        "active_days": len(by_day),
        "days_ok": days_ok,
        "days_ok_share": round(days_ok / window_days, 3),
        "median_views": int(med),
        "share_posts_ge": round(sum(1 for v in mature if v >= threshold) / len(mature), 3) if mature else 0,
        "avg_daily_sum": int(sum(all_views) / window_days),
        "max_views": max(mature) if mature else 0,
        "spike_ratio": round(max(mature) / med, 1) if med > 0 else 0,
    }


def make_flags(m, subscribers):
    flags = []
    if m["spike_ratio"] > 10:
        flags.append("spiky")                      # живёт вспышками
    if subscribers:
        err = m["median_views"] / subscribers
        if err > 1.0:
            flags.append("views>subs (проверить накрутку/рекламу)")
        elif err < 0.03:
            flags.append("err<3% (мёртвая аудитория)")
    if m["posts_in_window"] < 10:
        flags.append("мало постов")
    return "; ".join(flags)


# ----------------------------------------------------------------------------
# 2. Определение «это канал VPN-сервиса»
# ----------------------------------------------------------------------------

VPN_RE = re.compile(
    r"vpn|впн|vless|vmess|trojan|shadowsocks|wireguard|openvpn|xray|outline|"
    r"обход блокировок|обход замедлен|без блокировок|прокси|proxy",
    re.IGNORECASE,
)
BOT_RE = re.compile(r"t\.me/\w*bot\b|@\w*bot\b", re.IGNORECASE)

DEFAULT_QUERIES = [
    "vpn", "впн", "vpn бот", "впн бот", "vpn сервис", "впн сервис", "vpn россия",
    "лучший vpn", "быстрый vpn", "бесплатный vpn", "vpn для телеграм", "vpn для youtube",
    "обход блокировок", "обход замедления", "vless", "vless vpn", "shadowsocks",
    "wireguard", "outline vpn", "xray", "vmess", "прокси telegram", "vpn подписка",
    "vpn ключ", "vpn для iphone", "vpn для android", "vpn тарифы", "ваш vpn",
]


def vpn_info(title, username, about):
    text = f"{title or ''} {username or ''} {about or ''}"
    hits = len(VPN_RE.findall(text))
    return hits, bool(BOT_RE.search(about or ""))


def read_lines(path):
    """Читает текстовый файл со списком (по строке на элемент).
    Понимает UTF-8 (в том числе с BOM, как сохраняет старый Блокнот Windows) и кодировку cp1251."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    return [l.strip() for l in text.splitlines() if l.strip()]


def clean_username(s):
    """'@name', 'https://t.me/name', 't.me/name/123?x=1' -> 'name'."""
    s = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", s.strip(), flags=re.IGNORECASE)
    return s.lstrip("@").split("/")[0].split("?")[0]


# ----------------------------------------------------------------------------
# 3. Работа с Telegram
# ----------------------------------------------------------------------------


async def call(client, request, errors, retries=2):
    """Вызов API с ожиданием FloodWait."""
    for attempt in range(retries + 1):
        try:
            return await client(request)
        except errors.FloodWaitError as e:
            wait = e.seconds + 2
            print(f"    FloodWait {e.seconds}s — жду...", file=sys.stderr)
            if wait > 900 or attempt == retries:
                raise
            await asyncio.sleep(wait)


async def discover(client, queries, expand_depth, seeds, delay):
    from telethon import errors, types
    from telethon.tl import functions

    found = {}  # channel_id -> Channel

    def add(chats):
        n = 0
        for ch in chats:
            if (isinstance(ch, types.Channel) and ch.broadcast and ch.username
                    and not getattr(ch, "scam", False) and not getattr(ch, "fake", False)
                    and ch.id not in found):
                found[ch.id] = ch
                n += 1
        return n

    for s in seeds:
        try:
            add([await client.get_entity(s)])
        except Exception as e:  # noqa: BLE001
            print(f"  seed {s}: не найден ({e.__class__.__name__})")

    for q in queries:
        try:
            res = await call(client, functions.contacts.SearchRequest(q=q, limit=100), errors)
            n = add(res.chats)
            print(f"  поиск «{q}»: +{n} новых (всего {len(found)})")
        except Exception as e:  # noqa: BLE001
            print(f"  поиск «{q}»: ошибка {e.__class__.__name__}")
        await asyncio.sleep(delay)

    # «Похожие каналы» — только от тех, кто по описанию уже похож на VPN
    frontier = list(found.values())
    for depth in range(expand_depth):
        new = {}
        for ch in frontier:
            title_hits, _ = vpn_info(ch.title, ch.username, "")
            if title_hits == 0:
                continue
            try:
                res = await call(client, functions.channels.GetChannelRecommendationsRequest(channel=ch), errors)
            except Exception as e:  # noqa: BLE001
                print(f"  похожие для @{ch.username}: {e.__class__.__name__}")
                await asyncio.sleep(delay)
                continue
            before = set(found)
            add(res.chats)
            for cid in set(found) - before:
                new[cid] = found[cid]
            await asyncio.sleep(delay)
        print(f"  расширение, уровень {depth + 1}: +{len(new)} (всего {len(found)})")
        frontier = list(new.values())
        if not frontier:
            break
    return list(found.values())


async def measure(client, ch, args, tz):
    from telethon import errors
    from telethon.tl import functions

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.window + 2)
    full = await call(client, functions.channels.GetFullChannelRequest(channel=ch), errors)
    about = full.full_chat.about or ""
    subs = full.full_chat.participants_count or 0

    posts = []
    async for m in client.iter_messages(ch, limit=args.max_posts):
        if m.date < cutoff:
            break
        if m.views is not None:
            posts.append((m.date, m.views))

    m = compute_metrics(posts, now, args.window, args.threshold, tz)
    hits, has_bot = vpn_info(ch.title, ch.username, about)
    row = {
        "username": f"@{ch.username}",
        "title": ch.title,
        "subscribers": subs,
        "passes": "YES" if m["days_ok_share"] >= args.min_days_share else "",
        **m,
        "vpn_keyword_hits": hits,
        "bot_link_in_about": "yes" if has_bot else "",
        "flags": make_flags(m, subs),
        "about": about.replace("\n", " ")[:300],
        "link": f"https://t.me/{ch.username}",
    }
    return row


async def run(args):
    from telethon import TelegramClient

    api_id = args.api_id or os.environ.get("TG_API_ID")
    api_hash = args.api_hash or os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Нужны api_id и api_hash: переменные TG_API_ID / TG_API_HASH или --api-id / --api-hash")

    tz = timezone(timedelta(hours=args.utc_offset))
    queries = DEFAULT_QUERIES
    if args.queries_file:
        queries = read_lines(args.queries_file)
    seeds = []
    if args.seeds:
        seeds = [clean_username(l) for l in read_lines(args.seeds)]

    async with TelegramClient(args.session, int(api_id), api_hash) as client:
        print("== Поиск кандидатов ==")
        channels = await discover(client, queries, args.expand, seeds, args.delay)
        print(f"Кандидатов: {len(channels)}")

        print("== Замер просмотров ==")
        rows = []
        for i, ch in enumerate(channels, 1):
            try:
                row = await measure(client, ch, args, tz)
                rows.append(row)
                mark = "OK " if row["passes"] else "-- "
                print(f"  [{i}/{len(channels)}] {mark}@{ch.username}: дней>={args.threshold}: "
                      f"{row['days_ok']}/{args.window}, медиана {row['median_views']}")
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(channels)}] @{ch.username}: пропуск ({e.__class__.__name__})")
            await asyncio.sleep(args.delay)

    rows.sort(key=lambda r: (r["passes"] == "YES", r["days_ok_share"], r["median_views"]), reverse=True)
    if args.only_vpn:
        rows = [r for r in rows if r["vpn_keyword_hits"] > 0]
    if rows:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    ok = sum(1 for r in rows if r["passes"] == "YES")
    print(f"\nГотово: {len(rows)} каналов в {args.out}, из них проходят порог: {ok}")


# ----------------------------------------------------------------------------
# 4. Самопроверка логики метрик на синтетических каналах
# ----------------------------------------------------------------------------


def selftest():
    now = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
    msk = timezone(timedelta(hours=3))

    def at(days_ago, hour=12):
        base = (now.astimezone(msk) - timedelta(days=days_ago)).replace(hour=hour, minute=0)
        return base

    # A: два поста в день, по ~300 просмотров — живой канал
    A = [(at(d, h), 300 + d) for d in range(0, 40) for h in (10, 18)]
    # B: один вирусный пост на 10 000, остальное ~20 просмотров
    B = [(at(d), 20) for d in range(0, 40)]
    B[15] = (B[15][0], 10_000)
    # C: пост раз в 3 дня по 500 просмотров
    C = [(at(d), 500) for d in range(0, 40, 3)]
    # D: ежедневно по 150 — не дотягивает до 200
    D = [(at(d), 150) for d in range(0, 40)]

    mA = compute_metrics(A, now, 30, 200, msk)
    mB = compute_metrics(B, now, 30, 200, msk)
    mC = compute_metrics(C, now, 30, 200, msk)
    mD = compute_metrics(D, now, 30, 200, msk)

    assert mA["days_ok_share"] >= 0.95, mA          # проходит
    assert mB["days_ok_share"] <= 0.05, mB          # вирусный пост ≠ ежедневные просмотры
    assert mB["spike_ratio"] > 10 and "spiky" in make_flags(mB, 5000), mB
    assert 0.3 <= mC["days_ok_share"] <= 0.4, mC    # ~10 дней из 30
    assert mD["days_ok_share"] == 0, mD
    # сегодняшний (неполный) день не учитывается
    assert compute_metrics([(at(0), 999)], now, 30, 200, msk)["posts_in_window"] == 0
    # пост старше окна не учитывается
    assert compute_metrics([(at(45), 999)], now, 30, 200, msk)["posts_in_window"] == 0
    # распознавание VPN-сервиса
    assert vpn_info("Panda VPN | новости", "pandavpn", "Наш бот: @MyVpn_bot")[0] >= 2
    assert vpn_info("Рецепты", "recipes", "")[0] == 0
    assert vpn_info("x", "y", "заходи t.me/some_bot")[1] is True
    # имена каналов в любом привычном виде
    assert clean_username("@MyVpn") == "MyVpn"
    assert clean_username("https://t.me/MyVpn") == "MyVpn"
    assert clean_username("t.me/MyVpn/123?single") == "MyVpn"
    # чтение файлов со списками: UTF-8, UTF-8 с BOM (старый Блокнот), cp1251
    import tempfile
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as tf:
            tf.write("vpn\nвпн бот\n\nvless\n".encode(enc))
        assert read_lines(tf.name) == ["vpn", "впн бот", "vless"], enc
        os.unlink(tf.name)

    print("selftest OK")
    for name, m in (("A ежедневный", mA), ("B вирусный пост", mB), ("C раз в 3 дня", mC), ("D по 150", mD)):
        print(f"  {name:16} days_ok={m['days_ok']:>2}/30  share={m['days_ok_share']:<5} "
              f"median={m['median_views']:<5} spike={m['spike_ratio']}")


# ----------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    r = sub.add_parser("run")
    r.add_argument("--api-id")
    r.add_argument("--api-hash")
    r.add_argument("--session", default="vpn_scan")
    r.add_argument("--seeds", help="файл со списком @username / ссылок, по одному в строке")
    r.add_argument("--queries-file", help="свои ключевые слова для поиска, по одному в строке")
    r.add_argument("--expand", type=int, default=1, help="уровней «похожих каналов» (0 = выкл)")
    r.add_argument("--threshold", type=int, default=200, help="минимум просмотров у лучшего поста дня")
    r.add_argument("--window", type=int, default=30, help="окно в днях")
    r.add_argument("--min-days-share", type=float, default=0.7,
                   help="доля дней окна, в которые порог выполнен (0.7 ≈ 21 день из 30)")
    r.add_argument("--max-posts", type=int, default=400, help="максимум постов на канал")
    r.add_argument("--utc-offset", type=int, default=3, help="часовой пояс для границ дней (МСК = 3)")
    r.add_argument("--delay", type=float, default=1.5, help="пауза между запросами, сек")
    r.add_argument("--only-vpn", action="store_true", help="в CSV только каналы с VPN-словами в названии/описании")
    r.add_argument("--out", default="vpn_channels.csv")
    args = p.parse_args()
    if args.cmd == "selftest":
        selftest()
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
