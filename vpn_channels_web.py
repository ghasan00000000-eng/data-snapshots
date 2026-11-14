#!/usr/bin/env python3
"""
vpn_channels_web.py — то же, что vpn_channels.py, но БЕЗ api_id / api_hash и без входа в аккаунт.

Как работает: читает публичные веб-превью каналов (https://t.me/s/<канал>), берёт оттуда посты,
даты и просмотры и считает ту же метрику «ежедневности» (сколько дней из 30 у канала был пост
с >= 200 просмотров). Дополнительно обходит ссылки и упоминания других каналов внутри постов,
так что от 5–10 известных VPN-каналов можно дойти до новых.

Ограничения (честно):
  * Нет поиска по ключевым словам — нужны затравки (seeds.txt), дальше скрипт идёт по ссылкам.
  * Работает только для каналов с публичным веб-превью (у некоторых оно закрыто — такие пропускаются).
  * Это разбор HTML-страницы: если Telegram изменит разметку, парсер придётся поправить.

Установка:   pip install beautifulsoup4
Положите файл В ТУ ЖЕ ПАПКУ, где лежит vpn_channels.py (оттуда берутся формулы метрик).

Запуск:
  python vpn_channels_web.py selftest
  python vpn_channels_web.py run --seeds seeds.txt --expand 1
  python vpn_channels_web.py run --seeds seeds.txt --threshold 200 --window 30 --min-days-share 0.7
"""
import argparse
import csv
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from vpn_channels import clean_username, compute_metrics, make_flags, read_lines, vpn_info
except ImportError:
    sys.exit("Положите vpn_channels_web.py в ту же папку, где лежит vpn_channels.py")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Нужен пакет beautifulsoup4:  pip install beautifulsoup4")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NAME_RE = r"[A-Za-z][A-Za-z0-9_]{4,31}"
LINK_RE = re.compile(r"^https?://(?:t|telegram)\.me/(" + NAME_RE + r")(?:[/?#]|$)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w/])@(" + NAME_RE + r")")
SERVICE_NAMES = {
    "s", "c", "joinchat", "addlist", "addstickers", "addemoji", "share", "proxy", "socks", "iv",
    "contact", "boost", "login", "setlanguage", "telegram", "durov", "username", "bg", "invoice",
    "giftcode", "confirmphone", "addtheme", "message", "settings",
}


# ----------------------------------------------------------------------------
# Загрузка страниц
# ----------------------------------------------------------------------------


def fetch(url, timeout=25, retries=3):
    """Возвращает (итоговый_url, html) или (None, None)."""
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"})
            with urlopen(req, timeout=timeout) as r:
                return r.geturl(), r.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code == 429:
                try:
                    wait = int(e.headers.get("Retry-After", "30"))
                except ValueError:
                    wait = 30
                print(f"    429 — жду {min(wait, 120)} c...", file=sys.stderr)
                time.sleep(min(wait, 120))
                continue
            if e.code in (400, 403, 404):
                return None, None
            time.sleep(3 * (attempt + 1))
        except (URLError, TimeoutError, OSError):
            time.sleep(3 * (attempt + 1))
    return None, None


# ----------------------------------------------------------------------------
# Разбор страницы t.me/s/<канал>
# ----------------------------------------------------------------------------


def parse_count(s):
    """'512' -> 512, '1.2K' -> 1200, '3.34M' -> 3340000, '12 345' -> 12345."""
    if not s:
        return 0
    s = s.strip().replace("\xa0", "").replace(" ", "").upper()
    m = re.fullmatch(r"([\d.,]+)([KMКМ]?)", s)
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    mult = {"": 1, "K": 1e3, "К": 1e3, "M": 1e6, "М": 1e6}[m.group(2)]
    return int(round(num * mult))


def _usernames_from(href=None, text=None):
    found = set()
    if href:
        m = LINK_RE.match(href)
        if m:
            found.add(m.group(1))
    if text:
        found.update(MENTION_RE.findall(text))
    return found


