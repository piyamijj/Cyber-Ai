# Cyber AI Hafıza & Arama Servisi (RAG)

Oracle Cloud sunucunuzda port `8083` üzerinde çalışan, Qdrant vektör veritabanı ve `BAAI/bge-base-en-v1.5` embedding modeli kullanan, RAG (Retrieval-Augmented Generation) arama ve akıllı otomatik hafıza kaydı mikroservisi.

## Özellikler

- **RAG Arama (`POST /search`):** Kullanıcının sorduğu soruya göre Qdrant üzerinde anlamsal (vektörel) arama yapar. Alakasız geçmiş bilgileri elemek için bir benzerlik eşiği (`SEARCH_SCORE_THRESHOLD`) kullanır.
- **Akıllı Otomatik Hafıza Kaydı (`POST /remember`):** Bir konuşma tamamlandığında (kullanıcı mesajı + asistan cevabı), arka planda llama.cpp sunucunuza danışarak bu konuşmanın hafızaya değer kalıcı bir bilgi (tercih, kişisel detay, özel talimat vb.) içerip içermediğine karar verir. Eğer önemliyse, bilgiyi Türkçe özetleyip vektöre çevirerek Qdrant'a kaydeder. Sıradan sohbetleri atlar.
- **Sağlık Kontrolü (`GET /health`):** Servisin, Qdrant bağlantısının, koleksiyonun ve embedding modelinin durumunu raporlar.

## Kurulum ve Çalıştırma

1. **Klasörü Sunucuya Kopyalayın:**
   `cyber-memory-service` klasörünü Oracle sunucunuzda ev dizininize (`/home/ubuntu/cyber-memory-service`) kopyalayın.

2. **Bağımlılıkları Yükleyin:**
   Sunucuda şu komutu çalıştırarak gerekli Python paketlerini yükleyin:
   ```bash
   pip3 install -r requirements.txt --break-system-packages
   ```

3. **Systemd Servisini Kurun:**
   `cyber-memory.service` dosyasını `/etc/systemd/system/` dizinine kopyalayın ve servisi başlatın:
   ```bash
   sudo cp cyber-memory.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cyber-memory
   ```

4. **Çalıştığını Doğrulayın:**
   Servisin ayakta olduğunu ve Qdrant bağlantısını şu komutla test edin:
   ```bash
   curl http://localhost:8083/health
   ```

## Ortam Değişkenleri (Environment Variables)

Servis davranışını özelleştirmek için `cyber-memory.service` dosyasındaki `Environment` satırlarını düzenleyebilirsiniz:

- `QDRANT_HOST`: Qdrant sunucu adresi (Varsayılan: `localhost`)
- `QDRANT_PORT`: Qdrant portu (Varsayılan: `6333`)
- `QDRANT_COLLECTION`: Kullanılacak koleksiyon adı (Varsayılan: `cyber_memory`)
- `LLAMA_SERVER_URL`: llama.cpp sunucu adresi (Varsayılan: `http://localhost:8082`)
- `SEARCH_TOP_K`: Arama sırasında getirilecek maksimum kayıt sayısı (Varsayılan: `5`)
- `SEARCH_SCORE_THRESHOLD`: Alakasız kayıtları elemek için benzerlik eşiği (Varsayılan: `0.45`)

## Güvenlik Notu

Bu servis doğrudan tarayıcıdan çağrılmaz. Sadece Vercel üzerindeki proxy backend (sunucudan sunucuya) bu servisle konuşur. Bu nedenle ek bir kimlik doğrulama (auth) eklenmemiştir. Güvenlik için Oracle Cloud panelinizden `8083` portunu dış dünyaya açarken dikkatli olmanız önerilir.

---

## YENİ MİMARİ: Streaming Çırak-Usta (Draft-Critique) İşbirlikçi Boru Hattı

`/collab_stream` endpoint'i, önceki basit `/decide` (arama gerekli mi kararı) mekanizmasının ÜZERİNE inşa edilmiş, tam bir işbirlikçi cevap üretim mimarisidir. Ana mantık `collab_orchestrator.py` dosyasında bulunur.

### Nasıl Çalışır?

