"""
TGE RDN – Dzienny raport email z cenami energii elektrycznej.
Pobiera dane TGeBase z tge.pl, generuje wykres i tabelę, wysyła email.

Wymagania:
    pip install httpx beautifulsoup4 lxml matplotlib

Zmienne środowiskowe (GitHub Secrets lub plik .env):
    SMTP_HOST     – np. smtp.gmail.com
    SMTP_PORT     – np. 587
    SMTP_USER     – adres nadawcy (np. twoj@gmail.com)
    SMTP_PASS     – hasło aplikacji (nie zwykłe hasło!)
    RECIPIENT     – adres odbiorcy
"""

import os
import re
import base64
import io
import smtplib
import sys
from datetime import date, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from statistics import mean, stdev

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from bs4 import BeautifulSoup

# ── Konfiguracja ──────────────────────────────────────────────────────────────

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
RECIPIENT = os.getenv("RECIPIENT", SMTP_USER)

TGE_URL = "https://tge.pl/energia-elektryczna-rdn-tge-base"
PSE_RCE_URL = "https://api.raporty.pse.pl/api/rce-pln"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

# ── Pobieranie danych z TGE ───────────────────────────────────────────────────

def fetch_tge_data():
    """
    Pobiera dane TGeBase ze strony TGE.
    Fallback: PSE RCE API (publicznie dostępne bez autoryzacji).
    Zwraca (data_dostawy, avg, rows).
    """
    delivery_date = date.today() + timedelta(days=1)

    # Próba 1: TGE
    try:
        rows = _fetch_from_tge()
        if rows:
            avg_pln = round(mean(r["cena"] for r in rows), 2)
            print("  Źródło: TGE TGeBase")
            return delivery_date, avg_pln, rows
    except Exception as e:
        print(f"  TGE niedostępne ({e}), próbuję PSE...")

    # Próba 2: PSE RCE API
    try:
        rows = _fetch_from_pse(delivery_date)
        if rows:
            avg_pln = round(mean(r["cena"] for r in rows), 2)
            print("  Źródło: PSE RCE API")
            return delivery_date, avg_pln, rows
    except Exception as e:
        print(f"  PSE niedostępne ({e})")

    return delivery_date, None, []


def _fetch_from_tge() -> list:
    """Pobiera dane z tge.pl (TGeBase)."""
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        resp = client.get(TGE_URL)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    rows = []

    for t in soup.find_all("table"):
        headers_text = " ".join(
            th.get_text(strip=True).lower()
            for th in t.find_all(["th", "td"])[:8]
        )
        if any(kw in headers_text for kw in ["czas", "kurs", "wolumen", "0-1", "1-2"]):
            tbody = t.find("tbody") or t
            for tr in tbody.find_all("tr"):
                cells = [
                    td.get_text(strip=True).replace(",", ".").replace("\xa0", "").replace(" ", "")
                    for td in tr.find_all(["td", "th"])
                ]
                if len(cells) < 2:
                    continue
                label = cells[0]
                if not re.match(r"^\d{1,2}-\d{1,2}$", label):
                    continue
                try:
                    price = float(cells[1])
                    volume = float(cells[2]) if len(cells) > 2 else None
                    hour = int(label.split("-")[0])
                    rows.append({
                        "godzina": hour,
                        "label": f"{label}h",
                        "cena": price,
                        "wolumen": volume,
                    })
                except (ValueError, IndexError):
                    continue
            if rows:
                break

    return rows


