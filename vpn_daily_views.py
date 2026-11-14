#!/usr/bin/env python3
"""
vpn_daily_views.py — сколько просмотров канал получает В СУТКИ (а не просто «у скольки постов много просмотров»).

Зачем: Telegram отдаёт снаружи только накопленный счётчик просмотров поста («сейчас 383 тыс.»), а не
историю по дням. Поэтому один замер не отличит пост, который набрал всё за первый день, от поста,
который собирает по 10 тыс. просмотров КАЖДЫЙ день (как на графике TGStat для канала Quattro VPN).
TGStat видит дневную динамику, потому что сам регулярно снимает эти счётчики. Этот скрипт делает то же:

  1) snapshot — снимает счётчики просмотров всех недавних постов каждого канала и дописывает в файл.
  2) report   — считает разницу между снимками: прирост просмотров за сутки по ВСЕМ постам
                (и новым, и старым — «длинный хвост»), сравнивает с порогом.

Первый осмысленный результат — после второго снимка (через ~сутки; для быстрой прикидки можно
через 3–6 часов с --min-hours 3, но дневной ритм это искажает). Надёжный вывод «ежедневно» —
после 3+ суточных интервалов.

Положите файл В ТУ ЖЕ ПАПКУ, где лежат vpn_channels.py и vpn_channels_web.py. Нужен: pip install beautifulsoup4

Запуск:
  python vpn_daily_views.py selftest
  python vpn_daily_views.py snapshot --seeds seeds.txt            # раз в сутки, примерно в одно время
  python vpn_daily_views.py snapshot --from-csv vpn_channels_web.csv --only-vpn
  python vpn_daily_views.py report --threshold 200                # результат в daily_views_report.csv
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from vpn_channels import read_lines, vpn_info
    from vpn_channels_web import fetch, parse_page, seed_name
except ImportError:
    sys.exit("Положите vpn_daily_views.py в ту же папку, где лежат vpn_channels.py и vpn_channels_web.py")


# ----------------------------------------------------------------------------
# Снимки
# ----------------------------------------------------------------------------


def fetch_posts(name, depth_days, max_pages, delay, fetcher=fetch):
    """Счётчики просмотров всех постов за последние depth_days дней. Возвращает dict или None."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=depth_days)
    before = None
    head = None
    posts = {}
    for page in range(max_pages):
        url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
        final, html = fetcher(url)
        if not html or not final or "/s/" not in final:
            return None
        parsed = parse_page(html)
        if page == 0:
            if not parsed["is_channel"]:
                return None
            head = {k: parsed[k] for k in ("title", "description", "subscribers")}
        msgs = parsed["messages"]
        if not msgs:
            break
        for pid, dt, views in msgs:
            if dt is not None and views is not None:
                posts[pid] = [dt.astimezone(timezone.utc).isoformat(), views]
        new_before = min(pid for pid, _dt, _v in msgs)
        dated = [dt for _pid, dt, _v in msgs if dt is not None]
        if before is not None and new_before >= before:
            break
        before = new_before
        if dated and min(dated) < cutoff:
            break
        time.sleep(delay)
    return {**head, "posts": posts}


def load_channels(args):
    names = []
    if args.seeds:
        names += [seed_name(l) for l in read_lines(args.seeds)]
    if args.from_csv:
        with open(args.from_csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if args.only_vpn and "vpn_keyword_hits" in row:
                    if int(row.get("vpn_keyword_hits") or 0) == 0 and int(row.get("bots_in_posts") or 0) == 0:
                        continue
                names.append(seed_name(row["username"]))
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen and not n.lower().endswith("bot"):
            seen.add(n.lower())
            out.append(n)
    if not out:
        sys.exit("Нет каналов: укажите --seeds seeds.txt и/или --from-csv файл.csv")
    return out


def cmd_snapshot(args):
    names = load_channels(args)
    print(f"Снимаю счётчики: {len(names)} каналов, глубина {args.depth_days} дн.")
    done = 0
    with open(args.data, "a", encoding="utf-8") as f:
        for i, name in enumerate(names, 1):
            res = fetch_posts(name, args.depth_days, args.max_pages, args.delay)
            if res is None:
                print(f"  [{i}/{len(names)}] @{name}: нет публичного превью / не канал — пропуск")
                time.sleep(args.delay)
                continue
            rec = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ch": name,
                   "title": res["title"], "about": res["description"][:200],
                   "subs": res["subscribers"], "posts": {str(k): v for k, v in res["posts"].items()}}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            done += 1
            print(f"  [{i}/{len(names)}] @{name}: постов {len(res['posts'])}, подписчиков {res['subscribers']}")
            time.sleep(args.delay)
    print(f"Готово: снято {done} каналов -> {args.data}. Повторите через ~сутки в то же время, затем: report")


# ----------------------------------------------------------------------------
# Расчёт просмотров в сутки (чистые функции — проверяются selftest)
# ----------------------------------------------------------------------------