1. **Shared Context (Tek Seferlik RAG):** Kullanıcı sorusu geldiği ilk anda, `/decide` (arama gerekli mi?), `/search` (RAG) ve gerekirse Tavily web araması BİR SEFERDE ve paralel olarak çalıştırılır. Sonuç bir `SharedContext` nesnesinde toplanır. Çırak ve Usta modellerin HİÇBİRİ artık kendi başına tekrar arama yapmaz — ikisi de bu ortak bağlamı okur.
2. **Streaming Pipe:** Çırak model (0.5B) taslak cevabı üretirken, `asyncio.Queue` üzerinden chunk'lar canlı olarak akıtılır. Bu kuyruk aynı zamanda Usta modelin (14B) taslağı okumasını sağlar.
3. **Kod Seviyesinde Kesin Max-Tur Sınırı:** Revizyon döngüsü `COLLAB_MAX_REVISION_TURNS` (varsayılan 2, ortam değişkeninden ayarlanabilir, kod içinde 1-5 aralığına sıkıştırılır) ile SINIRLIDIR — bu bir öneri değil, `for turn in range(1, MAX_REVISION_TURNS + 1)` ile kod seviyesinde garanti edilen bir sınırdır.
4. **Approval Token / Early-Exit:** Usta model değerlendirmesinde `APPROVAL_OK` (COLLAB_APPROVAL_TOKEN) token'ını ürettiği an, döngü `break` ile ANINDA kesilir — bir sonraki tura hiç geçilmez.

### Yapılandırma (Ortam Değişkenleri)

- `COLLAB_MAX_REVISION_TURNS` (varsayılan: `2`) — maksimum revizyon turu.
- `COLLAB_APPROVAL_TOKEN` (varsayılan: `APPROVAL_OK`) — usta modelin onay sinyali.
- `COLLAB_EARLY_START_CHARS` (varsayılan: `0`) — 0 ise Usta, Çırak'ın taslağını TAMAMEN bitirmesini bekler (güvenli/sıralı mod). Pozitif bir değer verilirse (örn. `150`), Usta modeli Çırak o kadar karakter ürettiği an, Çırak HALA STREAM EDERKEN paralel başlar (gerçek eş-zamanlı/iç-içe mod). **DİKKAT:** Bu, GPU'suz 4 OCPU sunucuda CPU çekişmesine yol açabilir — açmadan önce `benchmark_cpu_contention.sh` ile ölçüm yapın.
- `DRAFT_MODEL_NAME` / `USTA_MODEL_NAME` — llama-server'a gönderilen model dosya adları.

### CPU Çekişmesi (Contention) — Dürüst Değerlendirme

Bu mimarinin en kritik riski, GPU olmayan 4 OCPU'lu bir sunucuda iki modeli gerçekten paralel çalıştırmanın CPU çekirdeklerini paylaştırıp her ikisini de yavaşlatma ihtimalidir. Bunu varsayımla değil, **gerçek ölçümle** doğrulamak için `benchmark_cpu_contention.sh` betiği eklendi:

```bash
cd ~/cyber-memory-service
bash benchmark_cpu_contention.sh
```

Bu betik 3 senaryoyu (Usta Tek Başına / Sıralı Akış / Tam Paralel Akış) ölçüp bir karşılaştırma tablosu ve öneri sunar. **AI asistanının sunucuya SSH erişimi olmadığı için bu ölçüm kullanıcı tarafından manuel çalıştırılmalıdır** — sonuçlara göre `COLLAB_EARLY_START_CHARS` değerini 0'da tutmak (varsayılan, güvenli) veya CPU izolasyonu (aşağıya bakın) uygulayarak paralel modu denemek mümkündür.

### CPU İzolasyonu Önerisi (Nice / Taskset)

Eğer paralel mod denenecekse, çekişmeyi azaltmak için iki seçenek `cyber-llama-draft.service.example` ve `cyber-llama-usta.service.example` dosyalarında detaylı olarak dokümante edildi:

1. **Nice (yumuşak öncelik, önerilen ilk adım):** Çırak servisine `Nice=10` eklenir, Usta servisi varsayılan (0) öncelikte kalır — Linux zamanlayıcısı çekişme anında CPU'yu Usta'ya öncelikli verir.
2. **Taskset (sert çekirdek sabitleme, daha invaziv):** Çırak `taskset -c 0,1` ile 0-1 çekirdeklerine, Usta `taskset -c 2,3` ile 2-3 çekirdeklerine sabitlenir — çekişme tamamen sıfırlanır ama Çırak'ın kendi hızı 2 çekirdekle sınırlanır.