def _fetch_from_pse(target_date: date) -> list:
    """
    Pobiera ceny RCE z publicznego API PSE (15-minutowe → agregujemy do godzinowych).
    Endpoint: https://api.raporty.pse.pl/api/rce-pln
    """
    date_str = target_date.strftime("%Y-%m-%d")
    url = f"{PSE_RCE_URL}?$filter=business_date eq '{date_str}'&$top=200"

    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    entries = data.get("value", [])
    if not entries:
        return []

    # PSE zwraca dane 15-minutowe – agregujemy do godzinowych (średnia)
    hourly: dict[int, list[float]] = {}
    for entry in entries:
        period = entry.get("period", "")  # np. "00:00 - 00:15"
        price = entry.get("rce_pln")
        if price is None:
            continue
        try:
            hour_str = period.split(":")[0].strip()
            hour = int(hour_str)
            hourly.setdefault(hour, []).append(float(price))
        except (ValueError, IndexError):
            continue

    rows = []
    for hour in sorted(hourly):
        avg = round(mean(hourly[hour]), 2)
        rows.append({
            "godzina": hour,
            "label": f"{hour}-{hour+1}h",
            "cena": avg,
            "wolumen": None,
        })

    return rows


# ── Generowanie wykresu ───────────────────────────────────────────────────────

def _bar_color(price: float) -> str:
    if price < 0:
        return "#1D9E75"
    if price < 150:
        return "#5DCAA5"
    if price < 350:
        return "#EF9F27"
    if price < 600:
        return "#378ADD"
    return "#E24B4A"


def generate_chart_png(rows: list, delivery_date: date) -> bytes:
    """Generuje wykres słupkowy jako PNG (bytes)."""
    labels = [r["label"] for r in rows]
    prices = [r["cena"] for r in rows]
    colors = [_bar_color(p) for p in prices]

    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor("#FAFAFA")
    ax.set_facecolor("#FAFAFA")

    bars = ax.bar(labels, prices, color=colors, width=0.7, zorder=2)

    # Linia średniej
    if prices:
        avg = mean(prices)
        ax.axhline(avg, color="#888", linewidth=1.2, linestyle="--", zorder=3,
                   label=f"Średnia: {avg:.1f} PLN/MWh")

    # Etykiety słupków
    for bar, price in zip(bars, prices):
        if abs(price) > 30:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + (8 if price >= 0 else -25),
                f"{price:.0f}",
                ha="center", va="bottom", fontsize=7, color="#444",
            )

    ax.set_xlabel("Godzina dostawy", fontsize=10, color="#555")
    ax.set_ylabel("PLN/MWh", fontsize=10, color="#555")
    ax.set_title(
        f"Ceny energii TGeBase — dostawa {delivery_date.strftime('%d.%m.%Y')} (wtorek)",
        fontsize=12, fontweight="bold", color="#222", pad=12,
    )
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.tick_params(axis="y", labelsize=9)
    ax.grid(axis="y", color="#ddd", linewidth=0.7, zorder=1)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Legenda kolorów
    legend_patches = [
        mpatches.Patch(color="#1D9E75", label="Ujemna"),
        mpatches.Patch(color="#5DCAA5", label="0–150"),
        mpatches.Patch(color="#EF9F27", label="150–350"),
        mpatches.Patch(color="#378ADD", label="350–600"),
        mpatches.Patch(color="#E24B4A", label=">600"),
    ]
    ax.legend(
        handles=legend_patches,
        title="PLN/MWh",
        loc="upper left",
        fontsize=8,
        title_fontsize=8,
        framealpha=0.85,
    )
    ax.legend(handles=legend_patches + [
        mpatches.Patch(color="#888", label=f"Śr. {mean(prices):.1f}" if prices else "")
    ], title="Legenda", loc="upper left", fontsize=8, framealpha=0.9)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Generowanie treści emaila ─────────────────────────────────────────────────

def _row_bg(price: float) -> str:
    if price < 0:
        return "#d4f5ea"
    if price < 150:
        return "#e8f8f3"
    if price < 350:
        return "#fef9ec"
    if price < 600:
        return "#e8f1fb"
    return "#fdecea"


