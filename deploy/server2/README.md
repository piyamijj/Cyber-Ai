# =====================================================================================
# CYBER AI - SUNUCU 2 (DAĞITIK MİMARİ) SIFIRDAN KURULUM REHBERİ (README.md)
# =====================================================================================
# Bu rehber, yeni ikinci sunucunuzda (Server 2, 79.76.38.185, 4 OCPU, 64GB RAM, GPU YOK)
# Docker tabanlı dağıtık mimariyi sıfırdan kurmanız için adım adım bir kılavuzdur.
#
# NEDEN DAĞITIK MİMARİ?
# ---------------------
# - Çırak model (0.5B) ve Qdrant Vektör Veritabanı bu ikinci sunucuya taşınarak,
#   Sunucu 1'deki (79.76.63.191) 14B Usta modelin CPU üzerindeki yükü tamamen sıfırlanır.
# - Usta model artık Sunucu 1'in 4 OCPU çekirdeğini tek başına ve tam verimle kullanır.
# - IP Geolocation analizimize göre, her iki sunucunuz da Oracle Cloud Stockholm (İsveç)
#   veri merkezinde yer almaktadır. Bu sayede aralarındaki ağ gecikmesi (network latency)
#   son derece düşüktür (tipik olarak <2ms). Bu ihmal edilebilir ağ maliyeti, CPU kilitlenmesini
#   kökten çözmenin getirdiği büyük performans kazancının yanında tamamen önemsizdir.
# =====================================================================================

## ADIM 1: SUNUCU 2'YE SSH İLE BAĞLANIN
Kendi yerel terminalinizden (veya Termux ortamınızdan) yeni boş Sunucu 2'ye SSH ile bağlanın:
```bash
ssh ubuntu@79.76.38.185
```
*(Eğer kullanıcı adınız farklıysa 'ubuntu' kısmını güncelleyin).*

---

## ADIM 2: SİSTEM GÜNCELLEMELERİNİ YAPIN
Sunucuya bağlandıktan sonra paket listelerini güncelleyin ve mevcut paketleri yükseltin:
```bash
sudo apt update && sudo apt upgrade -y
```

---

## ADIM 3: DOCKER VE DOCKER COMPOSE KURULUMUNU YAPIN
Ubuntu'nun eski ve kararsız varsayılan paketleri yerine, resmi Docker deposunu ekleyerek en güncel Docker Engine ve Docker Compose v2 eklentisini sıfırdan kurun:

```bash
# 1. Gerekli ön hazırlık paketlerini yükleyin
sudo apt install -y ca-certificates curl gnupg

# 2. Docker resmi GPG anahtarını ekleyin
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 3. Docker resmi apt deposunu sistem kaynaklarına ekleyin
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 4. Paket listesini güncelleyin ve Docker bileşenlerini yükleyin
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 5. Kurulumu doğrulayın (Sürümleri görmelisiniz)
docker --version
docker compose version

# 6. Docker komutlarını 'sudo' olmadan çalıştırabilmek için kullanıcınızı Docker grubuna ekleyin
sudo usermod -aG docker $USER

# 7. Grup değişikliklerinin aktif olması için oturumu yenileyin (veya çıkış yapıp tekrar bağlanın)
newgrp docker
```

---

## ADIM 4: GİTHUB REPOSUNU CLONE EDİN
Kurulum dosyalarını içeren GitHub reponuzu Sunucu 2'ye çekin ve ilgili dizine gidin:
```bash
git clone https://github.com/piyamijj/Cyber-Ai.git ~/cyber-ai-repo
cd ~/cyber-ai-repo/deploy/server2
```

---

## ADIM 5: MODEL DİZİNLERİNİ HAZIRLAYIN VE ÇIRAK MODELİ YÜKLEYİN
Model dosyaları ve prompt cache için gerekli klasörleri oluşturun:
```bash
mkdir -p models llama-cache
```

Çırak model dosyasını (`qwen2.5-0.5b-instruct-q4_k_m.gguf`) bu klasöre yerleştirmeniz gerekir. İki seçeneğiniz vardır:

### Seçenek A (Önerilen - Hızlı): Sunucu 1'den Doğrudan Kopyalama
Model dosyası zaten Sunucu 1'de mevcut olduğu için, internetten tekrar indirmek yerine SCP ile doğrudan sunucudan sunucuya çekmek çok daha hızlıdır. Sunucu 2 terminalindeyken şu komutla dosyayı çekin:
```bash
# NOT: Sunucu 1'e erişim için kullandığınız SSH anahtarının Sunucu 2'de tanımlı olması gerekir.
# Veya bu komutu kendi Termux/yerel bilgisayarınızdan iki sunucu arasında köprü kurarak da çalıştırabilirsiniz.
scp ubuntu@79.76.63.191:/home/ubuntu/models/qwen2.5-0.5b-instruct-q4_k_m.gguf ./models/
```

### Seçenek B: HuggingFace Üzerinden Yeniden İndirme
Eğer sunucular arası SCP bağlantısı kuramazsanız, resmi HuggingFace deposundan doğrudan Sunucu 2'ye indirin:
```bash
cd models
wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
cd ..
```
*(Dosya adının `qwen2.5-0.5b-instruct-q4_k_m.gguf` olduğundan emin olun).*

---

## ADIM 6: DOCKER KONTEYNERLERİNİ BAŞLATIN
`docker-compose.yml` dosyasındaki model adının indirdiğiniz dosya adıyla eşleştiğini doğruladıktan sonra konteynerleri arka planda derleyip ayağa kaldırın:

```bash
# Konteynerleri arka planda başlatın
docker compose up -d --build

# Çalışan konteynerleri kontrol edin (cyber-qdrant ve cyber-llama-draft ayakta olmalı)
docker compose ps

# Logları izleyerek servislerin hazır olduğunu doğrulayın
docker compose logs -f
```
*   **Qdrant için loglarda:** `Qdrant HTTP listening on 6333` mesajını görmelisiniz.
*   **Llama-server için loglarda:** Model yükleme adımlarının bittiğini ve `HTTP server listening` mesajını görmelisiniz.
*   *Log izleme ekranından çıkmak için `Ctrl+C` tuşlarına basın (Konteynerler çalışmaya devam eder).*

---

## ADIM 7: GÜVENLİK DUVARI KURALLARINI UYGULAYIN (KRİTİK!)
Sunucu 2'deki Qdrant ve Çırak model portlarının dış dünyaya tamamen açık kalması ciddi bir güvenlik riskidir.
**Hemen şimdi aynı dizindeki `SECURITY.md` dosyasını açın ve oradaki adımları uygulayarak:**
1. Oracle Cloud Console üzerinden,
2. Ve Ubuntu UFW üzerinden,
6333, 6334 ve 8088 portlarına **SADECE Sunucu 1'in (79.76.63.191) IP adresinden** erişim izni verin. Diğer tüm internet trafiğini engelleyin.

---

## ADIM 8: SUNUCULAR ARASI GERÇEK AĞ GECİKMESİNİ ÖLÇÜN
Güvenlik kurallarını uyguladıktan sonra, Sunucu 1'e (79.76.63.191) SSH ile bağlanın ve Sunucu 2'ye olan gerçek ağ erişim hızını test edin:

```bash
# Qdrant erişim süresi testi
curl -w "\nToplam Sure: %{time_total}s\n" -o /dev/null -s http://79.76.38.185:6333/healthz

# Çırak model erişim süresi testi
curl -w "\nToplam Sure: %{time_total}s\n" -o /dev/null -s http://79.76.38.185:8088/health
```
*   **Değerlendirme:** Eğer dönen `Toplam Sure` değerleri tutarlı olarak **0.005s (5ms) veya daha altında** ise, iki sunucu arasındaki ağ maliyeti yok denecek kadar azdır. Dağıtık mimariye tam bir güvenle geçebilirsiniz.
*   *Eğer süre 0.100s (100ms) üzerindeyse, beklenmeyen bir yönlendirme hatası veya bölge uyuşmazlığı olabilir. Geçişi durdurup ağ ayarlarını kontrol edin.*

---

## ADIM 9: MEVCUT QDRANT VERİLERİNİ SUNUCU 1'DEN TAŞIYIN
Sunucu 1'deki (79.76.63.191) mevcut geçmiş hafıza kayıtlarınızı Sunucu 2'ye aktarmak için:
1. **Sunucu 1'e** SSH ile bağlanın.
2. Reponun güncel halini Sunucu 1'e çekin (`git pull`).
3. `deploy/server2` dizinine gidin.
4. `.env.example` dosyasını `.env` olarak kopyalayıp içindeki IP adreslerini doğrulayın.
5. Taşıma betiğini çalıştırın:
```bash
bash migrate_qdrant.sh
```
Betik bittiğinde kaynak ve hedefteki kayıt sayılarını karşılaştıracaktır. Sayılar eşitse (`points_count` eşleşiyorsa) taşıma başarıyla tamamlanmıştır.

