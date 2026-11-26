import os, io, json, datetime as dt
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ---------------- Ayarlar ----------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip() or "nasdaq-claud-8axkh9njyd"
TEST = os.environ.get("TEST", "").lower() == "true"
INTRADAY = (os.environ.get("INTRADAY", "").lower() == "true"
            or os.environ.get("SCHEDULE", "") == "0 15 * * 1-5")
MANUAL = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"

MIN_P, MAX_P = 0.30, 5.0          # fiyat aralığı ($)
MIN_DOLLAR_VOL = 2_000_000        # 20 günlük ortalama günlük işlem hacmi ($)
HOLD_DAYS = 3                     # girişten sonra en fazla tutma süresi (işlem günü)
WATCH_DAYS = 3                    # kırılım adayının tetiklenmesi için beklenen gün
MAX_ACTIVE = 5                    # aynı anda takipteki sanal işlem sayısı
NEW_PER_DAY = 2                   # günde en fazla yeni sanal işlem
ALLOC = 50.0                      # sanal işlem başına $
COMMISSION = 3.0                  # Midas: alışta 1,50$ + satışta 1,50$
STATE = "state.json"


# ---------------- Bildirim ----------------
def send(text, title="Nasdaq Bot", priority=None):
    blocks, chunks, cur = text.split("\n\n"), [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > 1500:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    headers = {"Title": title}
    if priority:
        headers["Priority"] = priority
    for ch in chunks:
        try:
            r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=ch.encode("utf-8"),
                              headers=headers, timeout=30)
            print("ntfy cevabi:", r.status_code)
        except Exception as e:
            print("ntfy hatasi:", e)
    print(text)


def fmt(x):
    return f"{x:.2f}" if x >= 1 else f"{x:.3f}"


# ---------------- Veri ----------------
def universe():
    r = requests.get("https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt", timeout=30)
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Symbol"].notna() & ~df["Symbol"].astype(str).str.contains("File Creation")]
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
    bad = "Warrant|Right|Unit|Preferred|Note|Depositary"
    df = df[~df["Security Name"].astype(str).str.contains(bad, case=False)]
    syms = [s for s in df["Symbol"].astype(str) if s.isalpha() and len(s) <= 5]
    return sorted(set(syms))


def fetch(symbols, period, filt):
    out = {}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        try:
            data = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            print("indirme hatası", e)
            continue
        for s in chunk:
            try:
                df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                df.index = pd.to_datetime(df.index).tz_localize(None)
            except Exception:
                continue
            if len(df) < 5:
                continue
            if filt:
                if len(df) < 60:
                    continue
                last = df["Close"].iloc[-1]
                dv = (df["Close"] * df["Volume"]).tail(20).mean()
                if not (MIN_P <= last <= MAX_P) or dv < MIN_DOLLAR_VOL:
                    continue
            out[s] = df
    return out


# ---------------- Tarama ----------------
COUNT = [0] * 6   # 0 taranan, 1 trend, 2 kırılım, 3 hacim+mum, 4 zamanlama, 5 sinyal


def record(stage):
    for k in range(stage + 1):
        COUNT[k] += 1


def arrays(df):
    c, h, l, o = (df[k].values for k in ("Close", "High", "Low", "Open"))
    v = df["Volume"].values.astype(float)
    return c, h, l, o, v


def uptrend(c):
    """Net yükselen trend: fiyat > 20g ort > 50g ort ve 20g ort yükseliyor."""
    sma20, sma50 = c[-20:].mean(), c[-50:].mean()
    return c[-1] > sma20 > sma50 and sma20 > c[-25:-5].mean()


def atr14(c, h, l):
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    return tr[-14:].mean()


