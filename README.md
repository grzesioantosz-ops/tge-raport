# ⚡ TGE RDN – Dzienny raport email

Automatyczny raport wysyłany codziennie emailem z cenami energii elektrycznej
z Towarowej Giełdy Energii (TGeBase / RDN). Działa na GitHub Actions — **bez serwera, za darmo**.

---

## Co zawiera raport?

- 📊 **Wykres słupkowy** — 24 godziny dostawy z kolorowaniem przedziałów cenowych
- 📋 **Tabela cen godzinowych** — cena i wolumen dla każdej godziny
- 🔢 **Karty statystyk** — średnia, szczyt 7–22h, min, max

---

## Konfiguracja krok po kroku (15 minut)

### Krok 1 — Utwórz repozytorium GitHub

1. Zaloguj się na [github.com](https://github.com) (konto bezpłatne)
2. Kliknij **"New repository"**
3. Nadaj nazwę np. `tge-raport-email`
4. Zaznacz **"Private"** (raport tylko dla Ciebie)
5. Kliknij **"Create repository"**

### Krok 2 — Wgraj pliki

Wgraj do repozytorium oba pliki:
- `report.py`
- `.github/workflows/daily_report.yml`
- `requirements.txt`

Możesz to zrobić przez **"Add file → Upload files"** w interfejsie GitHub.

> ⚠️ Folder `.github/workflows/` musi istnieć dokładnie w tej postaci.

### Krok 3 — Hasło aplikacji Gmail

Raport wysyła email przez Gmail. Musisz wygenerować **hasło aplikacji** (nie zwykłe hasło):

1. Zaloguj się na swoje konto Google
2. Wejdź na: **myaccount.google.com → Bezpieczeństwo → Weryfikacja dwuetapowa** (włącz jeśli nie masz)
3. Wróć do Bezpieczeństwo → wyszukaj **"Hasła do aplikacji"**
4. Utwórz nowe hasło dla aplikacji "Poczta" → skopiuj 16-znakowy kod

### Krok 4 — Dodaj sekrety GitHub

W repozytorium GitHub:
**Settings → Secrets and variables → Actions → New repository secret**

Dodaj 5 sekretów:

| Nazwa | Wartość (przykład) |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `twoj.email@gmail.com` |
| `SMTP_PASS` | `abcd efgh ijkl mnop` ← hasło aplikacji |
| `RECIPIENT` | `twoj.email@gmail.com` |

> Możesz ustawić `RECIPIENT` na inny adres — raport zostanie wysłany tam.

### Krok 5 — Uruchom ręcznie (test)

1. Wejdź w repozytorium → zakładka **"Actions"**
2. Kliknij **"Dzienny raport cen energii TGE"**
3. Kliknij **"Run workflow"** → **"Run workflow"**
4. Po chwili sprawdź skrzynkę email!

---

## Harmonogram automatyczny

Raport wysyłany jest codziennie o **~15:30 czasu polskiego** (po publikacji Fixing I przez TGE).

Aby zmienić godzinę, edytuj linię w `.github/workflows/daily_report.yml`:
```yaml
- cron: "30 13 * * *"   # UTC — odpowiada 14:30 UTC = 15:30 PL (lato) / 14:30 PL (zima)
```

Generator wyrażeń cron: [crontab.guru](https://crontab.guru)

---

## Uruchamianie lokalnie (opcjonalne)

```bash
pip install -r requirements.txt

export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=twoj@gmail.com
export SMTP_PASS="abcd efgh ijkl mnop"
export RECIPIENT=twoj@gmail.com

python report.py
```

Na Windows (PowerShell):
```powershell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="twoj@gmail.com"
$env:SMTP_PASS="abcd efgh ijkl mnop"
$env:RECIPIENT="twoj@gmail.com"
python report.py
```

---

## Inne skrzynki (Outlook, Yahoo itp.)

| Dostawca | SMTP_HOST | SMTP_PORT |
|---|---|---|
| Gmail | smtp.gmail.com | 587 |
| Outlook/Hotmail | smtp.office365.com | 587 |
| Yahoo | smtp.mail.yahoo.com | 587 |
| Onet | smtp.poczta.onet.pl | 465 |

Dla Outlook/Onet hasło aplikacji może być opcjonalne — wystarczy zwykłe hasło.

---

## Dane źródłowe

- **TGE TGeBase** — średnia ważona ze wszystkich transakcji RDN (Fixing I + II + ciągłe)
- Publikacja codziennie ok. 14:00–15:00 dla dostawy dnia następnego
- Jednostki: PLN/MWh (netto, bez kosztów dystrybucji i podatków)

---

## Rozwiązywanie problemów

**Email nie dochodzi:**
- Sprawdź folder Spam
- Upewnij się, że hasło aplikacji jest poprawne (bez spacji)
- Sprawdź logi w zakładce Actions → kliknij na przebieg workflow

**Brak danych (TGE nie opublikowało):**
- Fixing I publikowany jest ok. 10:30–11:00, pełne dane TGeBase po 14:00
- W weekendy i święta dane mogą być opóźnione

**GitHub Actions nie uruchamia się automatycznie:**
- Repozytorium musi mieć przynajmniej jeden commit po dodaniu pliku workflow
- Sprawdź czy Actions są włączone: Settings → Actions → General → Allow all actions

---

*Dane z Towarowej Giełdy Energii S.A. (TGE) · tge.pl*
