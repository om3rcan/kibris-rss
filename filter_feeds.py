#!/usr/bin/env python3
"""Kıbrıs haber feed'lerinden spor/magazin vb. haberleri ayıklar.

feeds.toml'daki her feed'i indirir, engel kurallarına uyan <item>/<entry>
öğelerini çıkarır ve sonucu <out>/feeds/<slug>.xml olarak yazar. Ayrıca
Fluent Reader'a aktarmak için <out>/filtreli.opml, durum özeti için
<out>/rapor.json ve <out>/index.html üretir.

Bunlara ek olarak:
- <out>/feeds/birlesik.xml: tüm kaynaklardan, aynı haberin tekrarları tek
  kayda indirilmiş birleşik feed ("N kaynak" bilgisiyle).
- <out>/envanter.html: her sitede görülen kategori ve link klasörlerinin
  birikimli sayımı; engel listesini seçmek için.
Çalıştırmalar arası durum <out>/durum/*.json dosyalarında tutulur ve bir
sonraki çalıştırmada önceki yayından okunur.

Bir feed indirilemezse, daha önce yayımlanmış sürümü (GitHub Pages'teki
kopya) korunur; okuyucuda o kaynak boş görünmez.

Kullanım:
    python filter_feeds.py --out public
    python filter_feeds.py --out public --onceki onceki
    python filter_feeds.py --out public --base-url https://kullanici.github.io/repo/
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tomllib
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, unquote
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

import requests
from lxml import etree
from lxml import html as lhtml

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = 25
ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/elements/1.1/"
MEDIA = "http://search.yahoo.com/mrss/"

# --------------------------------------------------------------------------
# Metin normalizasyonu
# --------------------------------------------------------------------------
_TR = str.maketrans({
    "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
    "â": "a", "î": "i", "û": "u", "é": "e",
})


def norm(text: str | None) -> str:
    """Türkçe karakterlerden ve büyük/küçük harften bağımsız düz metin."""
    if not text:
        return ""
    text = unquote(text)
    text = text.replace("İ", "i").replace("I", "ı").lower().translate(_TR)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def slugify(name: str) -> str:
    return norm(name).replace(" ", "-") or "feed"


def build_matcher(terms: list[str]):
    """Terimlerden biriyle *başlayan* bir kelime arayan regex."""
    terms = [norm(t) for t in terms if norm(t)]
    if not terms:
        return None
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?:^|\s)(?:{alt})")


# --------------------------------------------------------------------------
# Öğe inceleme
# --------------------------------------------------------------------------
def local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def item_info(el) -> tuple[str, list[str], str]:
    """(başlık, kategoriler, link) döndürür. RSS 2.0, RSS 1.0 ve Atom."""
    title, cats, link = "", [], ""
    for ch in el:
        name = local(ch.tag)
        if name == "title":
            title = "".join(ch.itertext())
        elif name == "category":
            cats.append(ch.get("term") or ch.get("label") or "".join(ch.itertext()))
        elif name == "subject" and etree.QName(ch.tag).namespace == DC:
            cats.append("".join(ch.itertext()))
        elif name == "link" and not link:
            href = ch.get("href")
            if href is not None:
                if ch.get("rel", "alternate") == "alternate":
                    link = href
            else:
                link = "".join(ch.itertext()).strip()
    if not link:
        guid = el.find("guid")
        if guid is not None and (guid.text or "").startswith("http"):
            link = guid.text.strip()
    return title, [c for c in cats if c], link


def link_dirs(link: str) -> list[str]:
    """Linkin klasör bölümleri (son parça, yani haber slug'ı hariç)."""
    path = urlsplit(link).path
    parts = [p for p in path.split("/") if p]
    return parts[:-1] if len(parts) > 1 else []


_TAGS = re.compile(r"<[^>]+>")
_IMG = re.compile(r"""<img[^>]+src=["']([^"']+)""", re.I)
_STOP = set("ile icin olarak daha gibi olan oldu dedi sonra kadar".split())


def parse_date(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def plain(text: str) -> str:
    """HTML'den düz metin (bazı siteler HTML'i iki kez kaçışlıyor)."""
    text = html.unescape(_TAGS.sub(" ", html.unescape(text or "")))
    return re.sub(r"\s+", " ", text).strip()


def stems(text: str, limit: int | None = None) -> list[str]:
    """Kelime kökleri: Türkçe ekler yüzünden ilk 5 harf karşılaştırılır."""
    words = [w[:5] for w in norm(text).split() if len(w) > 2 and w not in _STOP]
    return words[:limit] if limit else words


def signature(title: str, text: str) -> dict:
    """Benzerlik imzası: başlık kökleri (b) ve metnin 3'lü kelime parçaları (m)."""
    w = stems(text, 80)
    shingles = {zlib.crc32(" ".join(w[i:i + 3]).encode()) for i in range(len(w) - 2)}
    return {"b": sorted(set(stems(title))), "m": sorted(shingles)}


def item_record(el, title: str, cats: list[str], link: str) -> dict:
    """Envanter ve birleşik feed için öğenin özeti."""
    guid = desc = content = img = date = ""
    for ch in el:
        name, ns = local(ch.tag), etree.QName(ch.tag).namespace if isinstance(ch.tag, str) else None
        if ns == MEDIA and name in ("content", "thumbnail"):
            img = img or ch.get("url", "")
        elif name in ("guid", "id"):
            guid = guid or "".join(ch.itertext()).strip()
        elif name in ("description", "summary"):
            desc = desc or "".join(ch.itertext())
        elif name in ("encoded", "content"):
            content = content or "".join(ch.itertext())
        elif name in ("pubDate", "published", "updated", "date"):
            date = date or "".join(ch.itertext())
        elif name == "enclosure" and (ch.get("type") or "").startswith("image"):
            img = img or ch.get("url", "")
    if not img:
        m = _IMG.search(html.unescape(desc + content))
        img = m.group(1) if m else ""
    text = plain(desc) or plain(content)
    title = plain(title)
    d = parse_date(date)
    return {
        "anahtar": guid or link or title,
        "baslik": title,
        "link": link,
        "kategoriler": [c.strip() for c in cats],
        "klasorler": [p for p in link_dirs(link) if not p.isdigit()],
        "tarih": d.astimezone(timezone.utc).isoformat() if d else None,
        "ozet": text if len(text) <= 400 else text[:400].rsplit(" ", 1)[0] + " …",
        "gorsel": img,
        "imza": signature(title, plain(content) or text),
        "atildi": False,
    }


def drop_boilerplate(records: list[dict]):
    """Aynı sitenin çoğu haberinde geçen kalıp parçaları imzadan çıkarır
    ("Haberin devamı için tıklayın", başlık sonundaki site adı vb.)."""
    if len(records) < 6:
        return
    mc = Counter(m for r in records for m in r["imza"]["m"])
    bc = Counter(b for r in records for b in r["imza"]["b"])
    for r in records:
        r["imza"]["m"] = [m for m in r["imza"]["m"] if mc[m] < 3]
        r["imza"]["b"] = [b for b in r["imza"]["b"] if bc[b] <= len(records) // 2]


class Rules:
    def __init__(self, cfg: dict):
        self.cat = build_matcher(cfg.get("kategori_engel", []))
        self.link = build_matcher(cfg.get("link_engel", []))
        self.title = build_matcher(cfg.get("baslik_engel", []))
        self.allow = build_matcher(cfg.get("kategori_izin", []))

    def reason(self, title: str, cats: list[str], link: str) -> str | None:
        """Haber atılacaksa nedenini, korunacaksa None döndürür."""
        ncats = [norm(c) for c in cats]
        if self.allow and any(self.allow.search(c) for c in ncats):
            return None
        if self.cat:
            for raw, c in zip(cats, ncats):
                if self.cat.search(c):
                    return f"kategori: {raw.strip()}"
        if self.link:
            for d in link_dirs(link):
                if self.link.search(norm(d)):
                    return f"link: /{d}/"
        if self.title and self.title.search(norm(title)):
            return "başlık"
        return None


def filter_feed(data: bytes, rules: Rules) -> tuple[bytes, int, int, list[str], list[dict]]:
    """Feed baytlarını filtreler. (yeni_bayt, toplam, atılan, örnekler, kayıtlar)."""
    parser = etree.XMLParser(recover=True, resolve_entities=False,
                             no_network=True, huge_tree=True,
                             remove_blank_text=False)
    root = etree.fromstring(data.lstrip(), parser)
    if root is None:
        raise ValueError("XML çözümlenemedi")
    kind = local(root.tag)
    if kind not in ("rss", "RDF", "feed"):
        raise ValueError(f"feed değil (kök öğe: {kind or '?'})")

    items = [el for el in root.iter()
             if local(el.tag) in ("item", "entry")]
    removed, samples, records = 0, [], []
    for el in items:
        title, cats, link = item_info(el)
        rec = item_record(el, title, cats, link)
        records.append(rec)
        why = rules.reason(title, cats, link)
        if why:
            el.getparent().remove(el)
            rec["atildi"] = True
            removed += 1
            if len(samples) < 5:
                samples.append(f"{title.strip()[:80]}  [{why}]")
    drop_boilerplate(records)
    out = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return out, len(items), removed, samples, records


# --------------------------------------------------------------------------
# İndirme ve çıktı
# --------------------------------------------------------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
})


def fetch(url: str) -> bytes:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


class Previous:
    """Önceki yayın: önce yerel kopya (--onceki), yoksa yayın adresi."""

    def __init__(self, local_dir: str | None, base_url: str | None):
        self.dir = Path(local_dir) if local_dir else None
        self.base_url = base_url

    def get(self, rel: str) -> bytes | None:
        if self.dir and self.dir.is_dir():
            p = self.dir / rel
            return p.read_bytes() if p.is_file() else None
        if self.base_url:
            try:
                return fetch(self.base_url + rel)
            except Exception:  # noqa: BLE001
                return None
        return None

    def json(self, rel: str) -> dict:
        data = self.get(rel)
        try:
            return json.loads(data) if data else {}
        except ValueError:
            return {}


# --------------------------------------------------------------------------
# RSS'i olmayan siteler: haber listesi sayfalarından feed üretme
# --------------------------------------------------------------------------
KIBRIS_TZ = ZoneInfo("Asia/Famagusta")
_REL = re.compile(r"(\d+)\s*(saniye|dakika|saat|gün|hafta|ay)\s*önce")
_REL_SEC = {"saniye": 1, "dakika": 60, "saat": 3600, "gün": 86400,
            "hafta": 604800, "ay": 2592000}
_BG = re.compile(r"url\('([^']+)'\)")
_TAK_LEAD = re.compile(r"^[^:]{0,60}\(TAK\)\s*:\s*")
_TAK_PAYWALL = "İçeriğin devamı için abone olmanız gerekmektedir."


def tak_date(text: str) -> datetime | None:
    """"03.11.2026 - 09:03:12" (Kıbrıs yerel saati)."""
    m = re.search(r"(\d\d)\.(\d\d)\.(\d{4})\s*-\s*(\d\d):(\d\d)(?::(\d\d))?", text or "")
    if not m:
        return None
    d, mo, y, h, mi, s = (int(x or 0) for x in m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=KIBRIS_TZ).astimezone(timezone.utc)


def tak_relative(text: str, now: datetime) -> datetime | None:
    """"15 dakika önce" gibi sunucunun yazdığı göreli zaman."""
    m = _REL.search(text or "")
    return now - timedelta(seconds=int(m.group(1)) * _REL_SEC[m.group(2)]) if m else None


def tak_detail(url: str) -> tuple[str, datetime | None]:
    """Haber sayfasından giriş paragrafı ve tam yayın saati. Metnin devamı
    abonelere açık, giriş paragrafı herkese açık."""
    doc = lhtml.fromstring(session.get(url, timeout=10).content)
    text = " ".join(doc.xpath('string(//div[contains(@class,"bulk-content")])').split())
    text = _TAK_LEAD.sub("", text.replace(_TAK_PAYWALL, "")).strip()
    return text, tak_date(doc.xpath('string(//*[contains(@class,"news-details-publish-date")])'))


def scrape_tak(feed: dict, prev_feed: bytes | None) -> bytes:
    """TAK'ın kategori sayfalarından RSS 2.0 üretir.

    Kategori sayfalarında iki tür kart var: üstteki slayt (tam tarih) ve
    alttaki liste (göreli zaman). Özet yalnızca haber sayfasında; önceki
    çalıştırmada alınan özetler önceki yayından okunur, sadece yeni
    haberlerin sayfası açılır."""
    base = feed["url"].rstrip("/") + "/"
    now = datetime.now(timezone.utc)
    items: dict[str, dict] = {}

    def add(link, title, cat, date, img):
        title = " ".join((title or "").split())
        if not link or not title:
            return
        it = items.setdefault(link, {"link": link, "baslik": title, "kategori": set(),
                                     "tarih": None, "gorsel": "", "ozet": ""})
        if cat:
            it["kategori"].add(" ".join(cat.split()))
        it["tarih"] = it["tarih"] or date
        it["gorsel"] = it["gorsel"] or img

    for page in feed["sayfalar"]:
        doc = lhtml.fromstring(fetch(base + page))
        for slide in doc.xpath('//div[contains(concat(" ", @class, " "), " carousel-item ")]'):
            a = slide.xpath('.//a[contains(@class,"slider-title-link")]')
            bg = _BG.search(" ".join(slide.xpath('.//*[contains(@class,"image-dashed")]/@style')))
            if a:
                add(a[0].get("href"), a[0].text_content(),
                    slide.xpath('string(.//*[contains(@class,"caption-category")])'),
                    tak_date(slide.xpath('string(.//*[contains(@class,"caption-date")])')),
                    bg.group(1) if bg else "")
        for card in doc.xpath('//div[contains(@class,"categories-news")]'):
            a = card.xpath('.//a[contains(@href,"/haber-detay/")]')
            bg = _BG.search(" ".join(card.xpath('.//*[contains(@class,"cropable-image")]/@style')))
            if a:
                add(a[0].get("href"), card.xpath('string(.//*[contains(@class,"news-description")])'),
                    card.xpath('string(.//*[contains(@class,"news-category-name")])'),
                    tak_relative(card.xpath('string(.//*[@name="newstimeleft"])'), now),
                    bg.group(1) if bg else "")
    if not items:
        raise ValueError("TAK sayfalarında haber bulunamadı (site tasarımı değişmiş olabilir)")

    # Önceki yayındaki özet ve tarihler
    known: dict[str, tuple[str, str]] = {}
    if prev_feed:
        old = etree.fromstring(prev_feed, etree.XMLParser(recover=True))
        if old is not None:
            for el in old.iter("item"):
                known[(el.findtext("link") or "").strip()] = (
                    el.findtext("description") or "", el.findtext("pubDate") or "")
    started = datetime.now(timezone.utc)
    errors = []
    for it in sorted(items.values(), key=lambda x: x["tarih"] or now, reverse=True):
        desc, date = known.get(it["link"], ("", ""))
        if desc:
            it["ozet"] = plain(desc)
            it["tarih"] = parse_date(date) or it["tarih"]
        elif datetime.now(timezone.utc) - started < timedelta(seconds=90):
            try:
                it["ozet"], exact = tak_detail(it["link"])
                it["tarih"] = exact or it["tarih"]
            except Exception as e:  # noqa: BLE001 - özet yoksa da haber yayımlanır
                errors.append(f"{type(e).__name__}: {e}")
    if errors:
        print(f'  ! {feed["ad"]}: {len(errors)} haber sayfası okunamadı ({errors[0][:120]})',
              file=sys.stderr)

    rss = etree.Element("rss", version="2.0")
    ch = etree.SubElement(rss, "channel")
    etree.SubElement(ch, "title").text = feed["ad"]
    etree.SubElement(ch, "link").text = base
    etree.SubElement(ch, "description").text = f'{feed["ad"]} (haber listesinden üretildi)'
    etree.SubElement(ch, "language").text = "tr"
    for it in sorted(items.values(), key=lambda x: x["tarih"] or now, reverse=True):
        el = etree.SubElement(ch, "item")
        etree.SubElement(el, "title").text = it["baslik"]
        etree.SubElement(el, "link").text = it["link"]
        etree.SubElement(el, "guid", isPermaLink="true").text = it["link"]
        etree.SubElement(el, "pubDate").text = format_datetime(it["tarih"] or now)
        for c in sorted(it["kategori"]):
            etree.SubElement(el, "category").text = c
        if it["ozet"]:
            etree.SubElement(el, "description").text = it["ozet"]
        if it["gorsel"]:
            etree.SubElement(el, "enclosure", url=quote(it["gorsel"], safe=":/%"),
                             type="image/jpeg", length="0")
    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8")


SCRAPERS = {"tak": scrape_tak}


def process(feed: dict, rules: Rules, outdir: Path, prev: Previous) -> dict:
    slug = slugify(feed["ad"])
    target = outdir / "feeds" / f"{slug}.xml"
    res = {"ad": feed["ad"], "kaynak": feed["url"], "dosya": f"feeds/{slug}.xml"}
    try:
        if feed.get("tur"):
            raw = SCRAPERS[feed["tur"]](feed, prev.get(res["dosya"]))
        else:
            raw = fetch(feed["url"])
        data, total, removed, samples, records = filter_feed(raw, rules)
        target.write_bytes(data)
        res.update(durum="ok", toplam=total, atilan=removed, ornekler=samples,
                   _kayitlar=records)
    except Exception as e:  # noqa: BLE001 - her hata raporlanır, akış durmaz
        res.update(durum="hata", hata=f"{type(e).__name__}: {e}"[:200])
        # Önceki yayımlanmış kopyayı koru
        old = prev.get(res["dosya"])
        if old:
            target.write_bytes(old)
            res["durum"] = "hata-eski-kopya"
    return res


# --------------------------------------------------------------------------
# Kategori envanteri
# --------------------------------------------------------------------------
def update_inventory(state: dict, results: list[dict], stamp: str) -> dict:
    """Her sitede görülen kategori ve klasörleri biriktirir. Bir haber
    yalnızca ilk görüldüğü çalıştırmada sayılır."""
    sites = state.setdefault("siteler", {})
    state.setdefault("baslangic", stamp)
    for r in results:
        if r["durum"] != "ok":
            continue
        s = sites.setdefault(r["ad"], {"haber": 0, "kategorisiz": 0,
                                       "kategori": {}, "klasor": {}, "gorulen": []})
        seen = set(s["gorulen"])
        for rec in r["_kayitlar"]:
            h = zlib.crc32(rec["anahtar"].encode())
            if h in seen:
                continue
            seen.add(h)
            s["gorulen"].append(h)
            s["haber"] += 1
            if not rec["kategoriler"]:
                s["kategorisiz"] += 1
            for c in set(rec["kategoriler"]):
                s["kategori"][c] = s["kategori"].get(c, 0) + 1
            for d in set(rec["klasorler"]):
                s["klasor"][d] = s["klasor"].get(d, 0) + 1
        s["gorulen"] = s["gorulen"][-1500:]
    state["guncelleme"] = stamp
    return state


# --------------------------------------------------------------------------
# Tekrar ayıklama ve birleşik feed
# --------------------------------------------------------------------------
def similarity(a: dict, b: dict) -> float:
    """İki imzanın benzerlik puanı; aynı haber değilse 0."""
    ba, bb = set(a["b"]), set(b["b"])
    tb = len(ba & bb)
    jb = tb / (len(ba | bb) or 1)
    ma, mb = set(a["m"]), set(b["m"])
    tm = len(ma & mb)
    om = tm / (min(len(ma), len(mb)) or 1)
    # Metin benzerliği tek başına yetmez: "TDP Genel Başkanı Zeki Çeler ve
    # beraberindeki..." gibi kalıp girişler farklı haberlerde de geçer.
    if ((jb >= 0.6 and tb >= 3) or (om >= 0.5 and tm >= 15)
            or (om >= 0.5 and tm >= 5 and tb >= 1)
            or (jb >= 0.3 and tb >= 2 and om >= 0.2 and tm >= 3)):
        return jb + om
    return 0.0


def _when(iso: str | None, now: datetime) -> datetime:
    d = datetime.fromisoformat(iso) if iso else now
    return min(d, now)  # saat dilimi yanlış siteler gelecekte görünmesin


def main_link(c: dict) -> str:
    """Kümenin ana linki: mümkünse abonelik istemeyen ilk kaynak."""
    return next((s["link"] for s in c["kaynaklar"] if not s.get("abonelik")), c["link"])


def update_clusters(state: dict, results: list[dict], now: datetime,
                    keep_hours: int, paywalled: set[str] = frozenset(),
                    window_hours: int = 36) -> list[dict]:
    """Yeni haberleri önceki kümelere ekler ya da yeni küme açar.

    Küme: aynı haberin farklı kaynaklardaki kopyaları. İlk gelen kopya
    kümenin temsilcisidir (başlık, link, guid ondan gelir), böylece guid
    çalıştırmalar arasında değişmez."""
    cutoff = now - timedelta(hours=keep_hours)
    clusters = [c for c in state.get("kumeler", [])
                if datetime.fromisoformat(c["ilk"]) >= cutoff]
    known = {k for c in clusters for k in c["anahtar"]}
    index: dict[tuple, set[int]] = defaultdict(set)

    def add_index(i: int, sig: dict):
        for b in sig["b"]:
            index["b", b].add(i)
        for m in sig["m"]:
            index["m", m].add(i)

    for i, c in enumerate(clusters):
        for sig in c["imza"]:
            add_index(i, sig)

    fresh = []
    for r in results:
        if r["durum"] != "ok":
            continue
        for rec in r["_kayitlar"]:
            key = f'{r["ad"]}|{rec["anahtar"]}'
            if rec["atildi"] or key in known:
                continue
            known.add(key)
            fresh.append((_when(rec["tarih"], now), key, r["ad"], rec))
    fresh.sort(key=lambda x: x[0])

    window = timedelta(hours=window_hours)
    for when, key, site, rec in fresh:
        sig = rec["imza"]
        cand = set()
        for b in sig["b"]:
            cand |= index.get(("b", b), set())
        for m in sig["m"]:
            cand |= index.get(("m", m), set())
        best, best_score = None, 0.0
        for i in cand:
            c = clusters[i]
            if abs(when - datetime.fromisoformat(c["ilk"])) > window:
                continue
            score = max(similarity(sig, s) for s in c["imza"])
            if score > best_score:
                best, best_score = i, score
        source = {"ad": site, "link": rec["link"], "tarih": when.isoformat()}
        if site in paywalled:
            source["abonelik"] = True
        if best is None:
            clusters.append({
                "id": "k-" + hashlib.sha1(key.encode()).hexdigest()[:16],
                "ilk": when.isoformat(),
                "baslik": rec["baslik"], "link": rec["link"],
                "ozet": rec["ozet"], "gorsel": rec["gorsel"],
                "kaynaklar": [source], "anahtar": [key], "imza": [sig],
            })
            best = len(clusters) - 1
        else:
            c = clusters[best]
            c["anahtar"].append(key)
            if all(s["ad"] != site for s in c["kaynaklar"]):
                c["kaynaklar"].append(source)
            if len(c["imza"]) < 5:
                c["imza"].append(sig)
            if not c["gorsel"]:
                c["gorsel"] = rec["gorsel"]
        add_index(best, sig)

    state["kumeler"] = clusters
    return clusters


def write_merged(path: Path, clusters: list[dict], cfg: dict, base_url: str | None,
                 now: datetime) -> int:
    hours = cfg.get("saat", 48)
    limit = cfg.get("en_fazla", 300)
    cutoff = now - timedelta(hours=hours)
    # Yalnızca abonelikli kaynakta olan haber, başka bir site de verir diye
    # bir süre bekletilir; okuyucu ilk gördüğü linki saklar.
    wait = timedelta(minutes=cfg.get("abonelik_bekleme_dk", 60))

    def ready(c: dict) -> bool:
        return (any(not s.get("abonelik") for s in c["kaynaklar"])
                or now - datetime.fromisoformat(c["ilk"]) >= wait)

    shown = sorted((c for c in clusters
                    if datetime.fromisoformat(c["ilk"]) >= cutoff and ready(c)),
                   key=lambda c: c["ilk"], reverse=True)[:limit]

    rss = etree.Element("rss", version="2.0", nsmap={"atom": ATOM})
    ch = etree.SubElement(rss, "channel")
    etree.SubElement(ch, "title").text = cfg.get("ad", "Kıbrıs Haberler (Birleşik)")
    etree.SubElement(ch, "link").text = base_url or "https://example.invalid/"
    etree.SubElement(ch, "description").text = (
        "Tüm kaynaklardan filtrelenmiş, tekrarları birleştirilmiş haberler")
    etree.SubElement(ch, "language").text = "tr"
    etree.SubElement(ch, "lastBuildDate").text = format_datetime(now)
    if base_url:
        etree.SubElement(ch, f"{{{ATOM}}}link", rel="self", type="application/rss+xml",
                         href=base_url + "feeds/birlesik.xml")
    for c in shown:
        n = len(c["kaynaklar"])
        it = etree.SubElement(ch, "item")
        etree.SubElement(it, "title").text = c["baslik"] + (f" ({n} kaynak)" if n > 1 else "")
        etree.SubElement(it, "link").text = main_link(c)
        etree.SubElement(it, "guid", isPermaLink="false").text = c["id"]
        etree.SubElement(it, "pubDate").text = format_datetime(datetime.fromisoformat(c["ilk"]))
        for s in c["kaynaklar"]:
            etree.SubElement(it, "category").text = s["ad"]
        links = ", ".join(f'<a href="{html.escape(s["link"])}">{html.escape(s["ad"])}</a>'
                          + (" (abonelik)" if s.get("abonelik") else "")
                          for s in c["kaynaklar"])
        body = (f'<p><img src="{html.escape(c["gorsel"])}"></p>' if c["gorsel"] else "")
        body += f'<p>{html.escape(c["ozet"])}</p><p><b>{n} kaynakta:</b> {links}</p>'
        etree.SubElement(it, "description").text = body
    path.write_bytes(etree.tostring(rss, xml_declaration=True, encoding="UTF-8"))
    return len(shown)


def write_opml(path: Path, folder: str, results: list[dict], base_url: str | None,
               merged_name: str):
    prefix = base_url or ""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<opml version="1.0"><head><title>Kıbrıs Haberler (Filtreli)</title></head><body>',
             f'<outline text={quoteattr(merged_name)} title={quoteattr(merged_name)} '
             f'type="rss" xmlUrl={quoteattr(prefix + "feeds/birlesik.xml")}/>',
             f'<outline text={quoteattr(folder)} title={quoteattr(folder)}>']
    for r in results:
        url = prefix + r["dosya"]
        parts = urlsplit(r["kaynak"])
        site = f"{parts.scheme}://{parts.netloc}/"
        lines.append(f'<outline text={quoteattr(r["ad"])} title={quoteattr(r["ad"])} '
                     f'type="rss" xmlUrl={quoteattr(url)} htmlUrl={quoteattr(site)}/>')
    lines.append("</outline></body></opml>")
    path.write_text("\n".join(lines), encoding="utf-8")