### Prompt Caching (Sistematikleştirme)

`SharedContext.to_system_blocks()` her turda BYTE-BİREBİR AYNI metni üretir (RAG+web verisi bir kez çekilip tüm turlarda tekrar kullanılır) — bu, llama.cpp'nin `--cache-reuse` bayrağının tam olarak hedeflediği kullanım şeklidir (`--cache-prompt` zaten varsayılan olarak açıktır). Önceki oturumda gözlemlenen 281ms sıcak-başlangıç düşüşü kısmen bu mekanizmanın zaten çalıştığını gösteriyor; `cyber-llama-draft.service.example` ve `cyber-llama-usta.service.example` dosyaları bunu SİSTEMATİK (her restart'ta kalıcı) hale getirmek için gerekli tam yapılandırmayı içerir.
**DÜZELTME NOTU:** `--prompt-cache`/`--prompt-cache-all` bayrakları önceki bir sürümde hatalı önerilmişti — llama-server bu bayrakları desteklemez (llama-cli'ye özeldir), "invalid argument" hatası verir. Doğru bayrak sadece `--cache-reuse`'dur, yukarıdaki iki `.service.example` dosyası bu şekilde güncellenmiştir.

### Yeni Dosyalar

- `collab_orchestrator.py` — Çırak-Usta streaming (token-stream) pipeline'ın tüm mantığı.
- `benchmark_cpu_contention.sh` — CPU çekişmesini ölçen benchmark betiği (sunucuda manuel çalıştırılır).
- `cyber-llama-draft.service.example` — Çırak servisi için Nice/taskset + cache-reuse örnek systemd yapılandırması.
- `cyber-llama-usta.service.example` — Usta servisi için cache-reuse + speculative decoding örnek systemd yapılandırması.

---

## ALTERNATİF MİMARİ: Cümle-Bazlı Sequential Relay (Kullanıcı Önerisi, A/B Karşılaştırma)

Kullanıcı, tam token-stream canlı besleme mimarisine (yukarıda) göre CPU çekişmesini azaltması beklenen, daha basit bir alternatif önerdi: **cümle sınırında (sentence-boundary) sıralı aktarım**. Bu, `collab_orchestrator_sentence.py` içinde uygulandı ve mevcut token-stream mimarisiyle YAN YANA (aynı API üzerinden, ayrı endpoint'lerle) sunuluyor — biri diğerinin yerine geçmedi, ikisi de test edilebilir durumda.

### Nasıl Çalışır?

1. Çırak model taslağı üretirken, `SentenceBuffer` sınıfı gelen chunk'ları biriktirir ve bir CÜMLE tamamlandığı an (nokta/ünlem/soru işareti tespit edilince) bu cümleyi hemen usta modele değerlendirmesi için gönderir.
2. Çırak, bu değerlendirmeyi BEKLEMEDEN bir sonraki cümleyi üretmeye devam eder (usta'nın değerlendirmesi arka planda `asyncio.Task` olarak çalışır).
3. Usta o cümleyi onaylarsa (`SENTENCE_OK` token'ı) cümle olduğu gibi kalır; reddederse (düzeltilmiş cümleyi döndürürse) o cümle nihai cevaba düzeltilmiş haliyle eklenir (splice-in).
4. **Dürüst sınırlama:** Çırak, üretim esnasında (mid-flight) usta'nın düzeltmesini "duyamaz" — llama.cpp API'si bunu desteklemiyor. Çırak kendi taslağını bağımsız üretmeye devam eder, usta'nın düzeltmesi sadece NİHAİ birleştirilmiş cevaba o cümlenin pozisyonunda yansır.
5. Tur mantığı burada farklı çalışır: cümle bazında reddedilme oranı `%30`'u (yapılandırılabilir `COLLAB_HIGH_REJECTION_RATIO`) aşarsa, bu tam bir revizyon turu (yeniden üretim) tetikler; aşmazsa hemen erken çıkış yapılır. Aynı `MAX_REVISION_TURNS` kesin sınırı burada da geçerlidir.

### Neden Daha Az CPU Çekişmesi Bekleniyor?

Token-stream mimarisinin erken-tetikleme modunda usta, çırağın TÜM üretim süresi boyunca paralel çalışabilir (uzun süreli çekişme). Cümle-bazlı relayde ise usta'nın her çağrısı KISA ÖMÜRLÜ'dür (tek bir cümle, düşük `max_tokens`) — çekişme penceresi çok daha kısa ve sık aralıklıdır, bu da CPU zamanlayıcısının daha adil paylaşım yapmasına imkan tanır. **Bu teorik bir beklenti — gerçek doğrulama aşağıdaki benchmark ile yapılmalı.**

### Endpoint'ler

- `POST /collab_stream_sentence` — Cümle-bazlı relay mimarisini SSE ile akıtır (`/collab_stream` ile aynı event sözleşmesi + ek `sentence_detail` event'i).
- `POST /collab_compare` — **Aynı soru için** her iki mimariyi ARDIŞIL çalıştırıp JSON karşılaştırma raporu döner (`token_stream` ve `sentence_relay` anahtarları altında `total_pipeline_seconds`, `turns_used`, `approved_early`).

### Yapılandırma (Ek Ortam Değişkenleri)

- `COLLAB_SENTENCE_APPROVAL_TOKEN` (varsayılan: `SENTENCE_OK`) — cümle bazlı onay sinyali.
- `COLLAB_HIGH_REJECTION_RATIO` (varsayılan: `0.3`) — bu oranın üzerinde reddedilme, tam revizyon turunu tetikler.

### A/B Karşılaştırma — Sunucuda Nasıl Test Edilir?

```bash
cd ~/cyber-memory-service
bash benchmark_architectures.sh
```

Bu betik 3 farklı türde soru (basit/güncel-web/teknik) ile her iki mimariyi `/collab_compare` üzerinden ölçüp bir özet tablo ve tavsiye sunar. **Bu betik mimarileri ARDIŞIL çalıştırdığı için CPU çekişmesini ölçmez** — sadece iki mimarinin kendi gecikme/tur davranışını kıyaslar. CPU çekişmesi ölçümü için hâlâ `benchmark_cpu_contention.sh` kullanılmalı (o script gerçek eş-zamanlı yük oluşturur).

### Kod Karmaşıklığı Kıyaslaması (Statik Değerlendirme)

| Kriter | Token-Stream | Cümle-Bazlı Relay |
|---|---|---|
| Asenkron yapı | `asyncio.Queue` + canlı SSE parse | `SentenceBuffer` (regex) + `asyncio.Task` listesi |
| Usta çağrı sayısı (tur başına) | 1 (tüm taslağı bir kerede okur) | N (N = cümle sayısı, her biri ayrı HTTP round-trip) |
| Usta çağrı süresi (ortalama) | Uzun (tüm taslağı değerlendirir) | Kısa (tek cümle, düşük max_tokens) |
| CPU çekişme penceresi | Potansiyel olarak uzun (erken-start modunda) | Kısa ve sık aralıklı |
| Determinizm / hata ayıklama kolaylığı | Orta (stream parse hataları izlenmeli) | Yüksek (her cümle bağımsız izlenebilir, `sentence_detail` event'i ile) |
| Bilinen zayıf nokta | Gerçek token-iç-içe değil (llama.cpp API kısıtı) | Cümle sınırı heuristiği kusursuz değil (örn. "Dr." gibi kısaltmalar) + fazladan HTTP overhead |

### Öneri (Gerçek Ölçüm Öncesi Ön Değerlendirme)

Kod karmaşıklığı ve teorik CPU-çekişme profili açısından **Cümle-Bazlı Relay mimarisi (Mimarî B) birincil öncelik olarak önerilir** — çünkü:
1. Usta'nın her çağrısı kısa ömürlü olduğu için GPU'suz 4 OCPU sunucuda çekişme riski daha düşük.
2. Kod akışı daha deterministik (her cümle bağımsız izlenip loglanabiliyor).
3. Hata durumunda etkilenen kapsam daha küçük (bir cümlenin usta çağrısı başarısız olursa sadece o cümle etkilenir, tüm taslak değil).

Ancak bu **ölçülmeden verilen bir ön değerlendirmedir** — `benchmark_architectures.sh` ve `benchmark_cpu_contention.sh` sonuçları geldiğinde bu öneri gerçek verilerle teyit edilmeli veya güncellenmelidir. Token-Stream mimarisi ikincil/opsiyonel olarak kod tabanında bırakıldı (kaldırılmadı) — `/collab_stream` üzerinden hâlâ kullanılabilir.