#!/usr/bin/env python3
"""
Harmonogram wyłączeń farmy PV Leśnice II (996 kWp).
Pobiera 15-minutowe ceny RCE z PSE na dzień następny i wyznacza ZWARTE OKNA,
w których cena jest poniżej progu opłacalności — do ręcznego wyłączenia farmy.

Logika decyzyjna (z umowy sprzedaży energii):
  - opłata bilansowania WB płacona od każdej MWh wprowadzonej (domyślnie 24 zł/MWh),
  - próg redukcji umownej 22 zł/MWh (poniżej wpada cena niezbilansowania, zwykle ujemna),
  => produkcja opłacalna tylko gdy RCE > próg. Domyślny próg = stawka bilansowania (24 zł).
"""

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from datetime import datetime, timedelta
import os
import ssl
import smtplib
from email.message import EmailMessage

# ─────────────── KONFIGURACJA ───────────────
def _env_float(nazwa, domyslna):
    v = os.environ.get(nazwa, "")
    return float(v) if v not in (None, "") else float(domyslna)

def _env_int(nazwa, domyslna):
    v = os.environ.get(nazwa, "")
    return int(v) if v not in (None, "") else int(domyslna)

PROG_ZL      = _env_float("PROG_ZL", 24.0)     # próg [zł/MWh]; poniżej = wyłącz
MIN_OKNO_KW  = _env_int("MIN_OKNO_KW", 2)      # min. długość okna [kwadranse] (2 = 30 min)
SCAL_PRZERWA = _env_int("SCAL_PRZERWA", 4)     # scal okna, gdy dzieli je ≤ N kwadransów (4 = 1h)
SCAL_PROG_ZL = _env_float("SCAL_PROG_ZL", 100) # ...i gdy maks. cena w przerwie < tej wartości
CAPACITY_KWP = 996
# ────────────────────────────────────────────


