#!/usr/bin/env python3
"""
POSZUKIWACZ STEALI v2 – skaner błędów cenowych i mega okazji + bot Telegram.

Co robi w każdym przebiegu:
 1. PROMOCJE NAJPIERW – sam znajduje w każdym sklepie strony „promocje / wyprzedaż / outlet /
    okazje / krótkie daty / black friday”, przechodzi ich kolejne strony i sprawdza te produkty jako pierwsze.
 2. CAŁY SKLEP – resztę produktów bierze z mapy strony (sitemap) i sprawdza po kolei w kółko,
    z pierwszeństwem dla produktów, które sklep właśnie zmienił lub dodał.
 3. 5 METOD WYKRYWANIA:
      • cena dużo niższa niż ZWYKLE w tym sklepie (własna historia cen),
      • ten sam EAN dużo taniej niż w pozostałych sklepach,
      • błąd wariantu – większe opakowanie tańsze niż mniejsze,
      • weryfikacja na Ceneo (tylko podejrzane produkty),
      • Twoja lista obserwowanych (/obserwuj dior sauvage 250).
 4. PEPPER – pilnuje kategorii „Błąd cenowy” i nowych okazji z Twoimi słowami kluczowymi.
 5. Liczy zysk z odsprzedaży i wysyła alert na Telegram.

Uruchomienie:
  python skaner.py            – jeden przebieg (GitHub)
  python skaner.py --petla    – w kółko (telefon / Termux)
  python skaner.py --test     – testowa wiadomość na Telegram
"""
import argparse
import gzip
import html as htmllib
import json
import os
import random
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlparse, urldefrag

import requests
import yaml

KATALOG = os.path.dirname(os.path.abspath(__file__))
DANE = os.path.join(KATALOG, "dane")
DZIEN = 86400


def TERAZ():
    return int(time.time())


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

NIE_PRODUKT = re.compile(
    r"/(blog|poradnik|porady|artykul|artykuly|news|kategori|category|categories|marki|marka|brands?|"
    r"producenci|producent|szukaj|search|koszyk|cart|konto|account|login|logowanie|rejestracja|regulamin|"
    r"kontakt|contact|pomoc|help|faq|cms|tag|tags|o-nas|about|sklepy|apteki|wishlist|"
    r"compare|porownaj|newsletter|kariera|polityka|zwroty|dostawa|inspiracje?|ulotki|gazetka)(/|$|\?|-)", re.I)
NIE_PLIK = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|css|js|xml|ico|mp4)(\?|$)", re.I)
BLOKADA = re.compile(r"cf-chl|cf_chl|challenge-platform|access denied|attention required|are you a robot|"
                     r"jesteś robotem|request unsuccessful|incapsula|perimeterx|px-captcha|"
                     r"_Incapsula_Resource|datadome|captcha-delivery", re.I)
SLOWA_PROMO = re.compile(r"promoc|wyprzeda|outlet|okazj|sale\b|/sale|black.?friday|cyber|przecen|rabat|"
                         r"hot.?deal|last.?minute|krótkie.?daty|krotkie.?daty|super.?cen|taniej|"
                         r"limited.?offer|oferta.?limit|mega", re.I)
PAGINACJA = re.compile(r"[?&](page|p|strona|str|pg|pageNumber|pageId)=\d+|[/_-](page|strona|str)?[/_-]?\d{1,3}/?$", re.I)
ML = re.compile(r"(\d{1,4}(?:[.,]\d)?)\s*(ml|g)\b", re.I)


# ---------------------------------------------------------------- narzędzia
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def tak(v):
    return str(v).strip().lower() in ("tak", "true", "yes", "1", "on")


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "sklep"