def build_html_email(rows: list, avg_pln: float | None, delivery_date: date) -> str:
    """Buduje HTML treści emaila z tabelą cen."""
    prices = [r["cena"] for r in rows]
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 0
    min_hour = next((r["label"] for r in rows if r["cena"] == min_price), "–")
    max_hour = next((r["label"] for r in rows if r["cena"] == max_price), "–")
    avg = round(mean(prices), 2) if prices else 0
    peak = [r["cena"] for r in rows if 7 <= r["godzina"] <= 21]
    offpeak = [r["cena"] for r in rows if r["godzina"] < 7 or r["godzina"] > 21]
    peak_avg = round(mean(peak), 2) if peak else "–"
    offpeak_avg = round(mean(offpeak), 2) if offpeak else "–"

    table_rows = ""
    for r in rows:
        bg = _row_bg(r["cena"])
        bold = " font-weight:600;" if r["cena"] == max_price or r["cena"] == min_price else ""
        table_rows += f"""
        <tr>
          <td style="padding:6px 12px; border-bottom:1px solid #eee; text-align:center;">{r['label']}</td>
          <td style="padding:6px 16px; border-bottom:1px solid #eee; text-align:right; background:{bg};{bold}">
            {r['cena']:.2f}
          </td>
          <td style="padding:6px 12px; border-bottom:1px solid #eee; text-align:right; color:#888; font-size:12px;">
            {f"{r['wolumen']:.1f}" if r['wolumen'] else "–"}
          </td>
        </tr>"""

    weekdays = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
    weekday = weekdays[delivery_date.weekday()]

    return f"""
<!DOCTYPE html>
<html lang="pl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
 <tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

   <!-- Nagłówek -->
   <tr>
    <td style="background:#1a3c5e;padding:24px 32px;">
     <p style="margin:0;font-size:11px;color:#7ab3d4;letter-spacing:1px;text-transform:uppercase;">Towarowa Giełda Energii · RDN</p>
     <h1 style="margin:6px 0 0;font-size:22px;color:#fff;font-weight:700;">Raport cen energii</h1>
     <p style="margin:4px 0 0;font-size:14px;color:#a8cce0;">
       Dostawa: <strong>{delivery_date.strftime('%d.%m.%Y')}</strong> ({weekday})
     </p>
    </td>
   </tr>

   <!-- Karty statystyk -->
   <tr>
    <td style="padding:24px 32px 12px;">
     <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
       <td width="25%" style="padding:0 6px 0 0;">
        <div style="background:#f0f7ff;border-radius:8px;padding:14px 16px;text-align:center;">
         <p style="margin:0;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Średnia TGeBase</p>
         <p style="margin:6px 0 0;font-size:22px;font-weight:700;color:#1a3c5e;">{avg:.0f}</p>
         <p style="margin:2px 0 0;font-size:11px;color:#888;">PLN/MWh</p>
        </div>
       </td>
       <td width="25%" style="padding:0 6px;">
        <div style="background:#fff8e6;border-radius:8px;padding:14px 16px;text-align:center;">
         <p style="margin:0;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Szczyt 7–22h</p>
         <p style="margin:6px 0 0;font-size:22px;font-weight:700;color:#B8860B;">{peak_avg}</p>
         <p style="margin:2px 0 0;font-size:11px;color:#888;">PLN/MWh</p>
        </div>
       </td>
       <td width="25%" style="padding:0 6px;">
        <div style="background:#fdecea;border-radius:8px;padding:14px 16px;text-align:center;">
         <p style="margin:0;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Maksimum</p>
         <p style="margin:6px 0 0;font-size:22px;font-weight:700;color:#c0392b;">{max_price:.0f}</p>
         <p style="margin:2px 0 0;font-size:11px;color:#888;">{max_hour}</p>
        </div>
       </td>
       <td width="25%" style="padding:0 0 0 6px;">
        <div style="background:#e8faf3;border-radius:8px;padding:14px 16px;text-align:center;">
         <p style="margin:0;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:0.5px;">Minimum</p>
         <p style="margin:6px 0 0;font-size:22px;font-weight:700;color:#1D9E75;">{min_price:.0f}</p>
         <p style="margin:2px 0 0;font-size:11px;color:#888;">{min_hour}</p>
        </div>
       </td>
      </tr>
     </table>
    </td>
   </tr>

   <!-- Wykres -->
   <tr>
    <td style="padding:16px 32px 8px;">
     <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#333;">Wykres godzinowy</p>
     <img src="cid:chart_image" width="576" style="width:100%;border-radius:6px;display:block;" alt="Wykres cen RDN">
    </td>
   </tr>

   <!-- Tabela -->
   <tr>
    <td style="padding:16px 32px 24px;">
     <p style="margin:0 0 12px;font-size:13px;font-weight:600;color:#333;">Tabela cen godzinowych (PLN/MWh)</p>
     <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:6px;overflow:hidden;font-size:13px;">
      <thead>
       <tr style="background:#f7f9fc;">
        <th style="padding:8px 12px;text-align:center;color:#555;font-weight:600;border-bottom:2px solid #e0e6ef;">Godzina</th>
        <th style="padding:8px 16px;text-align:right;color:#555;font-weight:600;border-bottom:2px solid #e0e6ef;">Cena (PLN/MWh)</th>
        <th style="padding:8px 12px;text-align:right;color:#555;font-weight:600;border-bottom:2px solid #e0e6ef;">Wolumen (MWh)</th>
       </tr>
      </thead>
      <tbody>
       {table_rows}
      </tbody>
     </table>
    </td>
   </tr>

   <!-- Stopka -->
   <tr>
    <td style="background:#f7f9fc;padding:16px 32px;border-top:1px solid #eee;">
     <p style="margin:0;font-size:11px;color:#aaa;text-align:center;">
      Dane: Towarowa Giełda Energii (TGE) · TGeBase · Indeks godzinowy RDN<br>
      Raport wygenerowany automatycznie {date.today().strftime('%d.%m.%Y')} · github.com/actions
     </p>
    </td>
   </tr>

  </table>
 </td></tr>
</table>
</body>
</html>"""