def pick_snapshots(snaps, min_hours):
    """Оставляет снимки, между которыми не меньше min_hours (слишком частые отбрасываются)."""
    kept = [snaps[0]]
    for s in snaps[1:]:
        if (s["_t"] - kept[-1]["_t"]).total_seconds() >= min_hours * 3600:
            kept.append(s)
    return kept


def interval_delta(prev, cur):
    """
    Прирост просмотров между двумя снимками.
    Возвращает (всего, из_них_у_старых_постов, часов).
      * пост был в прошлом снимке  -> прирост = разница счётчиков (старый пост, «длинный хвост»);
      * поста не было, но он опубликован после прошлого снимка -> новый, все его просмотры за интервал;
      * поста не было, а опубликован раньше -> вышел за глубину снимка, не знаем прирост, пропускаем.
    """
    hours = (cur["_t"] - prev["_t"]).total_seconds() / 3600
    total = old = 0
    for pid, (d, v) in cur["posts"].items():
        if pid in prev["posts"]:
            dv = max(0, v - prev["posts"][pid][1])
            old += dv
        elif datetime.fromisoformat(d) >= prev["_t"]:
            dv = v
        else:
            continue
        total += dv
    return total, old, hours


def compute_channel(snaps, min_hours=20, threshold=200):
    kept = pick_snapshots(snaps, min_hours)
    if len(kept) < 2:
        return None
    daily, tot, old, hours = [], 0, 0, 0.0
    for a, b in zip(kept, kept[1:]):
        t, o, h = interval_delta(a, b)
        daily.append(t * 24 / h)
        tot += t
        old += o
        hours += h
    ok = sum(1 for d in daily if d >= threshold)
    return {
        "intervals": len(daily),
        "span_days": round(hours / 24, 1),
        "avg_daily_views": int(tot * 24 / hours),
        "min_daily_views": int(min(daily)),
        "intervals_ok": ok,
        "share_ok": round(ok / len(daily), 3),
        "from_old_posts_share": round(old / tot, 2) if tot else 0,
    }


def load_snapshots(path):
    by_ch = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec["_t"] = datetime.fromisoformat(rec["t"])
            by_ch.setdefault(rec["ch"], []).append(rec)
    for v in by_ch.values():
        v.sort(key=lambda r: r["_t"])
    return by_ch


def cmd_report(args):
    by_ch = load_snapshots(args.data)
    rows, waiting = [], 0
    for ch, snaps in by_ch.items():
        m = compute_channel(snaps, args.min_hours, args.threshold)
        if m is None:
            waiting += 1
            continue
        last = snaps[-1]
        newest = max((datetime.fromisoformat(d) for d, _v in last["posts"].values()), default=None)
        hits, has_bot = vpn_info(last["title"], ch, last["about"])
        rows.append({
            "username": f"@{ch}",
            "title": last["title"],
            "subscribers": last["subs"],
            "passes": "YES" if m["share_ok"] >= args.min_days_share else "",
            "confidence": "ok" if m["intervals"] >= 3 else f"низкая ({m['intervals']} интервал.)",
            **m,
            "last_post_age_days": round((last["_t"] - newest).total_seconds() / 86400, 1) if newest else "",
            "vpn_keyword_hits": hits,
            "bot_link_in_about": "yes" if has_bot else "",
            "about": last["about"],
            "link": f"https://t.me/{ch}",
        })
    rows.sort(key=lambda r: (r["passes"] == "YES", r["avg_daily_views"]), reverse=True)
    for r in rows:
        mark = "OK " if r["passes"] else "-- "
        print(f"  {mark}{r['username']}: в сутки в среднем {r['avg_daily_views']}, минимум {r['min_daily_views']}, "
              f"интервалов {r['intervals']}, из них >= {args.threshold}: {r['intervals_ok']}, "
              f"доля от старых постов {r['from_old_posts_share']}")
    if rows:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    ok = sum(1 for r in rows if r["passes"] == "YES")
    print(f"\nГотово: {len(rows)} каналов в {args.out}, проходят порог: {ok}. "
          f"Ждут второго снимка (нужно >= {args.min_hours} ч между снимками): {waiting}")


# ----------------------------------------------------------------------------
# Самопроверка
# ----------------------------------------------------------------------------


def _snap(ch, t, posts):
    return {"ch": ch, "t": t.isoformat(), "_t": t, "title": ch, "about": "", "subs": 1000,
            "posts": {str(pid): [d.isoformat(), v] for pid, (d, v) in posts.items()}}