def cena_z_tekstu(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = re.sub(r"[^\d,.]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def zl(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " zł"


def esc(t):
    return htmllib.escape(str(t or ""))


def wczytaj_json(sciezka, domyslne):
    try:
        if sciezka.endswith(".gz"):
            with gzip.open(sciezka, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(sciezka, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return domyslne


def zapisz_json(sciezka, dane):
    os.makedirs(os.path.dirname(sciezka), exist_ok=True)
    tmp = sciezka + ".tmp"
    if sciezka.endswith(".gz"):
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=1)
    os.replace(tmp, sciezka)


def linki(tekst, baza):
    """Wszystkie linki ze strony: [(adres, tekst_linku)]."""
    wynik = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", tekst, re.S | re.I):
        h = re.search(r'href\s*=\s*["\']([^"\'#]+)', m.group(1), re.I)
        if not h:
            continue
        adr = urldefrag(urljoin(baza, htmllib.unescape(h.group(1).strip())))[0]
        if adr.startswith("http"):
            opis = re.sub(r"<[^>]+>", " ", m.group(2))
            opis += " " + " ".join(re.findall(r'(?:title|aria-label)\s*=\s*["\']([^"\']+)', m.group(1), re.I))
            wynik.append((adr, re.sub(r"\s+", " ", htmllib.unescape(opis)).strip()))
    return wynik


def ten_sam_sklep(url, adres):
    return urlparse(adres).netloc.replace("www.", "") in urlparse(url).netloc


# ---------------------------------------------------------------- pobieranie
class Klient:
    """Grzeczny klient: osobne tempo dla każdego serwisu, sam zwalnia, gdy serwis protestuje."""

    def __init__(self, przerwa):
        self.bazowa = przerwa
        self.przerwy, self.ostatnio, self.blokady, self.zamki = {}, {}, {}, {}
        self.glowny = threading.Lock()
        self.lokalny = threading.local()

    def _sesja(self):
        s = getattr(self.lokalny, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
                "Accept-Encoding": "gzip, deflate",
            })
            self.lokalny.s = s
        return s

    def _czekaj(self, host):
        with self.glowny:
            zamek = self.zamki.setdefault(host, threading.Lock())
        with zamek:
            przerwa = self.przerwy.get(host, self.bazowa)
            ile = self.ostatnio.get(host, 0) + przerwa * random.uniform(0.8, 1.3) - time.time()
            if ile > 0:
                time.sleep(ile)
            self.ostatnio[host] = time.time()

    def zglos_blokade(self, host):
        with self.glowny:
            self.blokady[host] = self.blokady.get(host, 0) + 1
            self.przerwy[host] = min(self.przerwy.get(host, self.bazowa) * 2, 20)

    def zablokowany(self, host):
        return self.blokady.get(host, 0) >= 8

    def get(self, url, binarnie=False, proby=3):
        host = urlparse(url).netloc
        for proba in range(proby):
            if self.zablokowany(host):
                return None
            self._czekaj(host)
            try:
                r = self._sesja().get(url, timeout=25, allow_redirects=True)
            except requests.RequestException:
                time.sleep(2 + proba * 3)
                continue
            if r.status_code in (403, 429, 503):
                self.zglos_blokade(host)
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(int(ra), 60) if ra.isdigit() else 5 + proba * 5)
                continue
            with self.glowny:
                self.blokady[host] = 0
                if self.przerwy.get(host, self.bazowa) > self.bazowa:
                    self.przerwy[host] = max(self.bazowa, self.przerwy[host] * 0.9)
            if r.status_code != 200:
                return None
            if binarnie:
                return r.content
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        return None


# ---------------------------------------------------------------- mapa strony
def znajdz_sitemapy(k, adres):
    txt = k.get(adres.rstrip("/") + "/robots.txt") or ""
    mapy = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)
    if not mapy:
        mapy = [adres.rstrip("/") + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml")]
    return list(dict.fromkeys(mapy))


LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", re.S)


def czytaj_sitemap(k, url, wynik, odwiedzone, glebokosc=0):
    if url in odwiedzone or glebokosc > 3 or len(odwiedzone) > 400:
        return
    odwiedzone.add(url)
    dane = k.get(url, binarnie=True)
    if not dane:
        return
    if dane[:2] == b"\x1f\x8b":
        try:
            dane = gzip.decompress(dane)
        except OSError:
            return
    tekst = dane.decode("utf-8", "replace")
    if "<sitemapindex" in tekst[:3000].lower():
        dzieci = [htmllib.unescape(x.strip()) for x in LOC.findall(tekst)]
        produktowe = [d for d in dzieci if re.search(r"produ|product|item|offer|artyku|towar", d, re.I)
                      and not re.search(r"categor|kategor|blog|brand|marki|image|cms", d, re.I)]
        for d in (produktowe or dzieci)[:300]:
            czytaj_sitemap(k, d, wynik, odwiedzone, glebokosc + 1)
        return
    for blok in re.findall(r"<url>(.*?)</url>", tekst, re.S):
        m = LOC.search(blok)
        if m:
            lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", blok, re.S)
            wynik[htmllib.unescape(m.group(1).strip())] = lm.group(1).strip() if lm else ""


def wyglada_na_produkt(url, adres, filtr):
    p = urlparse(url)
    if not p.path or p.path == "/" or NIE_PLIK.search(url) or NIE_PRODUKT.search(p.path):
        return False
    if not ten_sam_sklep(url, adres):
        return False
    return not filtr or filtr.lower() in url.lower()


# ---------------------------------------------------------------- odczyt produktu
def _obiekty(d):
    if isinstance(d, list):
        for x in d:
            yield from _obiekty(x)
    elif isinstance(d, dict):
        yield d
        for kl in ("@graph", "hasVariant", "mainEntity", "itemListElement", "item"):
            if kl in d:
                yield from _obiekty(d[kl])


def _typ(o):
    t = o.get("@type", "")
    return " ".join(t) if isinstance(t, list) else str(t)


def _ean(o):
    for kl in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "ean"):
        v = o.get(kl)
        if v:
            v = re.sub(r"\D", "", str(v))
            if 8 <= len(v) <= 14:
                return v.zfill(13) if len(v) == 12 else v
    return ""


def pojemnosc(tekst):
    m = ML.search(tekst or "")
    return float(m.group(1).replace(",", ".")) if m else None


def wyciagnij_produkty(tekst):
    """Lista wariantów ze strony produktu: dict(nazwa, ean, cena, dostepny, wariant, sku)."""
    wynik = []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", tekst, re.S | re.I):
        surowe = m.group(1).strip()
        try:
            dane = json.loads(surowe)
        except ValueError:
            try:
                dane = json.loads(re.sub(r"[\x00-\x1f]", " ", surowe))
            except ValueError:
                continue
        for o in _obiekty(dane):
            if "Product" not in _typ(o):
                continue
            nazwa = htmllib.unescape(str(o.get("name", "")).strip())
            ean_prod = _ean(o)
            oferty = o.get("offers") or []
            if isinstance(oferty, dict):
                oferty = oferty["offers"] if "AggregateOffer" in _typ(oferty) and oferty.get("offers") else [oferty]
            for of in oferty if isinstance(oferty, list) else []:
                if not isinstance(of, dict):
                    continue
                cena = cena_z_tekstu(of.get("price") or of.get("lowPrice"))
                if cena is None and isinstance(of.get("priceSpecification"), (dict, list)):
                    ps = of["priceSpecification"]
                    ceny = [cena_z_tekstu(x.get("price")) for x in (ps if isinstance(ps, list) else [ps])
                            if isinstance(x, dict)]
                    ceny = [c for c in ceny if c]
                    cena = min(ceny) if ceny else None
                if cena is None:
                    continue
                if str(of.get("priceCurrency", "PLN")).upper() not in ("PLN", "ZŁ", "ZL", ""):
                    continue
                wynik.append({
                    "nazwa": nazwa,
                    "ean": _ean(of) or ean_prod,
                    "cena": round(cena, 2),
                    "dostepny": not re.search(r"OutOfStock|SoldOut|Discontinued", str(of.get("availability", "")), re.I),
                    "wariant": str(of.get("name") or "").strip(),
                    "sku": str(of.get("sku") or o.get("sku") or "").strip(),
                })
    if not wynik:   # zapas: znaczniki meta / microdata
        cena = None
        for wz in (r'<meta[^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([^"\']+)',
                   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)',
                   r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)'):
            m = re.search(wz, tekst, re.I)
            if m and cena_z_tekstu(m.group(1)):
                cena = cena_z_tekstu(m.group(1))
                break
        if cena:
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', tekst, re.I) \
                or re.search(r"<title>(.*?)</title>", tekst, re.S | re.I)
            e = re.search(r'itemprop=["\']gtin1?3?["\'][^>]*content=["\'](\d{8,14})', tekst, re.I)
            wynik.append({"nazwa": htmllib.unescape(m.group(1).strip()) if m else "", "ean": e.group(1) if e else "",
                          "cena": round(cena, 2), "dostepny": True, "wariant": "", "sku": ""})
    uniq, widziane = [], set()
    for w in wynik:
        kl = (w["ean"], w["sku"], w["wariant"], w["cena"])
        if kl not in widziane:
            widziane.add(kl)
            uniq.append(w)
    # warianty bez EAN/SKU, które dublują ofertę zbiorczą rodzica – wyrzuć
    if len(uniq) > 1 and any(w["ean"] or w["sku"] for w in uniq):
        uniq = [w for w in uniq if w["ean"] or w["sku"] or w["wariant"]]
    return uniq