# ── Wysyłka emaila ────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, chart_png: bytes):
    """Wysyła email z wykresem osadzonym inline."""
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENT

    # HTML part
    alternative = MIMEMultipart("alternative")
    msg.attach(alternative)
    alternative.attach(MIMEText(html_body, "html", "utf-8"))

    # Wykres inline
    img = MIMEImage(chart_png, "png")
    img.add_header("Content-ID", "<chart_image>")
    img.add_header("Content-Disposition", "inline", filename="rdn_chart.png")
    msg.attach(img)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, RECIPIENT, msg.as_string())
        print(f"✓ Email wysłany do: {RECIPIENT}")


# ── Główna logika ─────────────────────────────────────────────────────────────

def main():
    print("Pobieranie danych z TGE...")
    try:
        delivery_date, avg_pln, rows = fetch_tge_data()
    except Exception as e:
        print(f"✗ Błąd pobierania danych: {e}")
        sys.exit(1)

    if not rows:
        print("✗ Brak danych godzinowych (TGE mogło jeszcze nie opublikować wyników).")
        sys.exit(1)

    print(f"✓ Pobrano {len(rows)} godzin. Średnia TGeBase: {avg_pln} PLN/MWh")

    print("Generowanie wykresu...")
    chart_png = generate_chart_png(rows, delivery_date)

    print("Budowanie emaila...")
    html = build_html_email(rows, avg_pln, delivery_date)

    subject = (
        f"⚡ Ceny energii RDN | {delivery_date.strftime('%d.%m.%Y')} "
        f"| śr. {avg_pln:.0f} PLN/MWh"
    )

    print(f"Wysyłanie emaila ({SMTP_HOST}:{SMTP_PORT})...")
    try:
        send_email(subject, html, chart_png)
    except Exception as e:
        print(f"✗ Błąd wysyłki emaila: {e}")
        sys.exit(1)

    print("✓ Gotowe!")


if __name__ == "__main__":
    main()