def selftest():
    import re

    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=3)
    times = [t0 + timedelta(hours=24 * i) for i in range(4)]
    hour = timedelta(hours=1)

    # A: каждый день выходит свежий пост, к следующему снимку набирает 400 просмотров
    A = [_snap("a", t, {k: (times[k] + hour, 400) for k in range(i)}) for i, t in enumerate(times)]
    cA = compute_channel(A)
    assert cA["avg_daily_views"] == 400 and cA["share_ok"] == 1.0 and cA["from_old_posts_share"] == 0, cA

    # B: как Quattro — один старый пост, который собирает по 10 000 просмотров в сутки, свежих постов нет
    old_dt = t0 - timedelta(days=10)
    B = [_snap("b", t, {1: (old_dt, 100_000 + 10_000 * i)}) for i, t in enumerate(times)]
    cB = compute_channel(B)
    assert cB["avg_daily_views"] == 10_000 and cB["share_ok"] == 1.0 and cB["from_old_posts_share"] == 1.0, cB

    # C: мёртвый канал — счётчики не меняются
    C = [_snap("c", t, {1: (old_dt, 5000)}) for t in times]
    cC = compute_channel(C)
    assert cC["avg_daily_views"] == 0 and cC["share_ok"] == 0, cC

    # D: одна вспышка на 50 000 за трое суток
    D = [_snap("d", times[0], {}), _snap("d", times[1], {}),
         _snap("d", times[2], {9: (times[1] + hour, 50_000)}), _snap("d", times[3], {9: (times[1] + hour, 50_000)})]
    cD = compute_channel(D)
    assert cD["intervals_ok"] == 1 and cD["min_daily_views"] == 0 and cD["share_ok"] < 0.5, cD

    # слишком частые снимки отбрасываются, один снимок — пока без результата
    close = [_snap("e", t0, {}), _snap("e", t0 + timedelta(hours=2), {})]
    assert compute_channel(close) is None and compute_channel(close[:1]) is None
    # пост, вышедший за глубину снимка, не засчитывается целиком
    E = [_snap("e", times[0], {}), _snap("e", times[1], {7: (old_dt, 999_999)})]
    assert compute_channel(E)["avg_daily_views"] == 0

    # сбор постов с id через страницы (разметка как у t.me/s)
    from vpn_channels_web import _header, _msg
    name = "demo_vpn_channel"
    msgs, pid = [], 0
    for d in range(40, 0, -1):
        pid += 1
        dt = (now - timedelta(days=d)).replace(hour=12, minute=0, second=0, microsecond=0)
        msgs.append((pid, dt, "1.5K"))
    calls = []

    def fake_fetch(url):
        calls.append(url)
        m = re.search(r"\?before=(\d+)", url)
        before = int(m.group(1)) if m else 10 ** 9
        page = [x for x in msgs if x[0] < before][-20:]
        body = "".join(_msg(name, p, dt, v) for p, dt, v in page)
        return url, f"<html><body>{_header(name, 'Demo', '5K', 'vpn')}{body}</body></html>"

    res = fetch_posts(name, 14, 10, 0, fetcher=fake_fetch)
    assert res and res["subscribers"] == 5000 and len(res["posts"]) >= 14, res
    assert all(v[1] == 1500 for v in res["posts"].values()) and len(calls) <= 2, calls
    assert fetch_posts("x" * 6, 14, 3, 0, fetcher=lambda u: ("https://t.me/xxxxxx", "<html></html>")) is None

    print("selftest OK")
    for label, c in (("A свежие посты", cA), ("B длинный хвост", cB), ("C мёртвый", cC), ("D вспышка", cD)):
        print(f"  {label:16} avg/сутки={c['avg_daily_views']:<6} min={c['min_daily_views']:<6} "
              f"интервалов>=порога: {c['intervals_ok']}/{c['intervals']}  доля старых постов={c['from_old_posts_share']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")

    s = sub.add_parser("snapshot", help="снять счётчики просмотров")
    s.add_argument("--seeds", help="файл со списком @каналов (по одному в строке)")
    s.add_argument("--from-csv", help="взять каналы из CSV (колонка username), например vpn_channels_web.csv")
    s.add_argument("--only-vpn", action="store_true", help="из CSV брать только каналы с VPN-словами/ботом")
    s.add_argument("--depth-days", type=int, default=14, help="за сколько дней назад снимать счётчики постов")
    s.add_argument("--max-pages", type=int, default=10, help="максимум страниц превью на канал")
    s.add_argument("--delay", type=float, default=1.2, help="пауза между запросами, сек")
    s.add_argument("--data", default="daily_views_data.jsonl", help="файл со снимками (дописывается)")

    r = sub.add_parser("report", help="посчитать просмотры в сутки по снимкам")
    r.add_argument("--data", default="daily_views_data.jsonl")
    r.add_argument("--threshold", type=int, default=200, help="минимум просмотров в сутки")
    r.add_argument("--min-hours", type=float, default=20, help="минимальный интервал между снимками, ч")
    r.add_argument("--min-days-share", type=float, default=0.7, help="доля интервалов с выполненным порогом")
    r.add_argument("--out", default="daily_views_report.csv")

    args = p.parse_args()
    if args.cmd == "selftest":
        selftest()
    elif args.cmd == "snapshot":
        cmd_snapshot(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