def bledy_wariantow(produkty):
    """Większe opakowanie tańsze niż mniejsze = typowy błąd cenowy. Zwraca {index: cena_odniesienia}."""
    z_ml = [(i, pojemnosc(p["wariant"] or p["nazwa"]), p["cena"]) for i, p in enumerate(produkty) if p["dostepny"]]
    z_ml = [x for x in z_ml if x[1]]
    wynik = {}
    for i, ml, cena in z_ml:
        mniejsze = [(m2, c2) for j, m2, c2 in z_ml if m2 < ml and j != i]
        if not mniejsze:
            continue
        m2, c2 = max(mniejsze)             # najbliższe mniejsze opakowanie
        if cena < c2 * 0.95:
            wynik[i] = round(c2 * (ml / m2) * 0.75, 2)   # ostrożnie: większe zwykle ~25% taniej za ml
    return wynik


# ---------------------------------------------------------------- historia
def zwykla_cena(hist, teraz):
    dzis = teraz // DZIEN
    ceny = [c for d, c in hist if dzis - 45 <= d < dzis]
    return statistics.median(ceny) if ceny else None


def dopisz_historie(item, cena, teraz):
    dzis = teraz // DZIEN
    h = item.setdefault("h", [])
    if h and h[-1][0] == dzis:
        h[-1][1] = min(h[-1][1], cena)
    else:
        h.append([dzis, cena])
    del h[:-40]