def parse_page(html):
    """
    Возвращает dict:
      is_channel  — есть ли у страницы шапка канала
      title, description, subscribers
      messages    — список (post_id, datetime|None, views|None)
      links       — множество username'ов, упомянутых в постах (каналы, боты, люди)
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {"is_channel": False, "title": "", "description": "", "subscribers": 0,
           "messages": [], "links": set()}

    info = soup.select_one(".tgme_channel_info")
    if info is None:
        return out
    out["is_channel"] = True
    t = info.select_one(".tgme_channel_info_header_title")
    out["title"] = t.get_text(" ", strip=True) if t else ""
    d = info.select_one(".tgme_channel_info_description")
    out["description"] = d.get_text(" ", strip=True) if d else ""
    for c in info.select(".tgme_channel_info_counter"):
        typ = c.select_one(".counter_type")
        val = c.select_one(".counter_value")
        if typ and val and typ.get_text(strip=True).lower().startswith(("subscriber", "подписчик")):
            out["subscribers"] = parse_count(val.get_text(strip=True))
    # ссылки в описании тоже полезны
    if d:
        for a in d.select("a[href]"):
            out["links"] |= _usernames_from(href=a["href"])
        out["links"] |= _usernames_from(text=d.get_text(" "))

    for msg in soup.select("div.tgme_widget_message"):
        dp = msg.get("data-post", "")
        if "/" not in dp:
            continue
        try:
            pid = int(dp.split("/")[1])
        except ValueError:
            continue
        views_el = msg.select_one("span.tgme_widget_message_views")
        views = parse_count(views_el.get_text(strip=True)) if views_el else None
        time_el = msg.select_one("a.tgme_widget_message_date time") or msg.select_one("time")
        dt = None
        if time_el and time_el.get("datetime"):
            try:
                dt = datetime.fromisoformat(time_el["datetime"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
        out["messages"].append((pid, dt, views))
        for a in msg.select("a[href]"):
            out["links"] |= _usernames_from(href=a["href"])
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el:
            out["links"] |= _usernames_from(text=text_el.get_text(" "))
    return out


def fetch_history(name, window_days, max_pages, delay, fetcher=fetch):
    """Листает историю канала назад, пока не выйдет за окно. Возвращает dict или None."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days + 2)
    before = None
    head = None
    posts, links = [], set()
    for page in range(max_pages):
        url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
        final, html = fetcher(url)
        if not html or not final or "/s/" not in final:
            return None  # не канал, нет превью или ошибка сети
        parsed = parse_page(html)
        if page == 0:
            if not parsed["is_channel"]:
                return None
            head = {k: parsed[k] for k in ("title", "description", "subscribers")}
        msgs = parsed["messages"]
        if not msgs:
            break
        links |= parsed["links"]
        for _pid, dt, views in msgs:
            if dt is not None and views is not None:
                posts.append((dt, views))
        new_before = min(pid for pid, _dt, _v in msgs)
        dated = [dt for _pid, dt, _v in msgs if dt is not None]
        if before is not None and new_before >= before:
            break
        before = new_before
        if dated and min(dated) < cutoff:
            break
        time.sleep(delay)
    links = {l for l in links if l.lower() != name.lower()}   # ссылки на сам канал не нужны
    return {"head": head, "posts": posts, "links": links}


# ----------------------------------------------------------------------------
# Основной запуск
# ----------------------------------------------------------------------------


def plausible(name):
    n = name.lower()
    return n not in SERVICE_NAMES and 5 <= len(n) <= 32


def seed_name(line):
    """'@name', 't.me/name', 'https://t.me/s/name/123' -> 'name'."""
    line = re.sub(r"^(https?://)?(t\.me|telegram\.me)/s/", "https://t.me/", line.strip(), flags=re.IGNORECASE)
    return clean_username(line)