def fetch_rce(date_str: str) -> list[dict]:
    # API PSE nie obsługuje $orderby ani $top — filtrujemy tylko po dacie,
    # a sortowanie kwadransów robimy po stronie Pythona.
    url = (f"https://api.raporty.pse.pl/api/rce-pln"
           f"?$filter=business_date eq '{date_str}'")
    r = requests.get(url, headers={"Accept": "application/json",
                                   "User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    recs = data.get("value", data) if isinstance(data, dict) else data
    if not recs:
        raise ValueError(f"Brak cen RCE dla {date_str}. Dane D+1 publikowane są po ~14:00.")
    # sortowanie po numerze kwadransa; fallback po znaczniku czasu
    try:
        recs = sorted(recs, key=lambda x: int(x.get("period", 0)))
    except (ValueError, TypeError):
        recs = sorted(recs, key=lambda x: str(x.get("dtime", "")))
    return recs


def build_series(recs: list[dict]):
    # ceny w kolejności kwadransów (recs są już posortowane)
    prices = [float(rec.get("rce_pln", 0)) for rec in recs]
    # czas POCZĄTKU każdego kwadransa liczymy z pozycji: kwadrans i zaczyna się o i*15 min.
    # To jest odporne na dowolny format pola dtime z API.
    times = []
    for i in range(len(prices)):
        h, m = divmod(i * 15, 60)
        times.append(f"{h:02d}:{m:02d}")
    return times, prices


def kw_to_time(idx: int, times: list) -> str:
    """Czas POCZĄTKU kwadransa idx; dla idx==len → koniec doby."""
    if idx < len(times):
        return times[idx]
    return "24:00"


def wyznacz_okna(times, prices):
    """Zwraca listę okien wyłączeń: (start_idx, end_idx_exclusive, min_cena, sr_cena, n_kw)."""
    n = len(prices)
    below = [p <= PROG_ZL for p in prices]

    # 1. surowe bloki ciągłe poniżej progu
    surowe = []
    i = 0
    while i < n:
        if below[i]:
            j = i
            while j < n and below[j]:
                j += 1
            surowe.append([i, j])   # [start, end_exclusive]
            i = j
        else:
            i += 1

    if not surowe:
        return []

    # 2. scalanie okien rozdzielonych krótką przerwą
    #    Przy RĘCZNYM wyłączaniu nie opłaca się włączać farmy na krótko między
    #    dwoma oknami wyłączeń — więcej zachodu niż zysku. Dlatego krótką przerwę
    #    (≤ SCAL_PRZERWA kwadransów) scalamy ZAWSZE, niezależnie od ceny w przerwie.
    #    SCAL_PROG_ZL pozostaje bezpiecznikiem dla przerw nieco dłuższych: tam
    #    scalamy tylko, gdy cena w przerwie nie skoczyła wysoko.
    scalone = [surowe[0]]
    for blk in surowe[1:]:
        prev = scalone[-1]
        przerwa = blk[0] - prev[1]                  # liczba kwadransów między oknami
        if przerwa <= SCAL_PRZERWA:
            prev[1] = blk[1]                         # krótka przerwa -> scal zawsze
            continue
        if przerwa <= SCAL_PRZERWA * 2:
            maxc = max(prices[prev[1]:blk[0]])
            if maxc < SCAL_PROG_ZL:
                prev[1] = blk[1]                     # średnia przerwa -> scal, jeśli tanio
                continue
        scalone.append(blk)

    # 3. statystyki + odsiew zbyt krótkich
    okna = []
    for s, e in scalone:
        seg = prices[s:e]
        okna.append({
            "start": s, "end": e, "n_kw": e - s,
            "min": min(seg), "sr": float(np.mean(seg)),
            "t_start": kw_to_time(s, times), "t_end": kw_to_time(e, times),
            "krotkie": (e - s) < MIN_OKNO_KW
        })
    return okna


def generate_chart(times, prices, okna, date_str, out_path):
    n = len(prices); x = np.arange(n)
    colors = ["#c0392b" if p <= PROG_ZL else "#27ae60" for p in prices]
    fig, ax = plt.subplots(figsize=(18, 7))
    ax.bar(x, prices, color=colors, width=0.9, zorder=2)
    ax.axhline(PROG_ZL, color="#e67e22", lw=1.5, ls="--", zorder=3)
    ax.axhline(0, color="#555", lw=0.8, zorder=1)

    # zacieniowanie okien wyłączeń
    for o in okna:
        if o["krotkie"]:
            continue
        ax.axvspan(o["start"] - 0.5, o["end"] - 0.5, color="#c0392b", alpha=0.10, zorder=0)
        ax.text((o["start"] + o["end"]) / 2 - 0.5, ax.get_ylim()[1] * 0.92,
                f'WYŁĄCZ\n{o["t_start"]}–{o["t_end"]}', ha="center", va="top",
                fontsize=8, fontweight="bold", color="#c0392b")

    ticks = list(range(0, n, 4))
    ax.set_xticks(ticks)
    ax.set_xticklabels([times[i] for i in ticks], rotation=45, fontsize=9)
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_ylabel("RCE [zł/MWh]", fontsize=11)
    ax.grid(axis="y", ls=":", alpha=0.6, zorder=0)

    d = datetime.strptime(date_str, "%Y-%m-%d")
    wd = ["poniedziałek","wtorek","środa","czwartek","piątek","sobota","niedziela"][d.weekday()]
    ax.set_title(f"Harmonogram wyłączeń – {wd}, {d.strftime('%-d.%-m.%Y')}\n"
                 f"Farma PV Leśnice II ({CAPACITY_KWP} kWp) | próg {PROG_ZL:.0f} zł/MWh",
                 fontsize=13, fontweight="bold", pad=12)

    leg = [mpatches.Patch(color="#c0392b", label=f"Wyłącz (RCE ≤ {PROG_ZL:.0f} zł/MWh)"),
           mpatches.Patch(color="#27ae60", label="Produkuj"),
           plt.Line2D([0],[0], color="#e67e22", lw=1.5, ls="--", label=f"Próg {PROG_ZL:.0f} zł/MWh")]
    ax.legend(handles=leg, loc="upper right", fontsize=9, framealpha=0.9)
    plt.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def build_body(times, prices, okna, date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    wd = ["poniedziałek","wtorek","środa","czwartek","piątek","sobota","niedziela"][d.weekday()]
    p = np.array(prices)
    n_below = int((p <= PROG_ZL).sum())
    glowne = [o for o in okna if not o["krotkie"]]

    L = []
    L.append("=" * 56)
    L.append(f"  HARMONOGRAM WYŁĄCZEŃ — LEŚNICE II ({CAPACITY_KWP} kWp)")
    L.append(f"  {wd}, {d.strftime('%-d.%-m.%Y')}")
    L.append("=" * 56)
    L.append("")

    if not glowne:
        L.append("  >>> NIE WYŁĄCZAJ — produkcja opłacalna przez całą dobę.")
        L.append("")
    else:
        wiele = len(glowne) > 1
        for idx, o in enumerate(glowne, 1):
            minut = o["n_kw"] * 15
            h, m = divmod(minut, 60)
            dur = f"{h}h {m:02d}min" if h else f"{m}min"
            naglowek = f"  OKNO {idx}:" if wiele else "  DZIAŁANIE NA JUTRO:"
            L.append(naglowek)
            L.append(f"     ►► WYŁĄCZ elektrownię o   {o['t_start']}")
            L.append(f"     ►► WŁĄCZ  elektrownię o   {o['t_end']}")
            L.append(f"        (wyłączenie na {dur}; min. cena {o['min']:.2f} zł/MWh)")
            L.append("")

    L.append("-" * 56)
    krotkie = [o for o in okna if o["krotkie"]]
    if krotkie:
        L.append(f"Krótkie okna < {MIN_OKNO_KW*15} min (do decyzji ręcznej):")
        for o in krotkie:
            L.append(f"   {o['t_start']}–{o['t_end']} (min {o['min']:.2f} zł/MWh)")
        L.append("")

    L.append(f"Próg opłacalności: {PROG_ZL:.0f} zł/MWh")
    L.append(f"Kwadransów poniżej progu: {n_below}/{len(prices)} ({n_below/4:.2f} godz.)")
    L.append(f"Cena doby: min {p.min():.2f}  |  maks {p.max():.2f} zł/MWh")
    L.append("")
    L.append("Wykres dobowy w załączniku. Źródło: api.raporty.pse.pl/api/rce-pln")
    return "\n".join(L)


def wyslij_mail(subject, body, attachment_path):
    """Wysyła harmonogram mailem przez SMTP. Używa sekretów jak w workflow TGE:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, RECIPIENT, RECIPIENT2."""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    rcpts = [a for a in (os.environ.get("RECIPIENT"), os.environ.get("RECIPIENT2")) if a]

    if not (host and user and pwd and rcpts):
        print("Pomijam wysyłkę maila — brak konfiguracji SMTP/odbiorców (uruchomienie lokalne).")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(rcpts)
    msg.set_content(body)

    with open(attachment_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="image", subtype="png",
                           filename=os.path.basename(attachment_path))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
    print(f"Mail wysłany do: {', '.join(rcpts)}")


def main():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    date_str = os.environ.get("PSE_DATE") or tomorrow

    recs = fetch_rce(date_str)
    times, prices = build_series(recs)
    okna = wyznacz_okna(times, prices)

    chart = os.environ.get("CHART_PATH", f"wylaczenia_{date_str}.png")
    body  = os.environ.get("BODY_PATH",  f"wylaczenia_{date_str}.txt")
    generate_chart(times, prices, okna, date_str, chart)
    tresc = build_body(times, prices, okna, date_str)
    with open(body, "w", encoding="utf-8") as f:
        f.write(tresc)

    print(tresc)

    # Zwięzłe podsumowanie do tematu maila
    glowne = [o for o in okna if not o["krotkie"]]
    if glowne:
        zakresy = ", ".join(f"{o['t_start']}-{o['t_end']}" for o in glowne)
        subject_info = f"WYLACZ {zakresy}"
    else:
        subject_info = "brak wylaczen"

    subject = f"Lesnice II {date_str} - {subject_info}"
    wyslij_mail(subject, tresc, chart)


if __name__ == "__main__":
    main()