def analyze(df):
    """Hacimli gerçek kırılım (bugün) veya son 3 gündeki kırılımın geri testi."""
    c, h, l, o, v = arrays(df)
    n = len(c)
    if n < 60 or not uptrend(c):
        record(0)
        return None
    stage = 1
    atr = atr14(c, h, l)
    for k in range(4):
        i = n - 1 - k
        if i < 21:
            break
        level = h[i - 20:i].max()
        rng = h[i] - l[i]
        volr = v[i] / max(v[i - 20:i].mean(), 1)
        if not (c[i] > level and c[i] >= o[i]):
            continue
        stage = max(stage, 2)
        if not (volr >= 1.5 and rng > 0 and (c[i] - l[i]) / rng >= 0.6):
            continue
        stage = max(stage, 3)
        if k > 0 and not (l[-1] <= level * 1.02 and c[-1] > level):
            continue
        stage = max(stage, 4)
        entry = c[-1]
        if entry > level * 1.08:
            continue
        stop = level - 0.5 * atr
        risk = entry - stop
        if risk <= 0 or not (0.015 <= risk / entry <= 0.12):
            continue
        record(5)
        return dict(kind="signal", type="Kırılım" if k == 0 else "Geri test", level=float(level),
                    entry=float(entry), stop=float(stop), target=float(entry + 2 * risk),
                    volr=float(volr), date=df.index[-1].strftime("%Y-%m-%d"))
    record(stage)
    return None


def watch(df):
    """Kırılıma %4 kadar yaklaşmış, yükselen trenddeki hisse: seviyenin üstüne çıkarsa alınacak."""
    c, h, l, o, v = arrays(df)
    if len(c) < 60 or not uptrend(c):
        return None
    level = h[-20:].max()
    if c[-1] >= level:
        return None
    dist = level / c[-1] - 1
    if dist > 0.04 or v[-3:].mean() < 0.9 * v[-20:].mean():
        return None
    atr = atr14(c, h, l)
    entry = level * 1.002
    stop = level - 0.5 * atr
    risk = entry - stop
    if risk <= 0 or not (0.015 <= risk / entry <= 0.12):
        return None
    return dict(kind="watch", type="Kırılım adayı", level=float(level), entry=float(entry),
                stop=float(stop), target=float(entry + 2 * risk), dist=float(dist),
                close=float(c[-1]), avgvol=float(v[-20:].mean()),
                date=df.index[-1].strftime("%Y-%m-%d"))


def earnings_soon(sym):
    try:
        cal = yf.Ticker(sym).calendar
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        today = dt.date.today()
        return any(0 <= (d - today).days <= 4 for d in dates)
    except Exception:
        return False


# ---------------- Sanal işlem takibi ----------------
def close_pos(p, ex, reason, msgs):
    p.update(status="closed", exit=float(ex), reason=reason,
             pnl_pct=(float(ex) / p["entry"] - 1) * 100)
    net = p["pnl_pct"] * ALLOC / 100 - COMMISSION
    msgs.append(f"{'✅' if net > 0 else '🔻'} {p['symbol']} kapandı ({reason}): "
                f"{p['pnl_pct']:+.1f}% (komisyon sonrası {net:+.2f}$)")


def update_positions(state, hist):
    msgs = []
    for p in state["positions"]:
        if p["status"] in ("closed", "cancelled"):
            continue
        df = hist.get(p["symbol"])
        if df is None:
            continue
        entered = p["status"] == "open"
        if entered:
            bars = df[df.index >= pd.Timestamp(p["entry_date"])]
        else:
            bars = df[df.index > pd.Timestamp(p["signal_date"])]
        days = watched = 0
        for idx, r in bars.iterrows():
            if not entered:
                if p["status"] == "pending":
                    entry = float(r["Open"])
                    if entry <= p["stop"] or entry >= p["target"]:
                        p["status"] = "cancelled"
                        msgs.append(f"⚪ {p['symbol']}: ertesi gün açılış ({fmt(entry)}) planın dışında kaldı, iptal.")
                        break
                else:  # watching
                    watched += 1
                    if r["High"] >= p["level"]:
                        entry = max(p["level"], float(r["Open"]))
                        if entry <= p["stop"] or entry >= p["target"]:
                            p["status"] = "cancelled"
                            msgs.append(f"⚪ {p['symbol']}: kırılım planın dışında gerçekleşti, iptal.")
                            break
                    elif watched >= WATCH_DAYS:
                        p["status"] = "cancelled"
                        msgs.append(f"⚪ {p['symbol']}: {WATCH_DAYS} günde kırılım olmadı, takip bitti.")
                        break
                    else:
                        continue
                p.update(entry=entry, status="open", entry_date=idx.strftime("%Y-%m-%d"))
                entered = True
                msgs.append(f"🔔 {p['symbol']}: sanal alım {fmt(entry)}")
            days += 1
            if r["Low"] <= p["stop"]:
                close_pos(p, min(p["stop"], r["Open"]), "stop", msgs)
                break
            if r["High"] >= p["target"]:
                close_pos(p, max(p["target"], r["Open"]), "hedef", msgs)
                break
            if days >= HOLD_DAYS:
                close_pos(p, r["Close"], f"{HOLD_DAYS} gün doldu", msgs)
                break
        p["days"] = days
    return msgs


