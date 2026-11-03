# Kıbrıs RSS Filtresi

Kıbrıs haber sitelerinin RSS feed'lerini her 10 dakikada bir indirir, spor,
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

## RSS'i olmayan siteler (TAK)

TAK'ın (Türk Ajansı Kıbrıs) RSS feed'i yok. `feeds.toml`'da `tur = "tak"`
olan kaynak için feed, sitenin kategori sayfalarından (`sayfalar`) üretilir.
Haber özetleri haber sayfalarından okunur; her haber sayfası yalnızca bir
kez açılır, sonraki çalıştırmalarda özet önceki yayından alınır.

TAK haberlerinin yalnızca giriş paragrafı herkese açık (`abonelik = true`).
Birleşik feed'de aynı haberi başka bir site de verdiyse ana link o siteye
gider. Sadece TAK'ta olan haberler, başka bir site de verir diye
`abonelik_bekleme_dk` kadar bekletilir.

TAK sayfa tasarımını değiştirirse bu kaynak durum sayfasında hata olarak
görünür; `filter_feeds.py` içindeki `scrape_tak` güncellenmelidir.

## Kategori envanteri

`envanter.html`, her sitenin haberlerinde görülen kategorileri ve link
klasörlerini sayar. Her haber yalnızca bir kez, filtreden önceki hâliyle
sayılır ve sayım çalıştırmalar boyunca birikir. Mevcut kurala takılanlar
"engelli" olarak işaretlenir. Engel listesine ne ekleneceğini buradan seçebilirsin.

Envanter ve birleşik feed'in durumu `durum/` klasöründe tutulur. İş akışı
her çalıştırmada önceki çıktıyı Actions önbelleğinden (yoksa yayındaki
siteden) okuyup bu durumu devam ettirir.

## Kurulum (bir kerelik, yaklaşık 5 dakika)

1. GitHub'da yeni bir **public** repo oluştur (ör. `kibris-rss`).
   Ücretsiz hesapta GitHub Pages yalnızca public repolarda çalışır.
2. Bu klasördeki tüm dosyaları repoya yükle. `.github/workflows/filtrele.yml`
   dosyasının bu yolda olması şart. Tarayıcıdan yüklerken gizli klasör
   görünmeyebilir; o durumda repoda "Add file → Create new file" ile
   `.github/workflows/filtrele.yml` adıyla oluşturup içeriği yapıştır.
3. **Settings → Pages → Build and deployment** bölümünde Source:
   **GitHub Actions** seç. (Daldan yayımlamada saatte 10 yayın sınırı var;
   Actions ile yayımlamada yok.)
4. **Actions** sekmesinde "RSS filtrele" iş akışını aç, **Run workflow** ile
   ilk kez elle çalıştır. 1-2 dakika sürer.
5. Düzenli çalışma için aşağıdaki "Dışarıdan tetikleme" adımlarını yap.
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

## Dışarıdan tetikleme (10 dakikada bir)

GitHub zamanlanmış iş akışlarını garanti etmez; yoğun saatlerde geciktirir
ya da atlar (bu repoda 30 dakikalık zamanlama ilk gün 36 yerine 2 kez
çalıştı). Düzenli çalışma için ücretsiz bir cron servisi iş akışını her 10
dakikada bir API ile başlatır. Zamanlama yedek olarak kalır.

1. **Token:** GitHub → Settings → Developer settings → Personal access
   tokens → **Fine-grained tokens** → Generate new token.
   - Repository access: **Only select repositories** → `kibris-rss`
   - Permissions → Repository permissions → **Actions: Read and write**
     (başka izin verme)
   - Expiration: en fazla 1 yıl; bitince yenilemen gerekir.
2. **cron-job.org** hesabı aç → Create cronjob:
   - URL: `https://api.github.com/repos/om3rcan/kibris-rss/actions/workflows/filtrele.yml/dispatches`
   - Execution schedule: Every 10 minutes
   - Advanced → Request method: **POST**
   - Headers:
     - `Authorization: Bearer TOKEN` (TOKEN yerine 1. adımdaki token)
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main"}`
3. **Test et:** cron-job.org'da "Test run". Yanıt **204** olmalı ve
   Actions sekmesinde `workflow_dispatch` ile yeni bir çalıştırma görünmeli.

Token yalnızca bu repoda iş akışı başlatabilir; sızarsa en kötü ihtimalle
iş akışı fazladan çalışır. Şüphelenirsen GitHub'dan silip yenisini oluştur.

Fluent Reader feed'leri kendi yenileme aralığında okur: Ayarlar'dan
yenileme aralığını 10 dakikaya çekmeyi unutma.

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