PAGE_STYLE = """body{font:15px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 16px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;vertical-align:top;text-align:left}
td.n{text-align:right;white-space:nowrap}tr.hata td,tr.hata-eski-kopya td{color:#a33}small{color:#666}
.engel{color:#a33}.izin{color:#272}details{margin:.4rem 0}summary{cursor:pointer}"""


def write_index(path: Path, results: list[dict], stamp: str, top: list[dict]):
    rows = []
    for r in results:
        if r["durum"] == "ok":
            st = f'{r["atilan"]} / {r["toplam"]} atıldı'
        elif r["durum"] == "hata-eski-kopya":
            st = "indirilemedi, önceki kopya korundu"
        else:
            st = "indirilemedi"
        det = "<br>".join(html.escape(s) for s in r.get("ornekler", []))
        err = html.escape(r.get("hata", ""))
        rows.append(f'<tr class="{r["durum"]}"><td><a href="{html.escape(r["dosya"])}">'
                    f'{html.escape(r["ad"])}</a></td><td>{st}</td>'
                    f'<td><small>{det or err}</small></td></tr>')
    hot = "".join(
        f'<tr><td class="n">{len(c["kaynaklar"])}</td><td><a href="{html.escape(main_link(c))}">'
        f'{html.escape(c["baslik"])}</a><br><small>'
        f'{html.escape(", ".join(s["ad"] for s in c["kaynaklar"]))}</small></td></tr>'
        for c in top)
    path.write_text(f"""<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kıbrıs RSS Filtresi</title>
<style>{PAGE_STYLE}</style>
<h1>Kıbrıs RSS Filtresi</h1>
<p>Son güncelleme: {stamp} · <a href="filtreli.opml">filtreli.opml</a> ·
<a href="feeds/birlesik.xml">birleşik feed</a> · <a href="envanter.html">kategori envanteri</a> ·
<a href="rapor.json">rapor.json</a></p>
<h2>En çok kaynakta geçenler (son 24 saat)</h2>
<table>{hot or '<tr><td>Henüz yok</td></tr>'}</table>
<h2>Kaynaklar</h2>
<table>{''.join(rows)}</table></html>""", encoding="utf-8")