def run(args):
    tz = timezone(timedelta(hours=args.utc_offset))
    seeds = [seed_name(l) for l in read_lines(args.seeds)] if args.seeds else []
    seeds = [s for s in seeds if s]
    if not seeds:
        sys.exit("Нужен файл с затравками: --seeds seeds.txt (по одному @каналу или ссылке в строке)")

    queue = deque((s, 0) for s in seeds)
    seen = {s.lower() for s in seeds}
    rows = []
    skipped = 0
    try:
        while queue and len(rows) < args.max_channels:
            name, depth = queue.popleft()
            if name.lower().endswith("bot"):
                continue
            res = fetch_history(name, args.window, args.max_pages, args.delay)
            if res is None:
                skipped += 1
                print(f"  @{name}: нет публичного превью / не канал — пропуск")
                time.sleep(args.delay)
                continue
            head, posts, links = res["head"], res["posts"], res["links"]
            now = datetime.now(timezone.utc)
            m = compute_metrics(posts, now, args.window, args.threshold, tz)
            hits, has_bot_about = vpn_info(head["title"], name, head["description"])
            bots = sorted(l for l in links if l.lower().endswith("bot"))
            row = {
                "username": f"@{name}",
                "title": head["title"],
                "subscribers": head["subscribers"],
                "passes": "YES" if m["days_ok_share"] >= args.min_days_share else "",
                **m,
                "vpn_keyword_hits": hits,
                "bot_link_in_about": "yes" if has_bot_about else "",
                "bots_in_posts": len(bots),
                "found_at_depth": depth,
                "flags": make_flags(m, head["subscribers"]),
                "about": head["description"][:300],
                "link": f"https://t.me/{name}",
            }
            rows.append(row)
            mark = "OK " if row["passes"] else "-- "
            print(f"  [{len(rows)}] {mark}@{name}: дней>={args.threshold}: {m['days_ok']}/{args.window}, "
                  f"медиана {m['median_views']}, подписчиков {head['subscribers']}")

            # расширяемся только от каналов про VPN (или от затравок)
            if depth < args.expand and (depth == 0 or hits > 0 or bots):
                new = 0
                for ln in sorted(links):
                    if ln.lower() not in seen and plausible(ln) and not ln.lower().endswith("bot"):
                        seen.add(ln.lower())
                        queue.append((ln, depth + 1))
                        new += 1
                if new:
                    print(f"      +{new} новых кандидатов из ссылок (очередь: {len(queue)})")
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nПрервано — сохраняю то, что успели собрать...")

    rows.sort(key=lambda r: (r["passes"] == "YES", r["days_ok_share"], r["median_views"]), reverse=True)
    if args.only_vpn:
        rows = [r for r in rows if r["vpn_keyword_hits"] > 0 or r["bots_in_posts"] > 0]
    if rows:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    ok = sum(1 for r in rows if r["passes"] == "YES")
    print(f"\nГотово: {len(rows)} каналов в {args.out}, проходят порог: {ok}, пропущено: {skipped}")


# ----------------------------------------------------------------------------
# Самопроверка на синтетических страницах (разметка — как у t.me/s)
# ----------------------------------------------------------------------------


def _header(name, title, subs, about):
    return (f'<div class="tgme_channel_info"><div class="tgme_channel_info_header">'
            f'<div class="tgme_channel_info_header_title"><span dir="auto">{title}</span></div>'
            f'<div class="tgme_channel_info_header_username"><a href="https://t.me/{name}">@{name}</a></div></div>'
            f'<div class="tgme_channel_info_counters"><div class="tgme_channel_info_counter">'
            f'<span class="counter_value">{subs}</span> <span class="counter_type">subscribers</span></div></div>'
            f'<div class="tgme_channel_info_description">{about}</div></div>')


def _msg(name, pid, dt, views, text=""):
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{name}/{pid}">'
            f'<div class="tgme_widget_message_bubble">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
            f'<div class="tgme_widget_message_footer compact js-message_footer">'
            f'<div class="tgme_widget_message_info short js-message_info">'
            f'<span class="tgme_widget_message_views">{views}</span><span class="copyonly"> views</span>'
            f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/{name}/{pid}">'
            f'<time datetime="{dt.isoformat()}" class="time">12:00</time></a></span>'
            f'</div></div></div></div></div>')