# ---------------------------------------------------------------- Telegram
class Telegram:
    def __init__(self, token, stan, max_odb):
        self.token, self.stan, self.max = token, stan, max_odb

    def _api(self, metoda, **p):
        if not self.token:
            return None
        try:
            return requests.post(f"https://api.telegram.org/bot{self.token}/{metoda}", data=p, timeout=30).json()
        except Exception as e:
            log("Telegram błąd:", e)
            return None

    def wyslij(self, tekst, do=None):
        for chat in ([do] if do else list(self.stan.setdefault("odbiorcy", []))):
            r = self._api("sendMessage", chat_id=chat, text=tekst[:4000], parse_mode="HTML",
                          disable_web_page_preview="true")
            if r and not r.get("ok") and "parse" in str(r.get("description", "")).lower():
                self._api("sendMessage", chat_id=chat, text=re.sub(r"<[^>]+>", "", tekst)[:4000])
            time.sleep(0.35)

    def komendy(self, obsluga):
        r = self._api("getUpdates", offset=self.stan.get("offset", 0), timeout=0)
        if not r or not r.get("ok"):
            return
        for u in r.get("result", []):
            self.stan["offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            tekst = (msg.get("text") or "").strip()
            if chat and tekst.startswith("/"):
                obsluga(chat, tekst.split()[0].split("@")[0].lower(), tekst)


POMOC = ("<b>Komendy</b>\n"
         "/status – co skanuję i czy coś blokuje\n"
         "/top – najlepsze okazje z 48 h\n"
         "/obserwuj dior sauvage 100 250 – alarm, gdy produkt z tymi słowami będzie ≤ 250 zł\n"
         "/lista – Twoje obserwowane\n"
         "/usun 2 – usuń pozycję nr 2 z listy\n"
         "/cisza 8 – wycisz alerty na 8 godzin\n"
         "/stop – wypisz mnie")


# ---------------------------------------------------------------- główna logika
class Skaner:
    def __init__(self, cfg, miejsce):
        self.cfg, self.miejsce = cfg, miejsce
        self.a = cfg.get("alarm", {}) or {}
        self.sp = cfg.get("sprzedaz", {}) or {}
        self.sk = cfg.get("skan", {}) or {}
        self.k = Klient(float(self.sk.get("przerwa_s", 0.6)))
        self.stan = wczytaj_json(os.path.join(DANE, "stan.json"), {})
        tg = cfg.get("telegram", {}) or {}
        token = os.environ.get("TELEGRAM_TOKEN") or str(tg.get("token") or "").strip()
        self.tg = Telegram(token, self.stan, int(tg.get("max_odbiorcow", 2)))
        for c in os.environ.get("TELEGRAM_CHAT", "").split(","):
            if c.strip().lstrip("-").isdigit() and int(c) not in self.stan.setdefault("odbiorcy", []):
                self.stan["odbiorcy"].append(int(c))
        self.sklepy = [s for s in cfg.get("sklepy", []) or [] if tak(s.get("wlaczony", "tak"))
                       and str(s.get("gdzie", "wszedzie")).lower() in ("wszedzie", "wszędzie", miejsce)]
        self.dane = {}
        self.koniec = 0
        self.ceneo_licznik = 0

    # ---------- komendy bota
    def obsluz(self, chat, cmd, tekst):
        odb = self.stan.setdefault("odbiorcy", [])
        arg = tekst.split()[1:]
        if cmd == "/start":
            if chat in odb:
                self.tg.wyslij("✅ Już jesteś zapisany.\n\n" + POMOC, chat)
            elif len(odb) < self.tg.max:
                odb.append(chat)
                self.tg.wyslij("✅ <b>Poszukiwacz Steali działa!</b>\nBędę tu wysyłać błędy cenowe i mega okazje.\n\n"
                               + POMOC, chat)
            else:
                self.tg.wyslij("⛔ Ten bot jest prywatny.", chat)
            return
        if chat not in odb:
            return
        obs = self.stan.setdefault("obserwowane", [])
        if cmd == "/stop":
            odb.remove(chat)
            self.tg.wyslij("👋 Wypisano. Wróć przez /start", chat)
        elif cmd == "/status":
            self.tg.wyslij(self.raport_status(), chat)
        elif cmd == "/top":
            self.tg.wyslij(self.raport_top(), chat)
        elif cmd == "/obserwuj":
            cena = cena_z_tekstu(arg[-1]) if arg else None
            slowa = [w.lower() for w in arg[:-1]] if cena else []
            if not cena or not slowa:
                self.tg.wyslij("Użycie: /obserwuj <słowa> <maks. cena>\nnp. /obserwuj dior sauvage 100 250", chat)
            else:
                obs.append({"s": slowa, "c": cena})
                self.tg.wyslij(f"🎯 Dodano: <b>{esc(' '.join(slowa))}</b> ≤ {zl(cena)}", chat)
        elif cmd == "/lista":
            self.tg.wyslij("🎯 <b>Obserwowane</b>\n" + ("\n".join(
                f"{i + 1}. {esc(' '.join(o['s']))} ≤ {zl(o['c'])}" for i, o in enumerate(obs)) or "(pusto)"), chat)
        elif cmd == "/usun":
            try:
                o = obs.pop(int(arg[0]) - 1)
                self.tg.wyslij(f"🗑 Usunięto: {esc(' '.join(o['s']))}", chat)
            except (IndexError, ValueError):
                self.tg.wyslij("Użycie: /usun <numer z /lista>", chat)
        elif cmd == "/cisza":
            godz = cena_z_tekstu(arg[0]) if arg else 8
            self.stan["cisza_do"] = TERAZ() + int((godz or 8) * 3600)
            self.tg.wyslij(f"🤫 Wyciszono na {int(godz or 8)} h. (Zbieram okazje – zobaczysz je w /top)", chat)
        else:
            self.tg.wyslij(POMOC, chat)

    def raport_status(self):
        st = self.stan.get("statystyki", {})
        linie = ["📊 <b>Status</b>"]
        for nazwa, s in sorted(st.items()):
            ikona = "🔴" if s.get("blokada") else ("🟡" if not s.get("produktow") else "🟢")
            linie.append(f"{ikona} <b>{esc(nazwa)}</b>: {s.get('produktow', 0)} prod., "
                         f"promocji {s.get('promo', 0)}, teraz {s.get('sprawdzone', 0)} stron ({s.get('miejsce', '')})")
            if s.get("blokada"):
                linie.append("   ⚠️ blokuje – ustaw mu <i>gdzie: telefon</i>")
            elif not s.get("produktow") and s.get("sprawdzone"):
                linie.append("   ⚠️ nie widzę cen – wyślij zrzut tego statusu do Claude")
        p = self.stan.get("pepper_status")
        if p:
            linie.append(f"{'🟢' if p == 'ok' else '🔴'} <b>Pepper</b>: {esc(p)}")
        if self.stan.get("ostatni_przebieg"):
            linie.append(f"\n🕒 Ostatni przebieg: {time.strftime('%d.%m %H:%M', time.localtime(self.stan['ostatni_przebieg']))}")
        linie.append(f"🔔 Alertów łącznie: {self.stan.get('alertow', 0)}")
        if self.stan.get("cisza_do", 0) > TERAZ():
            linie.append("🤫 Alerty wyciszone")
        return "\n".join(linie)

    def raport_top(self):
        lista = [x for x in self.stan.get("ostatnie", []) if x["t"] >= TERAZ() - 2 * DZIEN]
        lista.sort(key=lambda x: -x["proc"])
        if not lista:
            return "Na razie brak okazji z ostatnich 48 h. Cierpliwości 🙂"
        return "🏆 <b>Najlepsze okazje (48 h)</b>\n" + "\n".join(
            f"• -{x['proc']}% <a href=\"{esc(x['url'])}\">{esc(x['nazwa'][:60])}</a> – {zl(x['cena'])} ({esc(x['sklep'])})"
            for x in lista[:12])

    # ---------- dane sklepu
    def plik(self, sklep):
        return os.path.join(DANE, slug(sklep["nazwa"]) + ".json.gz")

    def wczytaj_sklep(self, sklep):
        d = wczytaj_json(self.plik(sklep), {})
        for kl, v in (("urls", {}), ("kursor", 0), ("sitemap_ts", 0), ("nie", []), ("p", {}), ("nie_ts", TERAZ()),
                      ("pilne", []), ("promo_listy", []), ("promo_ts", 0), ("promo_kursor", 0)):
            d.setdefault(kl, v)
        return d

    def odswiez_liste(self, sklep, d):
        if d["urls"] and TERAZ() - d["sitemap_ts"] < 6 * 3600:
            return []
        log(f"[{sklep['nazwa']}] czytam mapę strony…")
        nowe, odw = {}, set()
        for mapa in znajdz_sitemapy(self.k, sklep["adres"]):
            czytaj_sitemap(self.k, mapa, nowe, odw)
        nie = set(d["nie"])
        nowe = {u: lm for u, lm in nowe.items() if u not in nie and wyglada_na_produkt(u, sklep["adres"], sklep.get("filtr"))}
        if not nowe:
            log(f"[{sklep['nazwa']}] brak mapy strony – jadę na promocjach i linkach ze stron")
            d["sitemap_ts"] = TERAZ() - 5 * 3600
            return []
        zmienione = []
        if d["urls"]:
            zmienione = [u for u, lm in nowe.items() if lm and d["urls"].get(u) not in (None, lm)]
            zmienione += [u for u in nowe if u not in d["urls"]]
        d["urls"], d["sitemap_ts"] = nowe, TERAZ()
        log(f"[{sklep['nazwa']}] {len(nowe)} produktów w mapie, {len(zmienione)} zmienionych/nowych")
        return zmienione

    # ---------- promocje / wyprzedaże / outlet
    def znajdz_listy_promo(self, sklep, d):
        """Sam szuka na stronie głównej linków do promocji, wyprzedaży, outletu itd. (raz na dobę)."""
        reczne = [u for u in (sklep.get("promocje") or []) if u]
        if d["promo_listy"] and TERAZ() - d["promo_ts"] < DZIEN:
            return list(dict.fromkeys(reczne + d["promo_listy"]))
        tekst = self.k.get(sklep["adres"]) or ""
        znalezione = {}
        for adr, opis in linki(tekst, sklep["adres"]):
            if not ten_sam_sklep(adr, sklep["adres"]) or NIE_PLIK.search(adr):
                continue
            if re.search(r"blog|poradnik|regulamin|kontakt|faq|pomoc|newsletter|gazetk", adr, re.I):
                continue
            pkt = (2 if SLOWA_PROMO.search(urlparse(adr).path) else 0) + (1 if SLOWA_PROMO.search(opis) else 0)
            if pkt:
                znalezione[adr] = max(znalezione.get(adr, 0), pkt)
        najlepsze = sorted(znalezione, key=lambda u: (-znalezione[u], len(u)))[:int(self.sk.get("promocje_max_list", 8))]
        d["promo_listy"], d["promo_ts"] = najlepsze, TERAZ()
        if najlepsze:
            log(f"[{sklep['nazwa']}] strony promocji: " + ", ".join(urlparse(u).path for u in najlepsze))
        return list(dict.fromkeys(reczne + najlepsze))

    def produkty_z_list(self, sklep, d, listy):
        """Przechodzi strony promocji (z kolejnymi stronami) i zbiera linki do produktów."""
        znane = d["urls"]
        nie = set(d["nie"])
        max_stron = int(self.sk.get("promocje_max_stron", 30))
        produkty = []
        for lista in listy:
            kolejka, byly = [lista], set()
            while kolejka and len(byly) < max_stron and time.time() < self.koniec - 300:
                url = kolejka.pop(0)
                if url in byly:
                    continue
                byly.add(url)
                tekst = self.k.get(url)
                if not tekst:
                    continue
                baza_listy = re.sub(r"([/_-](page|strona|str)?[/_-]?\d{1,3}/?|[?].*)$", "", urlparse(lista).path)
                for adr, _ in linki(tekst, url):
                    if not ten_sam_sklep(adr, sklep["adres"]) or adr in nie:
                        continue
                    sciezka = urlparse(adr).path
                    if PAGINACJA.search(adr) and sciezka.startswith(baza_listy) and adr not in byly:
                        kolejka.append(adr)
                    elif adr in znane or (not znane and wyglada_na_produkt(adr, sklep["adres"], sklep.get("filtr"))
                                          and re.search(r"\d|-.*-", sciezka) and adr != lista
                                          and not SLOWA_PROMO.search(sciezka)):
                        produkty.append(adr)
        return list(dict.fromkeys(produkty))

    def kolejka(self, sklep, d, zmienione, promo):
        budzet = int(self.sk.get("produktow_na_przebieg", 1500))
        sprawdzone = {}
        for it in d["p"].values():
            sprawdzone[it["u"]] = max(sprawdzone.get(it["u"], 0), it.get("t", 0))
        # 1) promocje – najdawniej sprawdzane najpierw (do 60% budżetu)
        promo = sorted(promo, key=lambda u: sprawdzone.get(u, 0))
        q = [u for u in promo if TERAZ() - sprawdzone.get(u, 0) > 1800][: int(budzet * 0.6)]
        # 2) zmienione w mapie strony i nowości, 3) produkty, które ostatnio taniały
        pilne = list(dict.fromkeys(d["pilne"] + zmienione))
        gorace = [it["u"] for it in d["p"].values() if it.get("gor", 0) > TERAZ() - 3 * DZIEN]
        q += pilne[: budzet // 4] + gorace[: budzet // 6]
        d["pilne"] = pilne[budzet // 4:][:20000]
        q = list(dict.fromkeys(q))[:budzet]
        # 4) reszta – cały sklep po kolei, w kółko
        urls = list(d["urls"].keys())
        if urls and len(q) < budzet:
            start = d["kursor"] % len(urls)
            rot = (urls[start:] + urls[:start])[: budzet - len(q)]
            d["kursor"] = (start + len(rot)) % len(urls)
            q += rot
        return list(dict.fromkeys(q))

    # ---------- skan jednego sklepu
    def skanuj_sklep(self, sklep):
        nazwa = sklep["nazwa"]
        d = self.wczytaj_sklep(sklep)
        self.dane[nazwa] = d
        host = urlparse(sklep["adres"]).netloc
        try:
            zmienione = self.odswiez_liste(sklep, d)
        except Exception as e:
            log(f"[{nazwa}] błąd mapy strony: {e!r}")
            zmienione = []
        promo = []
        if tak(self.sk.get("promocje_najpierw", "tak")):
            try:
                promo = self.produkty_z_list(sklep, d, self.znajdz_listy_promo(sklep, d))
                log(f"[{nazwa}] produktów na promocjach: {len(promo)}")
            except Exception as e:
                log(f"[{nazwa}] błąd promocji: {e!r}")
        tylko_promo = tak(sklep.get("tylko_promocje", "nie"))
        q = promo[: int(self.sk.get("produktow_na_przebieg", 1500))] if tylko_promo else self.kolejka(sklep, d, zmienione, promo)
        promo_set = set(promo)
        sprawdzone, kandydaci, lock, nie = [0], [], threading.Lock(), set(d["nie"])
        min_cena = float(self.a.get("min_cena_zl", 8))

        def jeden(url):
            if time.time() > self.koniec or self.k.zablokowany(host):
                return
            tekst = self.k.get(url)
            if tekst is None:
                return
            produkty = wyciagnij_produkty(tekst)
            teraz = TERAZ()
            with lock:
                sprawdzone[0] += 1
                if not produkty:
                    if BLOKADA.search(tekst[:40000]) and len(tekst) < 200000:
                        self.k.zglos_blokade(host)
                    elif url not in promo_set:
                        nie.add(url)
                    return
                wielu = len(produkty) > 1
                warianty = bledy_wariantow(produkty) if wielu else {}
                for i, pr in enumerate(produkty):
                    klucz = url + ("#" + (pr["ean"] or pr["sku"] or str(i)) if wielu else "")
                    it = d["p"].setdefault(klucz, {"u": url})
                    nz = pr["nazwa"]
                    if wielu and pr["wariant"] and pr["wariant"].lower() not in nz.lower():
                        nz += f" – {pr['wariant']}"
                    it.update({"n": nz[:160], "e": pr["ean"], "t": teraz, "a": pr["dostepny"]})
                    stara = it.get("c")
                    zwykla = zwykla_cena(it.get("h", []), teraz)
                    it["c"] = pr["cena"]
                    if pr["dostepny"]:
                        dopisz_historie(it, pr["cena"], teraz)
                    if stara and pr["cena"] < stara * 0.9:
                        it["gor"] = teraz
                    if pr["dostepny"] and pr["cena"] >= min_cena:
                        kandydaci.append({"sklep": nazwa, "klucz": klucz, "it": it, "zwykla": zwykla,
                                          "wariant": warianty.get(i), "promo": url in promo_set})

        with ThreadPoolExecutor(max_workers=int(self.sk.get("rownolegle", 3))) as ex:
            list(ex.map(jeden, q))
        d["nie"] = list(nie)[-50000:]
        blok = self.k.zablokowany(host)
        log(f"[{nazwa}] sprawdzono {sprawdzone[0]} stron, kandydatów {len(kandydaci)}" + (" ⚠️ BLOKADA" if blok else ""))
        self.stan.setdefault("statystyki", {})[nazwa] = {
            "produktow": len(d["p"]), "sprawdzone": sprawdzone[0], "blokada": blok,
            "miejsce": self.miejsce, "promo": len(promo)}
        return kandydaci

    # ---------- Ceneo
    def ceneo(self, ean):
        cache = self.stan.setdefault("ceneo", {})
        c = cache.get(ean)
        if c and TERAZ() - c[1] < DZIEN:
            return c[0]
        if self.ceneo_licznik <= 0 or self.k.zablokowany("www.ceneo.pl"):
            return None
        self.ceneo_licznik -= 1
        tekst = self.k.get(f"https://www.ceneo.pl/;szukaj-{quote(ean)}", proby=1)
        ceny = None
        if tekst:
            lst = [p["cena"] for p in wyciagnij_produkty(tekst)]
            lst += [cena_z_tekstu(x) for x in re.findall(r'data-price="([\d.,]+)"', tekst)]
            lst += [cena_z_tekstu(a + "." + b) for a, b in re.findall(
                r'class="value">\s*([\d\s]+)\s*</span>\s*<span class="penny">\s*,?(\d{2})', tekst)]
            ceny = sorted({round(x, 2) for x in lst if x})[:30] or None
        cache[ean] = [ceny, TERAZ()]
        if len(cache) > 5000:
            for kl in sorted(cache, key=lambda x: cache[x][1])[:1000]:
                cache.pop(kl)
        return ceny

    # ---------- ocena
    def ocen(self, kandydaci):
        granica = TERAZ() - 4 * DZIEN
        indeks = {}
        for nazwa, d in self.dane.items():
            for it in d["p"].values():
                if it.get("e") and it.get("a") and it.get("t", 0) > granica and it.get("c"):
                    indeks.setdefault(it["e"], []).append((nazwa, it["c"], it["u"]))
        p_hist = float(self.a.get("spadek_vs_historia_proc", 35)) / 100
        p_inne = float(self.a.get("taniej_niz_inne_sklepy_proc", 25)) / 100
        p_ceneo = float(self.a.get("taniej_niz_ceneo_proc", 25)) / 100
        obs = self.stan.get("obserwowane", [])
        alerty = []
        for k in kandydaci:
            it, cena = k["it"], k["it"]["c"]
            k.update(sygnaly=[], odn=[], cel=None)
            if k["zwykla"] and cena <= k["zwykla"] * (1 - p_hist):
                k["sygnaly"].append("historia")
                k["odn"].append(k["zwykla"])
            inne = [(s, c, u) for s, c, u in indeks.get(it.get("e"), []) if s != k["sklep"]]
            k["inne"] = sorted(inne, key=lambda x: x[1])[:3]
            k["med_inne"] = statistics.median([c for _, c, _ in inne]) if inne else None
            if k["med_inne"] and cena <= k["med_inne"] * (1 - p_inne):
                k["sygnaly"].append("inne")
                k["odn"].append(k["med_inne"])
            if k["wariant"] and cena <= k["wariant"] * (1 - p_inne):
                k["sygnaly"].append("wariant")
                k["odn"].append(k["wariant"])
            nazwa_l = (it.get("n") or "").lower()
            for o in obs:
                if cena <= o["c"] and all(s in nazwa_l for s in o["s"]):
                    k["cel"] = o
            if k["sygnaly"] or k["cel"]:
                alerty.append(k)
        # Ceneo tylko dla podejrzanych, najpierw największe różnice
        self.ceneo_licznik = int((self.cfg.get("ceneo") or {}).get("max_zapytan_na_przebieg", 25))
        alerty.sort(key=lambda x: -(1 - x["it"]["c"] / max(x["odn"] or [x["it"]["c"]])))
        for al in alerty:
            al["ceneo"] = None
            if tak((self.cfg.get("ceneo") or {}).get("sprawdzaj", "tak")) and al["it"].get("e"):
                ceny = self.ceneo(al["it"]["e"]) or []
                wyzsze = [c for c in ceny if c > al["it"]["c"] * 1.02]   # pomijamy naszą własną ofertę na Ceneo
                al["ceneo"] = wyzsze[0] if wyzsze else None
                if al["ceneo"] and al["it"]["c"] <= al["ceneo"] * (1 - p_ceneo):
                    al["sygnaly"].append("ceneo")
                    al["odn"].append(al["ceneo"])
        return alerty

    def wyslij_alerty(self, alerty):
        prow = float(self.sp.get("prowizja_proc", 11)) / 100
        wys = float(self.sp.get("koszt_wysylki_zl", 12))
        min_zysk = float(self.a.get("min_zysk_zl", 15))
        prog_bledu = float(self.a.get("blad_cenowy_od_proc", 60))
        limit = int(self.a.get("max_wiadomosci_na_przebieg", 15))
        wyslane = self.stan.setdefault("wyslane", {})
        ostatnie = self.stan.setdefault("ostatnie", [])
        teraz = TERAZ()
        cisza = self.stan.get("cisza_do", 0) > teraz
        gotowe = []
        for al in alerty:
            it, cena = al["it"], al["it"]["c"]
            rynek = [x for x in (al["ceneo"], al["med_inne"], al["zwykla"], al["wariant"]) if x]
            if al["sygnaly"]:
                if al["ceneo"] and "ceneo" not in al["sygnaly"] and cena > al["ceneo"] * 0.9:
                    al["sygnaly"] = []          # Ceneo: gdzie indziej jest podobnie tanio – to nie okazja
                else:
                    zysk = min(rynek) * (1 - prow) - cena - wys
                    if min_zysk > 0 and zysk < min_zysk:
                        al["sygnaly"] = []
            if not al["sygnaly"] and not al["cel"]:
                continue
            proc = round((1 - cena / min(al["odn"])) * 100) if al["odn"] else 0
            zysk = (min(rynek) * (1 - prow) - cena - wys) if rynek else None
            poprz = wyslane.get(al["klucz"])
            if poprz and cena >= poprz[0] * 0.95 and (teraz - poprz[1] < 3 * DZIEN or not al["sygnaly"]):
                continue                        # już wysłane (obserwowane – tylko gdy cena znów spadnie)
            gotowe.append((proc, zysk or 0, al))
        gotowe.sort(key=lambda x: (-x[0], -x[1]))
        for proc, zysk, al in gotowe[:limit]:
            it = al["it"]
            if al["sygnaly"]:
                nag = "🚨 <b>PRAWDOPODOBNY BŁĄD CENOWY</b>" if proc >= prog_bledu else "🔥 <b>MEGA OKAZJA</b>"
                nag += f"  -{proc}%"
            else:
                nag = "🎯 <b>OBSERWOWANY PRODUKT</b>"
            linie = [nag, f"<b>{esc(it.get('n', '')[:150])}</b>", f"🏪 {esc(al['sklep'])}: <b>{zl(it['c'])}</b>"
                     + ("  🏷 promocja" if al["promo"] else "")]
            if al["zwykla"]:
                linie.append(f"📉 Zwykle w tym sklepie: {zl(al['zwykla'])}")
            if "wariant" in al["sygnaly"]:
                linie.append(f"📦 Mniejsze opakowanie jest droższe! (oczekiwana ok. {zl(al['wariant'])})")
            for s, c, _ in al["inne"]:
                linie.append(f"🏬 {esc(s)}: {zl(c)}")
            if al["ceneo"]:
                linie.append(f"🔎 Ceneo (inne oferty) od: {zl(al['ceneo'])}")
            if al["cel"]:
                linie.append(f"🎯 Twój próg: {esc(' '.join(al['cel']['s']))} ≤ {zl(al['cel']['c'])}")
            if zysk and al["sygnaly"]:
                linie.append(f"💰 Szac. zysk przy odsprzedaży: <b>~{zl(zysk)}</b>/szt.")
            if it.get("e"):
                linie.append(f"🏷 EAN: <code>{it['e']}</code>")
            linie.append(f"👉 <a href=\"{esc(it['u'])}\">KUP / otwórz</a>"
                         + (f" | <a href=\"https://www.ceneo.pl/;szukaj-{it['e']}\">Ceneo</a>" if it.get("e") else ""))
            linie.append("⚡ Błędy znikają szybko – kupuj od razu, sprawdź cenę w koszyku.")
            if not cisza:
                self.tg.wyslij("\n".join(linie))
            wyslane[al["klucz"]] = [it["c"], teraz]
            ostatnie.append({"t": teraz, "proc": proc, "url": it["u"], "nazwa": it.get("n", ""),
                             "cena": it["c"], "sklep": al["sklep"]})
            self.stan["alertow"] = self.stan.get("alertow", 0) + 1
            log(f"ALERT -{proc}% {it.get('n', '')[:60]} {zl(it['c'])}")
        if len(gotowe) > limit and not cisza:
            self.tg.wyslij(f"…i jeszcze {len(gotowe) - limit} okazji. Wpisz /top")
        self.stan["ostatnie"] = [x for x in ostatnie if x["t"] > teraz - 7 * DZIEN][-300:]
        self.stan["wyslane"] = {k: v for k, v in wyslane.items() if v[1] > teraz - 14 * DZIEN}

    # ---------- Pepper
    def pepper(self):
        pc = self.cfg.get("pepper") or {}
        if not tak(pc.get("wlaczony", "tak")):
            return
        slowa = [s.lower() for s in (pc.get("slowa") or [])]
        widziane = self.stan.setdefault("pepper_ids", [])
        pierwszy_raz = not self.stan.get("pepper_start")
        zrodla = [("blad", "https://www.pepper.pl/rss/grupa/blad-cenowy"),
                  ("blad", "https://www.pepper.pl/grupa/blad-cenowy"),
                  ("nowe", "https://www.pepper.pl/rss/nowe"),
                  ("nowe", "https://www.pepper.pl/nowe")]
        wpisy, ok = {}, set()
        for typ, url in zrodla:
            if typ in ok:
                continue                      # RSS zadziałał – strona HTML niepotrzebna
            tekst = self.k.get(url, proby=1)
            if not tekst:
                continue
            znalezione = 0
            for item in re.findall(r"<item>(.*?)</item>", tekst, re.S):
                t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
                l = re.search(r"<link>(.*?)</link>", item, re.S)
                if t and l:
                    m = re.search(r"-(\d+)/?$", l.group(1).strip().split("?")[0])
                    if m:
                        wpisy.setdefault(m.group(1), (typ, htmllib.unescape(t.group(1).strip()), l.group(1).strip()))
                        znalezione += 1
            if not znalezione:
                for adr, opis in linki(tekst, url):
                    m = re.search(r"pepper\.pl/promocje/[^/?#]+-(\d+)/?$", adr)
                    if m and len(opis) > 10:
                        wpisy.setdefault(m.group(1), (typ, opis[:200], adr))
                        znalezione += 1
            if znalezione or "<rss" in tekst[:500] or "<channel" in tekst[:2000]:
                ok.add(typ)
        self.stan["pepper_status"] = "ok" if ok else "niedostępny z tego miejsca"
        if ok:
            self.stan["pepper_start"] = True
        for pid, (typ, tytul, adr) in sorted(wpisy.items()):
            if pid in widziane:
                continue
            widziane.append(pid)
            if pierwszy_raz:
                continue                      # pierwszy przebieg – tylko zapamiętaj, nie spamuj
            tl = tytul.lower()
            pasuje = [s for s in slowa if s in tl]
            pasuje += [" ".join(o["s"]) for o in self.stan.get("obserwowane", []) if all(w in tl for w in o["s"])]
            if typ == "blad" or "błąd cenowy" in tl or "blad cenowy" in tl or pasuje:
                ikona = "🚨" if typ == "blad" or "błąd" in tl else "📢"
                if self.stan.get("cisza_do", 0) <= TERAZ():
                    self.tg.wyslij(f"{ikona} <b>Pepper</b>{' (' + esc(', '.join(pasuje)) + ')' if pasuje else ''}\n"
                                   f"{esc(tytul)}\n👉 <a href=\"{esc(adr)}\">Otwórz</a>")
        del widziane[:-1500]

    # ---------- jeden przebieg
    def przebieg(self):
        start = time.time()
        self.koniec = start + float(self.sk.get("limit_minut", 20)) * 60
        self.tg.komendy(self.obsluz)
        try:
            self.pepper()
        except Exception as e:
            log("Pepper błąd:", repr(e))
        kandydaci = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.sklepy))) as ex:
            for wynik in ex.map(self._bezpiecznie, self.sklepy):
                kandydaci += wynik
        for s in self.cfg.get("sklepy", []) or []:       # do porównań EAN także sklepy skanowane gdzie indziej
            if s["nazwa"] not in self.dane and os.path.exists(self.plik(s)):
                self.dane[s["nazwa"]] = self.wczytaj_sklep(s)
        self.wyslij_alerty(self.ocen(kandydaci))
        for s in self.sklepy:
            if s["nazwa"] in self.dane:
                self.sprzataj(self.dane[s["nazwa"]])
                zapisz_json(self.plik(s), self.dane[s["nazwa"]])
        self.tg.komendy(self.obsluz)
        self.stan["ostatni_przebieg"] = TERAZ()
        zapisz_json(os.path.join(DANE, "stan.json"), self.stan)
        log(f"Gotowe w {int(time.time() - start)} s")

    def _bezpiecznie(self, sklep):
        try:
            return self.skanuj_sklep(sklep)
        except Exception as e:
            log(f"[{sklep['nazwa']}] błąd: {e!r}")
            return []

    @staticmethod
    def sprzataj(d):
        granica = TERAZ() - 30 * DZIEN
        d["p"] = {k: v for k, v in d["p"].items() if v.get("t", 0) > granica}
        if TERAZ() - d.get("nie_ts", 0) > 7 * DZIEN:     # raz w tygodniu "nie-produkty" dostają drugą szansę
            d["nie"], d["nie_ts"] = [], TERAZ()