def write_inventory(path: Path, inv: dict, rules: Rules):
    """Kategori envanteri sayfası: önce tüm siteler toplu, sonra site site."""
    def mark(text: str, matcher) -> str:
        n = norm(text)
        if rules.allow and matcher is rules.cat and rules.allow.search(n):
            return '<span class="izin">izinli</span>'
        if matcher and matcher.search(n):
            return '<span class="engel">engelli</span>'
        return ""

    total: dict[str, Counter] = defaultdict(Counter)  # norm -> {ham ad: sayı}
    where: dict[str, set] = defaultdict(set)
    sites = inv.get("siteler", {})
    for site, s in sites.items():
        for c, n in s["kategori"].items():
            total[norm(c)][c] += n
            where[norm(c)].add(site)
    rows = []
    for key, forms in sorted(total.items(), key=lambda kv: -sum(kv[1].values())):
        name = forms.most_common(1)[0][0]
        rows.append(f'<tr><td>{html.escape(name)}</td><td class="n">{sum(forms.values())}</td>'
                    f'<td class="n">{len(where[key])}</td><td>{mark(name, rules.cat)}</td></tr>')

    def table(counts: dict, matcher, limit: int) -> str:
        items = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return "<table>" + "".join(
            f'<tr><td>{html.escape(k)}</td><td class="n">{v}</td><td>{mark(k, matcher)}</td></tr>'
            for k, v in items) + "</table>"

    blocks = []
    for site in sorted(sites):
        s = sites[site]
        blocks.append(
            f'<details><summary><b>{html.escape(site)}</b> · {s["haber"]} haber, '
            f'{s["kategorisiz"]} kategorisiz</summary>'
            f'<h4>Kategoriler</h4>{table(s["kategori"], rules.cat, 60)}'
            f'<h4>Link klasörleri</h4>{table(s["klasor"], rules.link, 30)}</details>')
    path.write_text(f"""<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kategori Envanteri</title>
<style>{PAGE_STYLE}</style>
<h1>Kategori envanteri</h1>
<p><a href="index.html">← durum sayfası</a> · Sayım başlangıcı: {html.escape(inv.get("baslangic", "?"))}
· Son: {html.escape(inv.get("guncelleme", "?"))}. Her haber bir kez sayılır, filtreden önceki
hâliyle. "engelli" işareti mevcut kurallara göre.</p>
<h2>Tüm siteler</h2>
<table><tr><th>Kategori</th><th>Haber</th><th>Site</th><th></th></tr>{''.join(rows)}</table>
<h2>Site site</h2>{''.join(blocks)}</html>""", encoding="utf-8")