def selftest():
    # --- разбор чисел
    assert parse_count("512") == 512 and parse_count("1.2K") == 1200
    assert parse_count("3.34M") == 3340000 and parse_count("12 345") == 12345 and parse_count("") == 0
    for raw in ("@MyVpn", "https://t.me/MyVpn", "t.me/s/MyVpn", "https://t.me/s/MyVpn/123?x=1"):
        assert seed_name(raw) == "MyVpn", raw
    assert plausible("MyVpn") and not plausible("joinchat") and not plausible("abc")

    # --- синтетический канал: 2 поста в день по ~300 просмотров за 60 дней, 20 постов на страницу
    now = datetime.now(timezone.utc)
    name = "demo_vpn_channel"
    all_msgs = []
    pid = 0
    for d in range(60, 0, -1):
        for h in (9, 18):
            pid += 1
            dt = (now - timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
            views = "1.2K" if pid % 7 == 0 else str(280 + pid % 40)
            text = "Наш бот: <a href=\"https://t.me/demo_vpn_bot\">@demo_vpn_bot</a>"
            if pid == 118:
                text += ' партнёры <a href="https://t.me/other_vpn_news">канал</a> и @mention_vpn_ch'
            all_msgs.append((pid, dt, views, text))

    calls = []

    def fake_fetch(url):
        calls.append(url)
        m = re.search(r"\?before=(\d+)", url)
        before = int(m.group(1)) if m else 10 ** 9
        page = [x for x in all_msgs if x[0] < before][-20:]
        body = "".join(_msg(name, p, dt, v, t) for p, dt, v, t in page)
        if "/s/nochannel" in url:
            return "https://t.me/nochannel", "<html><body>user</body></html>"
        return url, f"<html><body>{_header(name, 'Demo VPN', '12.3K', 'Быстрый VPN, бот @demo_vpn_bot')}{body}</body></html>"

    res = fetch_history(name, 30, 10, 0, fetcher=fake_fetch)
    n_pages = len(calls)
    assert res is not None
    assert res["head"]["subscribers"] == 12300 and res["head"]["title"] == "Demo VPN"
    assert n_pages >= 3, calls                         # листание страниц работает (30 дней * 2 = 60 постов)
    assert any("before=" in c for c in calls[1:]), calls
    assert n_pages <= 5, calls                         # и останавливается, выйдя за окно
    assert {"demo_vpn_bot", "other_vpn_news", "mention_vpn_ch"} <= res["links"], res["links"]
    assert name not in res["links"], res["links"]
    m = compute_metrics(res["posts"], now, 30, 200, timezone(timedelta(hours=3)))
    assert m["days_ok_share"] >= 0.95, m
    assert fake_fetch("https://t.me/s/nochannel")[0] == "https://t.me/nochannel"
    assert fetch_history("nochannel", 30, 3, 0, fetcher=fake_fetch) is None   # не канал -> None

    print("selftest OK")
    print(f"  страниц запрошено: {n_pages}, постов в окне: {m['posts_in_window']}, "
          f"days_ok={m['days_ok']}/30, median={m['median_views']}, ссылки: {sorted(res['links'])}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    r = sub.add_parser("run")
    r.add_argument("--seeds", help="файл со списком @username / ссылок, по одному в строке")
    r.add_argument("--expand", type=int, default=1, help="глубина обхода ссылок (0 = только затравки)")
    r.add_argument("--threshold", type=int, default=200, help="минимум просмотров у лучшего поста дня")
    r.add_argument("--window", type=int, default=30, help="окно в днях")
    r.add_argument("--min-days-share", type=float, default=0.7, help="доля дней окна с выполненным порогом")
    r.add_argument("--max-pages", type=int, default=12, help="максимум страниц превью на канал (~20 постов на странице)")
    r.add_argument("--max-channels", type=int, default=300, help="остановиться после N замеренных каналов")
    r.add_argument("--utc-offset", type=int, default=3, help="часовой пояс для границ дней (МСК = 3)")
    r.add_argument("--delay", type=float, default=1.2, help="пауза между запросами, сек")
    r.add_argument("--only-vpn", action="store_true", help="в CSV только каналы с VPN-словами или ботом в постах")
    r.add_argument("--out", default="vpn_channels_web.csv")
    args = p.parse_args()
    if args.cmd == "selftest":
        selftest()
    else:
        run(args)


if __name__ == "__main__":
    main()
