import os, io, json, datetime as dt
import requests
import numpy as np
import pandas as pd
import yfinance as yf

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TEST = os.environ.get("TEST", "").lower() == "true"

MIN_P, MAX_P = 1.0, 20.0          # fiyat aralığı ($)
MIN_DOLLAR_VOL = 2_000_000        # 20 günlük ortalama günlük işlem hacmi ($)
HOLD_DAYS = 3                     # en fazla tutma süresi (işlem günü)
MAX_OPEN = 2                      # aynı anda sanal pozisyon
ALLOC = 50.0                      # sanal işlem başına $
STATE = "state.json"


def tg(text):
    chat = get_chat()
    if not TOKEN:
        print("HATA: TELEGRAM_TOKEN secret'i bos. Secret adinin tam olarak TELEGRAM_TOKEN oldugunu kontrol et.")
    elif not chat:
        print("HATA: Sohbet bulunamadi. Token yanlis/eski olabilir veya botta Start'a basilmamis.")
    if not (TOKEN and chat):
        print(text)
        return
    for i in range(0, len(text), 3900):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": chat, "text": text[i:i + 3900]}, timeout=30)
        print("Telegram cevabi:", r.status_code, r.text[:150])


def get_chat():
    global CHAT
    if CHAT or not TOKEN:
        return CHAT
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30).json()
        print("getUpdates:", r.get("ok"), r.get("description", ""), "mesaj sayisi:", len(r.get("result", [])))
        for u in reversed(r.get("result", [])):
            m = u.get("message") or {}
            if m.get("chat", {}).get("id"):
                CHAT = str(m["chat"]["id"])
                break
    except Exception as e:
        print("chat id hatası", e)
    return CHAT


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


def analyze(df):
    """Hacimli gerçek kırılım (bugün) veya son 3 gündeki kırılımın geri testi."""
    c, h, l, o = (df[k].values for k in ("Close", "High", "Low", "Open"))
    v = df["Volume"].values.astype(float)
    n = len(c)
    if n < 40 or c[-1] < c[-20:].mean():
        return None
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = tr[-14:].mean()
    for k in range(4):
        i = n - 1 - k
        if i < 21:
            break
        level = h[i - 20:i].max()
        rng = h[i] - l[i]
        volr = v[i] / max(v[i - 20:i].mean(), 1)
        # gerçek kırılım: seviye üstü kapanış, yeşil mum, güçlü hacim, fitil değil (kapanış mumun üst kısmında)
        if not (c[i] > level and c[i] >= o[i] and volr >= 1.5 and rng > 0 and (c[i] - l[i]) / rng >= 0.6):
            continue
        if k > 0 and not (l[-1] <= level * 1.02 and c[-1] > level):
            continue
        entry = c[-1]
        if entry > level * 1.08:
            continue
        stop = level - 0.5 * atr
        risk = entry - stop
        if risk <= 0 or not (0.015 <= risk / entry <= 0.12):
            continue
        return dict(type="Kırılım" if k == 0 else "Geri test", level=float(level),
                    entry=float(entry), stop=float(stop), target=float(entry + 2 * risk),
                    volr=float(volr), date=df.index[-1].strftime("%Y-%m-%d"))
    return None


def earnings_soon(sym):
    try:
        cal = yf.Ticker(sym).calendar
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        today = dt.date.today()
        return any(0 <= (d - today).days <= 4 for d in dates)
    except Exception:
        return False


