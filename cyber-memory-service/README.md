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

`SharedContext.to_system_blocks()` her turda BYTE-BİREBİR AYNI metni üretir (RAG+web verisi bir kez çekilip tüm turlarda tekrar kullanılır) — bu, llama.cpp'nin `--prompt-cache`, `--prompt-cache-all` ve `--cache-reuse` bayraklarının tam olarak hedeflediği kullanım şeklidir. Önceki oturumda gözlemlenen 281ms sıcak-başlangıç düşüşü kısmen bu mekanizmanın zaten çalıştığını gösteriyor; `cyber-llama-draft.service.example` ve `cyber-llama-usta.service.example` dosyaları bunu SİSTEMATİK (her restart'ta kalıcı, disk üzerinde) hale getirmek için gerekli tam yapılandırmayı içerir.

### Yeni Dosyalar

- `collab_orchestrator.py` — Çırak-Usta streaming pipeline'ın tüm mantığı.
- `benchmark_cpu_contention.sh` — CPU çekişmesini ölçen benchmark betiği (sunucuda manuel çalıştırılır).
- `cyber-llama-draft.service.example` — Çırak servisi için Nice/taskset + prompt-cache örnek systemd yapılandırması.
- `cyber-llama-usta.service.example` — Usta servisi için prompt-cache + speculative decoding örnek systemd yapılandırması.