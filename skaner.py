#!/usr/bin/env python3
"""
POSZUKIWACZ STEALI v4 – skaner błędów cenowych i mega okazji + bot Telegram.

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
from collections import deque
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



def _produkty_ld(dane):
    """(id_grupy, obiekt Product, nazwa_rodzica). Warianty z hasVariant dostają grupę rodzica."""
    grupa = [0]
    lista = [False]

    def chodz(d):
        if isinstance(d, list):
            for x in d:
                yield from chodz(x)
        elif isinstance(d, dict):
            t = _typ(d)
            if "ItemList" in t or "OfferCatalog" in t:
                lista[0] = True
            if "Product" in t:
                grupa[0] += 1
                g = grupa[0]
                yield g, d, ""
                for w in d.get("hasVariant") or []:
                    if isinstance(w, dict):
                        yield g, w, str(d.get("name", ""))
            for kl in ("@graph", "mainEntity", "itemListElement", "item"):
                if kl in d:
                    yield from chodz(d[kl])

    wynik = list(chodz(dane))
    return wynik, lista[0]


def analizuj_strone(tekst, adres=""):
    """{'produkty': [...], 'lista': bool}. Lista = strona kategorii/wyszukiwania z wieloma różnymi produktami."""
    wynik, lista = [], False
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", tekst, re.S | re.I):
        surowe = m.group(1).strip()
        try:
            dane = json.loads(surowe)
        except ValueError:
            try:
                dane = json.loads(re.sub(r"[\x00-\x1f]", " ", surowe))
            except ValueError:
                continue
        obiekty, czy_lista = _produkty_ld(dane)
        lista = lista or czy_lista
        for g, o, rodzic in obiekty:
            nazwa = htmllib.unescape(str(o.get("name") or rodzic).strip())
            if rodzic and rodzic.lower() not in nazwa.lower():
                nazwa = f"{htmllib.unescape(rodzic)} {nazwa}"
            ean_prod = _ean(o)
            url_prod = str(o.get("url") or "")
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
                    "grupa": g, "nazwa": nazwa,
                    "ean": _ean(of) or ean_prod,
                    "cena": round(cena, 2),
                    "dostepny": not re.search(r"OutOfStock|SoldOut|Discontinued", str(of.get("availability", "")), re.I),
                    "wariant": str(of.get("name") or "").strip(),
                    "sku": str(of.get("sku") or o.get("sku") or "").strip(),
                    "url": urljoin(adres, str(of.get("url") or url_prod)) if (of.get("url") or url_prod) else "",
                    "zbiorcza": "lowPrice" in of and not of.get("price"),
                })
    if not wynik:   # zapas: znaczniki meta / microdata (zawsze pojedynczy produkt)
        cena = None
        for wz in (r'<meta[^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([^"\']+)',
                   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)'):
            m = re.search(wz, tekst, re.I)
            if m and cena_z_tekstu(m.group(1)):
                cena = cena_z_tekstu(m.group(1))
                break
        if cena:
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', tekst, re.I) \
                or re.search(r"<title>(.*?)</title>", tekst, re.S | re.I)
            e = re.search(r'itemprop=["\']gtin1?3?["\'][^>]*content=["\'](\d{8,14})', tekst, re.I)
            wynik.append({"grupa": 1, "nazwa": htmllib.unescape(m.group(1).strip()) if m else "",
                          "ean": e.group(1) if e else "", "cena": round(cena, 2), "dostepny": True,
                          "wariant": "", "sku": "", "url": "", "zbiorcza": False})
    uniq, widziane = [], set()
    for w in wynik:
        kl = (w["ean"], w["sku"], w["wariant"], w["cena"], w["nazwa"])
        if kl not in widziane:
            widziane.add(kl)
            uniq.append(w)
    # oferta zbiorcza rodzica ("od 99 zł") obok konkretnych wariantów – wyrzuć
    if any(not w["zbiorcza"] for w in uniq):
        uniq = [w for w in uniq if not w["zbiorcza"]]
    # wiele RÓŻNYCH produktów (różne grupy i różne nazwy) = strona listy, nie produkt
    grupy = {}
    for w in uniq:
        grupy.setdefault(w["grupa"], set()).add(re.sub(r"[^a-z]", "", w["nazwa"].lower())[:25])
    nazwy = set().union(*grupy.values()) if grupy else set()
    if len(grupy) >= 3 or (len(grupy) == 2 and len(nazwy) >= 2):
        lista = True
    return {"produkty": uniq, "lista": lista}


def wyciagnij_produkty(tekst):
    return analizuj_strone(tekst)["produkty"]


def bledy_wariantow(produkty):
    """Większe opakowanie TEGO SAMEGO produktu tańsze niż mniejsze. Zwraca {index: cena_odniesienia}."""
    wynik = {}
    for g in {p["grupa"] for p in produkty}:
        z_ml = []
        for i, p in enumerate(produkty):
            if p["grupa"] != g or not p["dostepny"]:
                continue
            m = ML.search(p["wariant"] or "") or ML.search(p["nazwa"] or "")
            if m:
                z_ml.append((i, float(m.group(1).replace(",", ".")), m.group(2).lower(), p["cena"]))
        if len({x[1] for x in z_ml}) < 2:
            continue
        for i, ml, jed, cena in z_ml:
            mniejsze = [(m2, c2) for j, m2, j2, c2 in z_ml if j != i and j2 == jed and 5 <= m2 < ml and ml / m2 <= 10]
            if not mniejsze:
                continue
            m2, c2 = max(mniejsze)
            if cena < c2 * 0.9:
                wynik[i] = round(c2 * 1.05, 2)    # ostrożnie: większe nie powinno być tańsze niż mniejsze
    return wynik


TYP_ZAPACHU = [(r"eau de parfum|woda perfumowana|\bedp\b", "edp"), (r"eau de toilette|woda toaletowa|\bedt\b", "edt"),
               (r"eau de cologne|woda kolo[nń]ska|\bedc\b", "edc"), (r"extrait|ekstrakt perfum|(?<!de )\bparfum\b(?! de)", "parfum"),
               (r"\btester\b|bez pude[lł]ka|unboxed", "tester"), (r"zestaw|\bset\b|gift|kpl\.|komplet", "zestaw"),
               (r"refill|uzupe[lł]nienie|wk[lł]ad", "refill"), (r"pr[oó]bka|\bmini\b|miniatur", "mini")]
NIEISTOTNE = set("woda perfumowana toaletowa dla kobiet mezczyzn meski damski damska meska unisex spray "
                 "atomizer ml g szt sztuk flakon perfumy perfum eau de parfum toilette cologne the and for "
                 "her him women men pour homme femme z i w na do edp edt edc tester zestaw set gift extrait refill mini "
                 "probka kpl komplet bez pudelka unboxed".split())


def klucz_nazwy(nazwa):
    """Klucz porównania bez EAN: marka+nazwa+typ+POJEMNOŚĆ (100 ml porównuje się tylko ze 100 ml)."""
    t = (nazwa or "").lower()
    t = t.translate(str.maketrans("ąćęłńóśźżéèêàâôöüû", "acelnoszzeeeaaoouu"))
    m = ML.search(t)
    if not m:
        return None
    ml = f"{float(m.group(1).replace(',', '.')):g}" + m.group(2)
    flagi = sorted({f for wz, f in TYP_ZAPACHU if re.search(wz, t)})
    slowa = sorted({w for w in re.findall(r"[a-z0-9]{2,}", ML.sub(" ", t)) if w not in NIEISTOTNE and not w.isdigit()})
    if len(slowa) < 2:
        return None
    return " ".join(slowa) + "|" + ml + "|" + ",".join(flagi)


def za_100(cena, nazwa):
    m = ML.search(nazwa or "")
    if not m:
        return ""
    il = float(m.group(1).replace(",", "."))
    return f" ({zl(cena / il * 100)}/100 {m.group(2).lower()})" if il >= 5 else ""


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
        self.zamek = threading.Lock()

    def _api(self, metoda, **p):
        if not self.token:
            return None
        try:
            return requests.post(f"https://api.telegram.org/bot{self.token}/{metoda}", data=p, timeout=30).json()
        except Exception as e:
            log("Telegram błąd:", e)
            return None

    def wyslij(self, tekst, do=None, przyciski=None, podglad=None):
        """przyciski: [(tekst, url), ...] – duże klikalne przyciski pod wiadomością.
        podglad: adres strony, której podgląd (zdjęcie produktu) ma pokazać Telegram."""
        extra = {}
        if przyciski:
            rzedy = [[{"text": t, "url": u}] for t, u in przyciski[:1]]
            if len(przyciski) > 1:
                rzedy.append([{"text": t, "url": u} for t, u in przyciski[1:3]])
            extra["reply_markup"] = json.dumps({"inline_keyboard": rzedy})
        if podglad:
            extra["link_preview_options"] = json.dumps({"url": podglad, "prefer_small_media": True, "show_above_text": False})
        else:
            extra["disable_web_page_preview"] = "true"
        with self.zamek:
            for chat in ([do] if do else list(self.stan.setdefault("odbiorcy", []))):
                r = self._api("sendMessage", chat_id=chat, text=tekst[:4000], parse_mode="HTML", **extra)
                if r and not r.get("ok"):
                    if r.get("error_code") == 429:   # Telegram prosi o wolniej
                        time.sleep(int((r.get("parameters") or {}).get("retry_after", 3)) + 1)
                    extra.pop("link_preview_options", None)
                    self._api("sendMessage", chat_id=chat, text=re.sub(r"<[^>]+>", "", tekst)[:4000],
                              disable_web_page_preview="true",
                              **({"reply_markup": extra["reply_markup"]} if "reply_markup" in extra else {}))
                time.sleep(0.05)

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
        if self.stan.get("v") != 3:
            for kl in ("wyslane", "ostatnie", "ceneo"):
                self.stan.pop(kl, None)
            self.stan["v"] = 3
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
        self.ilock = threading.RLock()          # wspólny indeks cen i licznik alertów
        self.po_ean, self.po_nazwie = {}, {}
        self.na_sklep, self.pominiete, self.wyslanych = {}, 0, 0

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
        c = self.stan.get("ceneo_status")
        if c:
            linie.append(f"{'🟢' if c == 'ok' else '🔴'} <b>Ceneo</b>: {esc(c)}")
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
            f"{i + 1}. <b>−{x['proc']}%</b>  {zl(x['cena'])}  ·  {esc(x['sklep'])}\n    <a href=\"{esc(x['url'])}\">{esc(x['nazwa'][:70])}</a>"
            for i, x in enumerate(lista[:12]))

    # ---------- dane sklepu
    def plik(self, sklep):
        return os.path.join(DANE, slug(sklep["nazwa"]) + ".json.gz")

    def wczytaj_sklep(self, sklep):
        d = wczytaj_json(self.plik(sklep), {})
        if d.get("v") != 3:
            d = {"urls": d.get("urls", {}), "sitemap_ts": d.get("sitemap_ts", 0), "kursor": d.get("kursor", 0), "v": 3}
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
        d = self.dane.get(nazwa) or self.wczytaj_sklep(sklep)
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
        budzet = int(self.sk.get("produktow_na_przebieg", 2500))
        q = promo[:budzet] if tak(sklep.get("tylko_promocje", "nie")) else self.kolejka(sklep, d, zmienione, promo)
        promo_set = set(promo)
        kolejka, zrobione = deque(q), set()
        sprawdzone, lock, nie = [0], threading.Lock(), set(d["nie"])
        min_cena = float(self.a.get("min_cena_zl", 8))
        cena_url = {it["u"]: it.get("c") for it in d["p"].values()}

        def jeden(url):
            tekst = self.k.get(url)
            if tekst is None:
                return
            strona = analizuj_strone(tekst, url)
            produkty = strona["produkty"]
            teraz = TERAZ()
            nowi = []
            with lock:
                sprawdzone[0] += 1
                if strona["lista"]:
                    # strona kategorii: ceny z listy tylko jako wskazówka – produkty, które wyraźnie
                    # potaniały, sprawdzamy NATYCHMIAST (na początek kolejki), resztę później
                    for pr in produkty:
                        u = pr["url"]
                        if not u or u == url or not ten_sam_sklep(u, sklep["adres"]) or u in zrobione:
                            continue
                        stara = cena_url.get(u)
                        if stara and pr["cena"] < stara * 0.85:
                            kolejka.appendleft(u)
                        else:
                            d["pilne"].append(u)
                    if url not in promo_set:
                        nie.add(url)
                    return
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
                    wlasny = pr["url"] if pr["url"] and ten_sam_sklep(pr["url"], sklep["adres"]) else url
                    it = d["p"].setdefault(klucz, {"u": wlasny})
                    it["u"] = wlasny
                    nz = pr["nazwa"]
                    if wielu and pr["wariant"] and pr["wariant"].lower() not in nz.lower():
                        nz += f" – {pr['wariant']}"
                    it.update({"n": nz[:160], "e": pr["ean"], "t": teraz, "a": pr["dostepny"], "k": klucz_nazwy(nz)})
                    stara = it.get("c")
                    zwykla = zwykla_cena(it.get("h", []), teraz)
                    it["c"] = pr["cena"]
                    if pr["dostepny"]:
                        dopisz_historie(it, pr["cena"], teraz)
                    if stara and pr["cena"] < stara * 0.9:
                        it["gor"] = teraz
                    if pr["dostepny"] and pr["cena"] >= min_cena:
                        dni = len([1 for dz, _ in it.get("h", []) if dz < teraz // DZIEN])
                        nowi.append({"sklep": nazwa, "klucz": klucz, "it": it, "zwykla": zwykla, "dni": dni,
                                     "wariant": warianty.get(i), "promo": url in promo_set, "strona": url})
            for k in nowi:                                    # ocena i alert od razu, bez czekania na koniec
                self.dodaj_do_indeksu(nazwa, k["it"])
                al = self.ocen_jeden(k)
                if al:
                    self.wyslij_jeden(al)

        def pracownik():
            while time.time() < self.koniec and not self.k.zablokowany(host):
                with lock:
                    while kolejka and kolejka[0] in zrobione:
                        kolejka.popleft()
                    if not kolejka:
                        return
                    url = kolejka.popleft()
                    zrobione.add(url)
                try:
                    jeden(url)
                except Exception as e:
                    log(f"[{nazwa}] błąd strony {url}: {e!r}")

        watki = [threading.Thread(target=pracownik) for _ in range(int(self.sk.get("rownolegle", 4)))]
        for w in watki:
            w.start()
        for w in watki:
            w.join()
        d["nie"] = list(nie)[-50000:]
        blok = self.k.zablokowany(host)
        log(f"[{nazwa}] sprawdzono {sprawdzone[0]} stron" + (" ⚠️ BLOKADA" if blok else ""))
        with self.ilock:
            self.stan.setdefault("statystyki", {})[nazwa] = {
                "produktow": len(d["p"]), "sprawdzone": sprawdzone[0], "blokada": blok,
                "miejsce": self.miejsce, "promo": len(promo)}
        return []

    def ceneo(self, ean):
        cache = self.stan.setdefault("ceneo", {})
        c = cache.get(ean)
        if c and TERAZ() - c[1] < DZIEN:
            return c[0]
        with self.ilock:
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
        self.stan["ceneo_status"] = "ok" if tekst else "niedostępne z tego miejsca"
        if len(cache) > 5000:
            for kl in sorted(cache, key=lambda x: cache[x][1])[:1000]:
                cache.pop(kl)
        return ceny

    # ---------- ocena

    def dodaj_do_indeksu(self, sklep, it):
        if not (it.get("a") and it.get("c") and it.get("t", 0) > TERAZ() - 4 * DZIEN):
            return
        o = (sklep, it["c"], it["u"])
        with self.ilock:
            if it.get("e"):
                self.po_ean.setdefault(it["e"], {})[sklep + "|" + it["u"]] = o
            if it.get("k"):
                self.po_nazwie.setdefault(it["k"], {})[sklep + "|" + it["u"]] = o

    def ocen_jeden(self, k):
        """Cena rynkowa = mediana 3 najtańszych ofert TEGO SAMEGO produktu (EAN, a bez EAN: nazwa + pojemność)
        w innych sklepach. Zwraca alert albo None. Alert wymaga ≥ 2 punktów pewności."""
        it, cena = k["it"], k["it"]["c"]
        p_hist = float(self.a.get("spadek_vs_historia_proc", 35)) / 100
        p_inne = float(self.a.get("taniej_niz_inne_sklepy_proc", 25)) / 100
        k.update(sygnaly=[], odn=[], cel=None, pkt=0, ceneo=None)
        with self.ilock:
            oferty = list((self.po_ean.get(it.get("e")) or {}).values()) if it.get("e") else []
            k["dopasowanie"] = "ten sam EAN" if oferty else "nazwa + pojemność"
            if not oferty:
                oferty = list((self.po_nazwie.get(it.get("k")) or {}).values())
        najtansze = {}
        for s_, c, u in oferty:
            if s_ != k["sklep"] and (s_ not in najtansze or c < najtansze[s_][0]):
                najtansze[s_] = (c, u)
        inne = sorted((c, s_, u) for s_, (c, u) in najtansze.items())
        k["inne"] = [(s_, c, u) for c, s_, u in inne[:3]]
        k["rynek"] = statistics.median([c for c, _, _ in inne[:3]]) if inne else None
        if k["rynek"] and cena <= k["rynek"] * (1 - p_inne):
            k["sygnaly"].append("inne sklepy")
            k["odn"].append(k["rynek"])
            k["pkt"] += 2 if len(inne) >= 2 else 1
        if k["zwykla"] and k["dni"] >= 2 and cena <= k["zwykla"] * (1 - p_hist):
            k["sygnaly"].append("historia ceny")
            k["odn"].append(k["zwykla"])
            k["pkt"] += 2 if k["dni"] >= 5 else 1
        if k["wariant"] and cena <= k["wariant"] * (1 - p_inne):
            k["sygnaly"].append("mniejsze opakowanie droższe")
            k["odn"].append(k["wariant"])
            k["pkt"] += 1
        nazwa_l = (it.get("n") or "").lower()
        for o in self.stan.get("obserwowane", []):
            if cena <= o["c"] and all(w in nazwa_l for w in o["s"]):
                k["cel"] = o
        if not k["pkt"] and not k["cel"]:
            return None
        if k["pkt"] >= 1 and it.get("e") and tak((self.cfg.get("ceneo") or {}).get("sprawdzaj", "tak")):
            ceny = self.ceneo(it["e"]) or []
            wyzsze = [c for c in ceny if c > cena * 1.02]          # bez naszej własnej oferty
            k["ceneo"] = statistics.median(wyzsze[:3]) if wyzsze else None
            if k["ceneo"] and cena <= k["ceneo"] * (1 - float(self.a.get("taniej_niz_ceneo_proc", 25)) / 100):
                k["sygnaly"].append("Ceneo")
                k["odn"].append(k["ceneo"])
                k["pkt"] += 2
        return k

    def wyslij_jeden(self, al):
        """Decyzja + natychmiastowa wysyłka jednego alertu."""
        it, cena = al["it"], al["it"]["c"]
        teraz = TERAZ()
        prow = float(self.sp.get("prowizja_proc", 11)) / 100
        wys = float(self.sp.get("koszt_wysylki_zl", 12))
        min_zysk = float(self.a.get("min_zysk_zl", 15))
        if al["pkt"] < 2:
            al["sygnaly"] = []
        rynek = [x for x in (al["ceneo"], al["rynek"], al["zwykla"] if al["dni"] >= 2 else None) if x]
        zysk = (min(rynek) * (1 - prow) - cena - wys) if rynek else None
        if al["sygnaly"]:
            if al["ceneo"] and "Ceneo" not in al["sygnaly"] and cena > al["ceneo"] * 0.9:
                al["sygnaly"] = []                 # na Ceneo jest podobnie tanio – to nie okazja
            elif min_zysk > 0 and (zysk or 0) < min_zysk:
                al["sygnaly"] = []
        if not al["sygnaly"] and not al["cel"]:
            return
        proc = round((1 - cena / min(al["odn"])) * 100) if al["odn"] else 0
        with self.ilock:
            wyslane = self.stan.setdefault("wyslane", {})
            poprz = wyslane.get(al["klucz"])
            if poprz and cena >= poprz[0] * 0.95 and (teraz - poprz[1] < 3 * DZIEN or not al["sygnaly"]):
                return
            n = self.na_sklep.get(al["sklep"], 0) + 1
            self.na_sklep[al["sklep"]] = n
            limit = int(self.a.get("max_wiadomosci_na_przebieg", 30))
            if n > 25:                             # zalew z jednego sklepu = zmiana strony, nie błędy
                if n == 26:
                    ost = self.stan.setdefault("ostrzezenia", {})
                    if teraz - ost.get(al["sklep"], 0) > DZIEN:
                        ost[al["sklep"]] = teraz
                        self.tg.wyslij(f"⚠️ <b>{esc(al['sklep'])}</b>: podejrzanie dużo okazji naraz – "
                                       f"wstrzymuję alerty z tego sklepu do następnego przebiegu.")
                return
            if self.wyslanych >= limit:
                self.pominiete += 1
                cisza = True
            else:
                cisza = self.stan.get("cisza_do", 0) > teraz
            wyslane[al["klucz"]] = [cena, teraz]
        if proc >= 85 and al["sygnaly"]:           # skrajna zniżka – sprawdź jeszcze raz stronę produktu
            tekst = self.k.get(al["strona"], proby=1) or ""
            if not any(abs(p["cena"] - cena) < 0.01 for p in analizuj_strone(tekst, al["strona"])["produkty"]):
                return
        nazwa = it.get("n", "")
        if al["sygnaly"]:
            gw = "★" * min(al["pkt"], 5)
            if proc >= float(self.a.get("blad_cenowy_od_proc", 60)):
                linie = [f"🚨🚨 <b>BŁĄD CENOWY  −{proc}%</b>"]
            else:
                linie = [f"🔥 <b>MEGA OKAZJA  −{proc}%</b>"]
        else:
            gw = ""
            linie = ["🎯 <b>TWÓJ OBSERWOWANY PRODUKT</b>"]
        linie += [f"<b>{esc(nazwa[:140])}</b>", "",
                  f"💸 <b>{zl(cena)}</b> w <b>{esc(al['sklep'])}</b>{za_100(cena, nazwa)}"
                  + ("  🏷 promocja" if al["promo"] else "")]
        porown = []
        if al["rynek"]:
            porown.append(f"rynek {zl(al['rynek'])}")
        if al["zwykla"] and al["dni"] >= 2:
            porown.append(f"zwykle tu {zl(al['zwykla'])}")
        if al["ceneo"]:
            porown.append(f"Ceneo {zl(al['ceneo'])}")
        if porown:
            linie.append("📊 " + "  ·  ".join(porown))
        if al["inne"]:
            linie.append("🏬 " + "  ·  ".join(f"<a href=\"{esc(u)}\">{esc(s_)}</a> {zl(c)}" for s_, c, u in al["inne"]))
        if al["sygnaly"] and zysk:
            linie.append(f"💰 Zysk przy odsprzedaży: <b>~{zl(zysk)}</b>/szt.")
        if al["cel"]:
            linie.append(f"🎯 Twój próg: {esc(' '.join(al['cel']['s']))} ≤ {zl(al['cel']['c'])}")
        if gw:
            linie.append(f"✅ Pewność {gw}  <i>({', '.join(al['sygnaly'])}"
                         + (f"; porównanie: {al['dopasowanie']}" if al["inne"] else "") + ")</i>")
        linie.append(f"⏱ {time.strftime('%H:%M', time.localtime(teraz))}  ·  sprawdź cenę w koszyku i kupuj od razu")
        przyciski = [("🛒  OTWÓRZ PRODUKT  →", it["u"])]
        if it.get("e"):
            przyciski.append(("🔎 Ceneo", f"https://www.ceneo.pl/;szukaj-{it['e']}"))
        if al["inne"]:
            przyciski.append((f"🏬 {al['inne'][0][0]}", al["inne"][0][2]))
        if not cisza:
            self.tg.wyslij("\n".join(linie), przyciski=przyciski, podglad=it["u"])
        with self.ilock:
            if not cisza:
                self.wyslanych += 1
            self.stan.setdefault("ostatnie", []).append(
                {"t": teraz, "proc": proc, "url": it["u"], "nazwa": nazwa, "cena": cena, "sklep": al["sklep"]})
            self.stan["alertow"] = self.stan.get("alertow", 0) + 1
        log(f"ALERT -{proc}% {nazwa[:60]} {zl(cena)} ({al['sklep']})")

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
                    self.tg.wyslij(f"{ikona} <b>PEPPER{' – BŁĄD CENOWY' if typ == 'blad' else ''}</b>"
                                   f"{'  (' + esc(', '.join(pasuje)) + ')' if pasuje else ''}\n<b>{esc(tytul)}</b>",
                                   przyciski=[("🛒  OTWÓRZ OKAZJĘ  →", adr)], podglad=adr)
        del widziane[:-1500]

    # ---------- jeden przebieg
    def przebieg(self):
        start = time.time()
        self.koniec = start + float(self.sk.get("limit_minut", 22)) * 60
        self.tg.komendy(self.obsluz)
        try:
            self.pepper()
        except Exception as e:
            log("Pepper błąd:", repr(e))
        self.ceneo_licznik = int((self.cfg.get("ceneo") or {}).get("max_zapytan_na_przebieg", 40))
        for s_ in self.cfg.get("sklepy", []) or []:          # indeks cen ze wszystkich sklepów od startu
            if os.path.exists(self.plik(s_)):
                self.dane[s_["nazwa"]] = self.wczytaj_sklep(s_)
                for it in self.dane[s_["nazwa"]]["p"].values():
                    self.dodaj_do_indeksu(s_["nazwa"], it)
        log(f"Indeks rynkowy: {len(self.po_ean)} EAN, {len(self.po_nazwie)} nazw")
        with ThreadPoolExecutor(max_workers=max(1, len(self.sklepy))) as ex:
            list(ex.map(self._bezpiecznie, self.sklepy))
        if self.pominiete:
            self.tg.wyslij(f"…i jeszcze {self.pominiete} okazji ponad limit wiadomości. Wpisz /top")
        teraz = TERAZ()
        self.stan["ostatnie"] = [x for x in self.stan.get("ostatnie", []) if x["t"] > teraz - 7 * DZIEN][-300:]
        self.stan["wyslane"] = {k: v for k, v in self.stan.get("wyslane", {}).items() if v[1] > teraz - 14 * DZIEN}
        for s_ in self.sklepy:
            if s_["nazwa"] in self.dane:
                self.sprzataj(self.dane[s_["nazwa"]])
                zapisz_json(self.plik(s_), self.dane[s_["nazwa"]])
        self.tg.komendy(self.obsluz)
        self.stan["ostatni_przebieg"] = TERAZ()
        zapisz_json(os.path.join(DANE, "stan.json"), self.stan)
        log(f"Gotowe w {int(time.time() - start)} s, wysłano {self.wyslanych}")

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