def detect_base_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")  # "kullanici/repo"
    if not repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{name}/"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="feeds.toml")
    ap.add_argument("--out", default="public")
    ap.add_argument("--base-url", help="Yayın adresi (varsayılan: GitHub Pages'ten tahmin)")
    ap.add_argument("--onceki", help="Önceki yayının yerel kopyası (gh-pages checkout'u); "
                                     "verilmezse yayın adresinden indirilir")
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    rules = Rules(cfg.get("filtre", {}))
    merged_cfg = cfg.get("birlesik", {})
    feeds = cfg.get("feed", [])
    base_url = args.base_url or detect_base_url()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    prev = Previous(args.onceki, base_url)

    outdir = Path(args.out)
    (outdir / "feeds").mkdir(parents=True, exist_ok=True)
    (outdir / "durum").mkdir(exist_ok=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda f: process(f, rules, outdir, prev), feeds))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")

    inv = update_inventory(prev.json("durum/envanter.json"), results, stamp)
    (outdir / "durum" / "envanter.json").write_text(
        json.dumps(inv, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_inventory(outdir / "envanter.html", inv, rules)

    state = prev.json("durum/kumeler.json")
    clusters = update_clusters(state, results, now,
                               keep_hours=max(merged_cfg.get("saat", 48), 36) + 48,
                               paywalled={f["ad"] for f in feeds if f.get("abonelik")})
    (outdir / "durum" / "kumeler.json").write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    merged_name = merged_cfg.get("ad", "Kıbrıs Haberler (Birleşik)")
    shown = write_merged(outdir / "feeds" / "birlesik.xml", clusters, merged_cfg, base_url, now)
    day = (now - timedelta(hours=24)).isoformat()
    top = sorted((c for c in clusters if c["ilk"] >= day and len(c["kaynaklar"]) > 1),
                 key=lambda c: (-len(c["kaynaklar"]), c["ilk"]))[:15]

    for r in results:
        r.pop("_kayitlar", None)
    multi = sum(len(c["kaynaklar"]) > 1 for c in clusters)
    write_opml(outdir / "filtreli.opml",
               cfg.get("filtre", {}).get("opml_klasor", "Kıbrıs Haberler (Filtreli)"),
               results, base_url, merged_name)
    (outdir / "rapor.json").write_text(
        json.dumps({"guncelleme": stamp,
                    "birlesik": {"yayinda": shown, "kume": len(clusters), "cok_kaynakli": multi},
                    "feedler": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_index(outdir / "index.html", results, stamp, top)
    (outdir / ".nojekyll").touch()

    for r in results:
        if r["durum"] == "ok":
            print(f'✓ {r["ad"]}: {r["atilan"]}/{r["toplam"]} haber atıldı')
            for s in r["ornekler"]:
                print(f"    - {s}")
        else:
            print(f'✗ {r["ad"]}: {r["durum"]} ({r.get("hata", "")})')

    ok = sum(r["durum"] == "ok" for r in results)
    print(f"\n{ok}/{len(results)} feed güncellendi.")
    print(f"Birleşik feed: {shown} haber yayında; {len(clusters)} kümenin "
          f"{multi} tanesi birden fazla kaynakta.")
    # Hiçbir feed indirilemediyse iş akışı başarısız sayılsın
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