---

## ADIM 10: SUNUCU 1'DEKİ HAFIZA SERVİSİNİ GÜNCELLEYİN
Veriler taşındığına göre, artık Sunucu 1'deki `cyber-memory` servisinin yönünü Sunucu 2'ye çevirebiliriz:

1. Sunucu 1'de, güncellenmiş olan `cyber-memory.service` dosyasını systemd dizinine kopyalayın:
```bash
sudo cp ~/cyber-ai-repo/cyber-memory-service/cyber-memory.service /etc/systemd/system/cyber-memory.service
```
2. Systemd yapılandırmasını yenileyin ve servisi yeniden başlatın:
```bash
sudo systemctl daemon-reload
sudo systemctl restart cyber-memory
```
3. Servisin durumunu ve loglarını izleyin:
```bash
sudo systemctl status cyber-memory
journalctl -u cyber-memory -f
```
*Loglarda Qdrant bağlantısının başarıyla kurulduğunu ve Sunucu 2'deki (79.76.38.185) adrese bağlandığını doğrulamalısınız.*

---

## ADIM 11: SİSTEMİ UÇTAN UCA TEST EDİN
Canlı web sitenizi (https://www.cyberci.duckdns.org) açın ve yeni bir sohbet başlatarak test edin.
Sohbet akışının pürüzsüz olduğunu, RAG (hafıza) kayıtlarının doğru şekilde geldiğini ve usta-çırak işbirliğinin çalıştığını doğrulayın.

---

## ADIM 12: ESKİ VERİLERİ TEMİZLEYİN (OPSİYONEL)
Sistemin dağıtık mimaride en az 24 saat sorunsuz çalıştığından emin olduktan sonra, Sunucu 1'de disk alanı kazanmak için eski yerel Qdrant konteynerini durdurup silebilirsiniz:
```bash
# Sadece Sunucu 1'de çalıştırın (Emin olduktan sonra!)
docker ps # eski qdrant konteyner adını bulun
docker stop <eski-qdrant-konteyner-id>
docker rm <eski-qdrant-konteyner-id>
```

---

## SIK KARŞILAŞILAN SORUNLAR VE ÇÖZÜMLERİ (TROUBLESHOOTING)

### 1. Docker komutlarında "Permission Denied" Hatası
*   **Neden:** Kullanıcınız Docker grubuna eklenmiş ama oturum yenilenmemiştir.
*   **Çözüm:** `newgrp docker` komutunu çalıştırın veya SSH oturumunu kapatıp yeniden bağlanın.

### 2. Konteyner Başlatılırken Port Çakışması (Port already in use)
*   **Neden:** Sunucu 2'de halihazırda 6333 veya 8088 portunu kullanan başka bir servis çalışıyordur.
*   **Çözüm:** `sudo lsof -i :6333` veya `sudo lsof -i :8088` ile portu kullanan süreci bulun ve durdurun.

### 3. Llama-server Loglarında "Model file not found" Hatası
*   **Neden:** `docker-compose.yml` içindeki model dosya adı ile `./models` klasörüne indirdiğiniz dosya adı birebir eşleşmiyordur.
*   **Çözüm:** Dosya adlarını kontrol edin, compose dosyasındaki `command:` alanındaki dosya adını indirdiğiniz dosya adıyla birebir aynı olacak şekilde güncelleyin ve `docker compose up -d` komutunu tekrar çalıştırın.

### 4. Sunucu 1'den Sunucu 2'ye Bağlantı Kurulamıyor (Connection Refused / Timeout)
*   **Neden:** `SECURITY.md` içindeki güvenlik duvarı kuralları yanlış uygulanmış veya Oracle Cloud Security List kuralı aktif edilmemiştir.
*   **Çözüm:** Sunucu 2'de `sudo ufw status` ile kuralları inceleyin. Oracle Cloud panelinde Sunucu 1'in IP'sinin doğru yazıldığından (`/32` ekiyle) emin olun.