def stats(state):
    cl = [p for p in state["positions"] if p["status"] == "closed"]
    if not cl:
        return "📊 Sanal işlem geçmişi: henüz kapanan işlem yok."
    net = [p["pnl_pct"] * ALLOC / 100 - COMMISSION for p in cl]
    win = sum(1 for x in net if x > 0)
    return (f"📊 Sanal işlem: {len(cl)} işlem, kazanma %{win / len(cl) * 100:.0f}, "
            f"komisyon sonrası toplam {sum(net):+.2f}$ (işlem başı {ALLOC:.0f}$, komisyon {COMMISSION:.0f}$)")


# ---------------- Gün içi kontrol (açılıştan sonra) ----------------
def intraday(state):
    today = dt.date.today()
    items = [w for w in state.get("watch", [])
             if not w.get("alerted") and (today - dt.date.fromisoformat(w["date"])).days <= 4]
    if not items:
        print("Takip listesi boş.")
        return
    syms = [w["symbol"] for w in items]
    try:
        data = yf.download(syms, period="1d", interval="5m", group_by="ticker",
                           auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        print("gün içi veri hatası", e)
        return
    ny_today = pd.Timestamp.now(tz="America/New_York").date()
    lines = []
    for w in items:
        try:
            df = data[w["symbol"]] if isinstance(data.columns, pd.MultiIndex) else data
            df = df[["High", "Close", "Volume"]].dropna()
            if df.empty or df.index[-1].date() != ny_today:
                continue
        except Exception:
            continue
        last, hi, vol = float(df["Close"].iloc[-1]), float(df["High"].max()), float(df["Volume"].sum())
        if hi < w["level"]:
            continue
        w["alerted"] = True
        ext = last / w["level"] - 1
        note = (f"⚠️ seviyenin %{ext * 100:.1f} üstünde, peşinden koşma" if ext > 0.05
                else "seviyenin altına geri döndü, bekle" if last < w["level"]
                else "plana uygun bölgede")
        lines.append(f"🚀 {w['symbol']} kırılım seviyesini ({fmt(w['level'])}) geçti\n"
                     f"Şu an {fmt(last)} | Stop {fmt(w['stop'])} | Hedef {fmt(w['target'])}\n"
                     f"Hacim: günlük ortalamanın %{vol / max(w['avgvol'], 1) * 100:.0f}'i | {note}")
    if lines:
        send("\n\n".join(lines) + "\n\nBilanço tarihini kendin kontrol et. Yatırım tavsiyesi değildir.",
             title="Nasdaq Kirilim", priority="high")
    else:
        print("Henüz kırılım yok.")


# ---------------- Ana akış ----------------
def main():
    if TEST:
        send("✅ Nasdaq botu çalışıyor. Hafta içi Türkiye saatiyle yaklaşık 16:00'da (piyasa açılmadan önce) "
             "tarama, 18:00'de kırılım kontrolü yapıp mesaj atacağım.")
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    state.setdefault("positions", [])
    state.setdefault("watch", [])

    if INTRADAY:
        intraday(state)
        json.dump(state, open(STATE, "w"), indent=1, ensure_ascii=False)
        return

    hist = fetch(universe(), "6mo", True)
    if not hist:
        send("⚠️ Hisse verisi alınamadı, bugün tarama yapılamadı.")
        return
    ldate = max(df.index[-1] for df in hist.values()).strftime("%Y-%m-%d")
    if not MANUAL and state.get("last_bar") == ldate:
        send("ℹ️ Yeni işlem günü verisi yok (piyasa dün kapalıydı). Bugün tarama yok.")
        return
    state["last_bar"] = ldate

    live = [p["symbol"] for p in state["positions"] if p["status"] in ("pending", "watching", "open")]
    msgs = update_positions(state, fetch(live, "1mo", False)) if live else []

    active = {p["symbol"] for p in state["positions"] if p["status"] in ("pending", "watching", "open")}
    sigs, watches = [], []
    for s, df in hist.items():
        if s in active:
            continue
        a = analyze(df)
        if a:
            a["symbol"] = s
            sigs.append(a)
            continue
        w = watch(df)
        if w:
            w["symbol"] = s
            watches.append(w)
    sigs.sort(key=lambda x: x["volr"], reverse=True)
    watches.sort(key=lambda x: x["dist"])

    final_sigs = [a for a in sigs[:10] if not earnings_soon(a["symbol"])][:3]
    final_watch = [a for a in watches[:10] if not earnings_soon(a["symbol"])][:4]

    slots = min(NEW_PER_DAY, MAX_ACTIVE - len([p for p in state["positions"]
                                               if p["status"] in ("pending", "watching", "open")]))
    lines = []
    for a in final_sigs + final_watch:
        tracked = slots > 0
        if tracked:
            slots -= 1
            state["positions"].append(dict(symbol=a["symbol"], signal_date=a["date"], type=a["type"],
                                           level=a["level"], stop=a["stop"], target=a["target"],
                                           status="pending" if a["kind"] == "signal" else "watching"))
        mark = ("🟢" if tracked else "🔵") if a["kind"] == "signal" else ("🟢" if tracked else "🟡")
        tail = " | sanal işleme alındı" if tracked else ""
        if a["kind"] == "signal":
            lines.append(f"{mark} {a['symbol']} ({a['type']})\n"
                         f"Giriş ~{fmt(a['entry'])} | Stop {fmt(a['stop'])} | Hedef {fmt(a['target'])}\n"
                         f"Kırılım seviyesi {fmt(a['level'])} | Hacim {a['volr']:.1f}x | Risk/Getiri 1:2{tail}")
        else:
            lines.append(f"{mark} {a['symbol']} (Kırılım adayı)\n"
                         f"{fmt(a['level'])} üstüne çıkarsa al | Stop {fmt(a['stop'])} | Hedef {fmt(a['target'])}\n"
                         f"Şu an {fmt(a['close'])} (seviyeye %{a['dist'] * 100:.1f}){tail}")

    # takip listesini kaydet (gün içi kontrol bunu kullanır)
    today = dt.date.today()
    keep = [w for w in state["watch"] if not w.get("alerted")
            and (today - dt.date.fromisoformat(w["date"])).days <= 4
            and w["symbol"] not in {a["symbol"] for a in final_watch}]
    state["watch"] = keep + [dict(symbol=a["symbol"], level=a["level"], stop=a["stop"],
                                  target=a["target"], avgvol=a["avgvol"], date=today.isoformat(),
                                  alerted=False) for a in final_watch]

    funnel = (f"🔎 Eleme: taranan {COUNT[0]} → trend {COUNT[1]} → kırılım {COUNT[2]} → "
              f"hacim/mum {COUNT[3]} → zamanlama {COUNT[4]} → sinyal {COUNT[5]} | kırılım adayı {len(watches)}")
    head = f"📅 Nasdaq tarama ({today:%d.%m.%Y}) | veri günü {ldate}"
    body = "\n\n".join(lines) if lines else "Bugün şartları sağlayan hisse yok. Zorlamıyoruz."
    legend = ("🟢 sanal işleme alındı | 🔵 sadece bilgi | 🟡 kırılım adayı: seviyeyi geçerse "
              "18:00'de ayrıca bildirim gelir") if lines else ""
    foot = "\n".join(x for x in msgs + [stats(state), funnel] if x)
    send(f"{head}\n\n{body}\n\n{legend}\n\n{foot}\n\nBilanço tarihini kendin kontrol et. "
         f"Yatırım tavsiyesi değildir; sanal işlemle test aşamasındadır.")
    json.dump(state, open(STATE, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
