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