def wczytaj_cfg():
    with open(os.path.join(KATALOG, "sklepy.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--petla", action="store_true", help="działaj w kółko (telefon)")
    ap.add_argument("--test", action="store_true", help="wyślij testową wiadomość")
    args = ap.parse_args()
    miejsce = os.environ.get("MIEJSCE") or ("telefon" if args.petla else "github")
    if args.test:
        s = Skaner(wczytaj_cfg(), miejsce)
        s.tg.komendy(s.obsluz)
        if not s.stan.get("odbiorcy"):
            print("Nikt jeszcze nie napisał /start do bota. Napisz /start i uruchom ponownie.")
        s.tg.wyslij("🧪 Test: bot działa i może do Ciebie pisać.")
        zapisz_json(os.path.join(DANE, "stan.json"), s.stan)
        return
    if not args.petla:
        Skaner(wczytaj_cfg(), miejsce).przebieg()
        return
    log("Tryb pętli – Ctrl+C aby zatrzymać")
    while True:
        cfg = wczytaj_cfg()
        s = Skaner(cfg, miejsce)
        try:
            s.przebieg()
        except Exception as e:
            log("Błąd przebiegu:", repr(e))
        nastepny = time.time() + float((cfg.get("skan") or {}).get("telefon_co_minut", 15)) * 60
        while time.time() < nastepny:      # w międzyczasie szybko odpowiadamy na komendy
            time.sleep(20)
            try:
                s.tg.komendy(s.obsluz)
                zapisz_json(os.path.join(DANE, "stan.json"), s.stan)
            except Exception:
                pass


if __name__ == "__main__":
    main()