def update_positions(state, hist):
    msgs = []
    for p in state["positions"]:
        if p["status"] in ("closed", "cancelled"):
            continue
        df = hist.get(p["symbol"])
        if df is None:
            continue
        bars = df[df.index > pd.Timestamp(p["signal_date"])]
        if bars.empty:
            continue
        if p["status"] == "pending":
            o = float(bars["Open"].iloc[0])
            if o <= p["stop"] or o >= p["target"]:
                p["status"] = "cancelled"
                msgs.append(f"⚪ {p['symbol']}: ertesi gün açılış ({o:.2f}) planın dışında kaldı, işlem iptal.")
                continue
            p["entry"], p["status"] = o, "open"
            p["entry_date"] = bars.index[0].strftime("%Y-%m-%d")
        days = 0
        for _, r in bars.iterrows():
            days += 1
            reason = None
            if r["Low"] <= p["stop"]:
                ex, reason = min(p["stop"], r["Open"]), "stop"
            elif r["High"] >= p["target"]:
                ex, reason = max(p["target"], r["Open"]), "hedef"
            elif days >= HOLD_DAYS:
                ex, reason = r["Close"], "3 gün doldu"
            if reason:
                p.update(status="closed", exit=float(ex), reason=reason,
                         pnl_pct=(float(ex) / p["entry"] - 1) * 100)
                icon = "✅" if p["pnl_pct"] > 0 else "🔻"
                msgs.append(f"{icon} {p['symbol']} kapandı ({reason}): {p['pnl_pct']:+.1f}%")
                break
        p["days"] = days
    return msgs


def stats(state):
    cl = [p for p in state["positions"] if p["status"] == "closed"]
    if not cl:
        return "📊 Sanal işlem geçmişi: henüz kapanan işlem yok."
    win = sum(1 for p in cl if p["pnl_pct"] > 0)
    tot = sum(p["pnl_pct"] * ALLOC / 100 for p in cl)
    return f"📊 Sanal işlem: {len(cl)} işlem, kazanma %{win / len(cl) * 100:.0f}, toplam {tot:+.2f}$ (işlem başı {ALLOC:.0f}$)"


def main():
    if TEST:
        tg("✅ Nasdaq botu çalışıyor. Hafta içi her akşam (Nasdaq kapanışından sonra) tarama yapıp mesaj atacağım.")
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"positions": []}
    msgs = []

    active = [p["symbol"] for p in state["positions"] if p["status"] in ("pending", "open")]
    if active:
        msgs += update_positions(state, fetch(active, "1mo", False))

    hist = fetch(universe(), "6mo", True)
    active = {p["symbol"] for p in state["positions"] if p["status"] in ("pending", "open")}
    sigs = []
    for s, df in hist.items():
        if s in active:
            continue
        a = analyze(df)
        if a:
            a["symbol"] = s
            sigs.append(a)
    sigs.sort(key=lambda x: x["volr"], reverse=True)

    final = []
    for a in sigs[:12]:
        if earnings_soon(a["symbol"]):
            continue
        final.append(a)
        if len(final) == 3:
            break

    slots = MAX_OPEN - len([p for p in state["positions"] if p["status"] in ("pending", "open")])
    lines = []
    for a in final:
        tracked = slots > 0
        if tracked:
            slots -= 1
            state["positions"].append(dict(symbol=a["symbol"], signal_date=a["date"], type=a["type"],
                                           stop=a["stop"], target=a["target"], status="pending"))
        lines.append(f"{'🟢' if tracked else '🔵'} {a['symbol']} ({a['type']})\n"
                     f"Giriş ~{a['entry']:.2f} | Stop {a['stop']:.2f} | Hedef {a['target']:.2f}\n"
                     f"Kırılım seviyesi {a['level']:.2f} | Hacim {a['volr']:.1f}x | Risk/Getiri 1:2"
                     f"{' | sanal işleme alındı' if tracked else ''}")

    head = f"📅 Nasdaq tarama ({dt.date.today():%d.%m.%Y})"
    body = "\n\n".join(lines) if lines else "Bugün şartları sağlayan hisse yok. Zorlamıyoruz."
    tail = "\n".join(msgs + [stats(state)])
    tg(f"{head}\n\n{body}\n\n{tail}\n\nYatırım tavsiyesi değildir; sanal işlemle test aşamasındadır.")
    json.dump(state, open(STATE, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
