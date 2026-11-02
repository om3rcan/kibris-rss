#!/usr/bin/env python3
"""Kıbrıs haber feed'lerinden spor/magazin vb. haberleri ayıklar.

feeds.toml'daki her feed'i indirir, engel kurallarına uyan <item>/<entry>
öğelerini çıkarır ve sonucu <out>/feeds/<slug>.xml olarak yazar. Ayrıca
Fluent Reader'a aktarmak için <out>/filtreli.opml, durum özeti için
<out>/rapor.json ve <out>/index.html üretir.

Bir feed indirilemezse, daha önce yayımlanmış sürümü (GitHub Pages'teki
kopya) korunur; okuyucuda o kaynak boş görünmez.

Kullanım:
    python filter_feeds.py --out public
    python filter_feeds.py --out public --base-url https://kullanici.github.io/repo/
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote
from xml.sax.saxutils import quoteattr

import requests
from lxml import etree

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TIMEOUT = 25
ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/elements/1.1/"

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


def filter_feed(data: bytes, rules: Rules) -> tuple[bytes, int, int, list[str]]:
    """Feed baytlarını filtreler. (yeni_bayt, toplam, atılan, örnekler)."""
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
    removed, samples = 0, []
    for el in items:
        title, cats, link = item_info(el)
        why = rules.reason(title, cats, link)
        if why:
            el.getparent().remove(el)
            removed += 1
            if len(samples) < 5:
                samples.append(f"{title.strip()[:80]}  [{why}]")
    out = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return out, len(items), removed, samples


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


def process(feed: dict, rules: Rules, outdir: Path, base_url: str | None) -> dict:
    slug = slugify(feed["ad"])
    target = outdir / "feeds" / f"{slug}.xml"
    res = {"ad": feed["ad"], "kaynak": feed["url"], "dosya": f"feeds/{slug}.xml"}
    try:
        data, total, removed, samples = filter_feed(fetch(feed["url"]), rules)
        target.write_bytes(data)
        res.update(durum="ok", toplam=total, atilan=removed, ornekler=samples)
    except Exception as e:  # noqa: BLE001 - her hata raporlanır, akış durmaz
        res.update(durum="hata", hata=f"{type(e).__name__}: {e}"[:200])
        # Önceki yayımlanmış kopyayı koru
        if base_url:
            try:
                old = fetch(base_url + res["dosya"])
                target.write_bytes(old)
                res["durum"] = "hata-eski-kopya"
            except Exception:  # noqa: BLE001
                pass
    return res


def write_opml(path: Path, folder: str, results: list[dict], base_url: str | None):
    prefix = base_url or ""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<opml version="1.0"><head><title>Kıbrıs Haberler (Filtreli)</title></head><body>',
             f'<outline text={quoteattr(folder)} title={quoteattr(folder)}>']
    for r in results:
        url = prefix + r["dosya"]
        parts = urlsplit(r["kaynak"])
        site = f"{parts.scheme}://{parts.netloc}/"
        lines.append(f'<outline text={quoteattr(r["ad"])} title={quoteattr(r["ad"])} '
                     f'type="rss" xmlUrl={quoteattr(url)} htmlUrl={quoteattr(site)}/>')
    lines.append("</outline></body></opml>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_index(path: Path, results: list[dict], stamp: str):
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
    path.write_text(f"""<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kıbrıs RSS Filtresi</title>
<style>body{{font:15px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:6px;vertical-align:top}}
tr.hata td,tr.hata-eski-kopya td{{color:#a33}}small{{color:#666}}</style>
<h1>Kıbrıs RSS Filtresi</h1>
<p>Son güncelleme: {stamp} · <a href="filtreli.opml">filtreli.opml</a> · <a href="rapor.json">rapor.json</a></p>
<table>{''.join(rows)}</table></html>""", encoding="utf-8")


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
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    rules = Rules(cfg.get("filtre", {}))
    feeds = cfg.get("feed", [])
    base_url = args.base_url or detect_base_url()
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    outdir = Path(args.out)
    (outdir / "feeds").mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda f: process(f, rules, outdir, base_url), feeds))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    write_opml(outdir / "filtreli.opml",
               cfg.get("filtre", {}).get("opml_klasor", "Kıbrıs Haberler (Filtreli)"),
               results, base_url)
    (outdir / "rapor.json").write_text(
        json.dumps({"guncelleme": stamp, "feedler": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_index(outdir / "index.html", results, stamp)
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
    # Hiçbir feed indirilemediyse iş akışı başarısız sayılsın
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
