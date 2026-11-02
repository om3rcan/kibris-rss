# Kıbrıs RSS Filtresi

Kıbrıs haber sitelerinin RSS feed'lerini her 30 dakikada bir indirir, spor,
magazin, astroloji gibi haberleri ayıklar ve temizlenmiş feed'leri GitHub
Pages'te yayımlar. Fluent Reader orijinal feed'ler yerine bu temiz feed'leri
okur.

## Nasıl filtreliyor?

Her haber üç açıdan kontrol edilir (kurallar `feeds.toml` içinde):

1. **RSS kategorisi**: Sitenin habere verdiği kategori "Spor", "Magazin",
   "Astroloji" vb. ise haber atılır. En güvenilir yöntem budur.
2. **Link yolu**: `site.com/spor/...` veya `site.com/magazin-haberleri/...`
   gibi bir klasördeyse haber atılır. Kategori bilgisi vermeyen siteler için.
   Haber başlığından üretilen son parça bakılmaz, yani
   `/gundem/bakan-spor-salonunu-acti` korunur.
3. **Başlık kelimeleri**: Varsayılan olarak kapalı. Yanlış pozitif riski
   yüksek olduğu için sadece gerektiğinde açılmalı.

Eşleştirme Türkçe karakter ve büyük/küçük harften bağımsızdır.

## Birleşik feed

`feeds/birlesik.xml`, tüm kaynaklardaki filtrelenmiş haberleri tek feed'de
toplar. Aynı haber (ör. aynı TAK haberi ya da basın açıklaması) birden fazla
sitede çıktıysa tek kayıt olarak görünür. Başlığa "(N kaynak)" eklenir,
açıklamada her kaynağın linki yer alır. Haberin ilk görüldüğü site kaydın
ana linkidir.

Aynı haber olup olmadığı başlık ve metnin ilk cümlelerinin benzerliğine
bakılarak ve 36 saatlik zaman aralığı içinde aranır. Türkçe ekler yüzünden
kelimelerin ilk 5 harfi karşılaştırılır. Ayarlar `feeds.toml` içindeki
`[birlesik]` bölümünde.

## Kategori envanteri

`envanter.html`, her sitenin haberlerinde görülen kategorileri ve link
klasörlerini sayar. Her haber yalnızca bir kez, filtreden önceki hâliyle
sayılır ve sayım çalıştırmalar boyunca birikir. Mevcut kurala takılanlar
"engelli" olarak işaretlenir. Engel listesine ne ekleneceğini buradan seçebilirsin.

Envanter ve birleşik feed'in durumu `durum/` klasöründe tutulur. İş akışı
her çalıştırmada önceki yayını (`gh-pages`) okuyup bu durumu devam ettirir.

## Kurulum (bir kerelik, yaklaşık 5 dakika)

1. GitHub'da yeni bir **public** repo oluştur (ör. `kibris-rss`).
   Ücretsiz hesapta GitHub Pages yalnızca public repolarda çalışır.
2. Bu klasördeki tüm dosyaları repoya yükle. `.github/workflows/filtrele.yml`
   dosyasının bu yolda olması şart. Tarayıcıdan yüklerken gizli klasör
   görünmeyebilir; o durumda repoda "Add file → Create new file" ile
   `.github/workflows/filtrele.yml` adıyla oluşturup içeriği yapıştır.
3. **Settings → Actions → General → Workflow permissions** bölümünde
   "Read and write permissions" seçili olsun.
4. **Actions** sekmesinde "RSS filtrele" iş akışını aç, **Run workflow** ile
   ilk kez elle çalıştır. 1-2 dakika sürer ve `gh-pages` dalını oluşturur.
5. **Settings → Pages → Build and deployment** bölümünde Source: "Deploy from
   a branch", Branch: `gh-pages` / `(root)` seç ve kaydet.
6. Birkaç dakika sonra `https://KULLANICI.github.io/kibris-rss/` adresinde
   durum sayfası açılır. Hangi feed'den kaç haber atıldığını burada görürsün.
7. Aynı adresteki `filtreli.opml` dosyasını indir ve Fluent Reader'da
   **Ayarlar → Kaynaklar → OPML içe aktar** ile ekle. Eski (filtresiz)
   kaynakları silebilirsin. OPML'in başında birleşik feed de vardır; tek
   tek kaynaklar yerine sadece onu takip etmek de mümkün.

## Günlük kullanım

- **Kural değiştirmek**: Repoda `feeds.toml` dosyasını düzenle ve kaydet.
  Kayıt anında iş akışı tetiklenir, birkaç dakikada yeni feed'ler yayında
  olur.
- **Kaynak eklemek**: `feeds.toml` sonuna yeni bir `[[feed]]` bloğu ekle.
  Ardından güncel `filtreli.opml`'den sadece yeni kaynağı Fluent Reader'a
  ekle (veya OPML'i tekrar içe aktar).
- **Neyin atıldığını görmek**: Durum sayfasında her kaynak için atılan
  haberlerden örnekler ve neden atıldıkları yazar. `rapor.json` daha
  ayrıntılıdır.
- **Bir site indirilemezse**: O kaynağın son başarılı kopyası korunur,
  okuyucuda boş görünmez. Durum sayfasında kırmızı olarak işaretlenir.

## Bilinmesi gerekenler

- GitHub zamanlanmış iş akışlarını yoğun saatlerde birkaç dakika gecikmeli
  çalıştırabilir.
- Repoda 60 gün boyunca hiç commit olmazsa GitHub zamanlanmış iş akışını
  durdurur ve e-posta atar. Actions sekmesinden tek tıkla yeniden
  etkinleştirilir.
- Bazı siteler GitHub sunucularından gelen istekleri engelleyebilir. Böyle
  bir kaynak sürekli kırmızı görünürse onu Fluent Reader'da orijinal feed
  adresiyle tutmak gerekir.

## Yerelde çalıştırma

```bash
pip install -r requirements.txt
python filter_feeds.py --out public
# Önceki yayının durumunu bir klasörden okumak için:
python filter_feeds.py --out public --onceki eski-public
```
