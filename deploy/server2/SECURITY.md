# =====================================================================================
# CYBER AI - DAĞITIK MİMARİ GÜVENLİK VE ERİŞİM KONTROL REHBERİ (SECURITY.md)
# =====================================================================================
# Bu rehber, Sunucu 2'de (79.76.38.185) Docker ile ayağa kaldıracağınız Qdrant (6333/6334)
# ve Çırak model (8088) servislerinin güvenliğini sağlamak için hazırlanmıştır.
#
# NEDEN BU AYARLAR ZORUNLUDUR?
# ----------------------------
# Varsayılan olarak Docker portları dış dünyaya tamamen açık (0.0.0.0) olarak bağlar.
# Eğer bu portları sınırlandırmazsanız, internetteki HERHANGİ BİRİ:
#   1. Qdrant (6333) portunuza bağlanıp tüm geçmiş RAG hafıza kayıtlarınızı okuyabilir/silebilir.
#   2. Çırak (8088) portunuza bağlanıp sunucu kaynaklarınızı (CPU/RAM) kötüye kullanabilir.
#
# Bu nedenle, bu servislerin dış dünyaya kapatılması ve SADECE Sunucu 1'in (79.76.63.191)
# IP adresinden gelen isteklere izin verilmesi KESİNLİKLE ZORUNLUDUR.
# =====================================================================================

## 1. KATMAN: ORACLE CLOUD SECURITY LIST (BULUT SEVİYESİ GÜVENLİK)

Oracle Cloud altyapısında, sanal makinenize (VM) gelen trafik işletim sistemine ulaşmadan önce bulut seviyesindeki "Security List" (Güvenlik Listesi) kuralları tarafından filtrelenir. Bu en güvenilir ve ilk savunma hattıdır.

### Adım Adım Yapılandırma:
1. **Oracle Cloud Console** hesabınıza giriş yapın.
2. Sol menüden **Networking > Virtual Cloud Networks** (Sanal Bulut Ağları) bölümüne gidin.
3. Sunucu 2'nin (79.76.38.185) bağlı olduğu **VCN** adına tıklayın.
4. Sol menüden **Security Lists** (Güvenlik Listeleri) seçeneğine tıklayın.
5. Sunucu 2'nin subnet'ine (alt ağ) bağlı olan aktif Güvenlik Listesine tıklayın (genelde "Default Security List for..." adındadır).
6. **Add Ingress Rules** (Giriş Kuralları Ekle) butonuna tıklayın.
7. Aşağıdaki 3 kuralı sırayla ekleyin (Her port için ayrı bir kural):

#### Kural 1: Qdrant REST API (Port 6333)
*   **Source Type:** CIDR
*   **Source CIDR:** `79.76.63.191/32` *(Sunucu 1'in tam IP adresi. /32 eki sadece bu tek IP'ye izin verir)*
*   **IP Protocol:** TCP
*   **Source Port Range:** All (Boş bırakın)
*   **Destination Port Range:** `6333`
*   **Description:** Cyber AI - Sunucu 1 Qdrant REST API Izni

#### Kural 2: Qdrant gRPC (Port 6334)
*   **Source Type:** CIDR
*   **Source CIDR:** `79.76.63.191/32`
*   **IP Protocol:** TCP
*   **Source Port Range:** All (Boş bırakın)
*   **Destination Port Range:** `6334`
*   **Description:** Cyber AI - Sunucu 1 Qdrant gRPC Izni

#### Kural 3: Çırak Model API (Port 8088)
*   **Source Type:** CIDR
*   **Source CIDR:** `79.76.63.191/32`
*   **IP Protocol:** TCP
*   **Source Port Range:** All (Boş bırakın)
*   **Destination Port Range:** `8088`
*   **Description:** Cyber AI - Sunucu 1 Cirak Model API Izni

> ⚠️ **KRİTİK UYARI:** Bu kuralların hiçbirinde kaynak (Source CIDR) olarak `0.0.0.0/0` (tüm internet) kullanmayın! Sadece Sunucu 1'in IP'sini (`79.76.63.191/32`) yazın.
>
> *Not: Eğer VCN'inizde klasik Security List yerine daha yeni olan "Network Security Groups (NSG)" kullanıyorsanız, aynı kuralları ilgili NSG içine eklemeniz gerekir.*

---

## 2. KATMAN: UBUNTU UFW FIREWALL (İŞLETİM SİSTEMİ SEVİYESİ GÜVENLİK)

Bulut seviyesindeki kuralların yanlışlıkla silinmesi veya değiştirilmesi ihtimaline karşı, Sunucu 2'nin kendi içinde de işletim sistemi seviyesinde bir güvenlik duvarı (UFW) kurmak "Savunma Derinliği" (Defense in Depth) açısından mükemmel bir ek güvenlik sağlar.

### ⚠️ ÇOK ÖNEMLİ - KİLİTLENME UYARISI:
UFW'yi aktif etmeden önce **SSH portunuza (varsayılan 22)** kesinlikle izin vermelisiniz. Aksi takdirde sunucuyla olan SSH bağlantınız anında kesilir ve sunucuya bir daha KESİNLİKLE ERİŞEMEZSİNİZ (Kilitlenirsiniz).

### Adım Adım UFW Yapılandırması:

```bash
# 1. Adım: SSH bağlantınızın kesilmemesi için SSH portuna izin verin (ZORUNLU!)
sudo ufw allow 22/tcp

# 2. Adım: Sadece Sunucu 1'in IP'sinden (79.76.63.191) Qdrant REST portuna (6333) izin verin
sudo ufw allow from 79.76.63.191 to any port 6333 proto tcp

# 3. Adım: Sadece Sunucu 1'in IP'sinden Qdrant gRPC portuna (6334) izin verin
sudo ufw allow from 79.76.63.191 to any port 6334 proto tcp

# 4. Adım: Sadece Sunucu 1'in IP'sinden Çırak Model portuna (8088) izin verin
sudo ufw allow from 79.76.63.191 to any port 8088 proto tcp

# 5. Adım: Güvenlik duvarını aktif edin (Size onay soracaktır, 'y' yazıp Enter'a basın)
sudo ufw enable

# 6. Adım: Kuralların doğru uygulandığını ve aktif olduğunu doğrulayın
sudo ufw status verbose
```

---

## ALTERNATİF: IPTABLES KULLANIMI (UFW YERİNE)

Eğer sunucunuzda UFW yüklü değilse veya doğrudan ham `iptables` kurallarını kullanmayı tercih ediyorsanız, aşağıdaki komutları sırayla çalıştırabilirsiniz.

> *Not: iptables kurallarında sıra çok önemlidir. İzin verme (ACCEPT) kuralları, engelleme (DROP) kurallarından ÖNCE gelmelidir.*

```bash
# 1. Sunucu 1'den gelen isteklere izin ver (ACCEPT)
sudo iptables -A INPUT -p tcp -s 79.76.63.191 --dport 6333 -j ACCEPT
sudo iptables -A INPUT -p tcp -s 79.76.63.191 --dport 6334 -j ACCEPT
sudo iptables -A INPUT -p tcp -s 79.76.63.191 --dport 8088 -j ACCEPT

# 2. Diğer tüm IP'lerden gelen istekleri engelle (DROP)
sudo iptables -A INPUT -p tcp --dport 6333 -j DROP
sudo iptables -A INPUT -p tcp --dport 6334 -j DROP
sudo iptables -A INPUT -p tcp --dport 8088 -j DROP

# 3. Kuralların reboot (yeniden başlatma) sonrası kalıcı olması için kaydedin
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save
```

---

## 3. KATMAN: GÜVENLİK DOĞRULAMA TESTİ (VERIFICATION)

Kuralları uyguladıktan sonra, sistemin gerçekten dış dünyaya kapalı ve sadece Sunucu 1'e açık olduğunu doğrulamak için şu iki testi yapın:

### Test 1: Sunucu 1'den Erişim Testi (BAŞARILI OLMALI)
Sunucu 1'e (79.76.63.191) SSH ile bağlanın ve Sunucu 2'deki Qdrant'a istek atın:
```bash
curl -I http://79.76.38.185:6333/healthz
```
*   **Beklenen Sonuç:** `HTTP/1.1 200 OK` yanıtı anında dönmelidir.

### Test 2: Dış Dünyadan Erişim Testi (ENGELLENMELİ)
Kendi kişisel bilgisayarınızdan (veya sunucular dışındaki herhangi bir internet ağından) aynı isteği atın:
```bash
curl --connect-timeout 5 -I http://79.76.38.185:6333/healthz
```
*   **Beklenen Sonuç:** İstek **zaman aşımına uğramalı** (Connection timed out) veya bağlantı reddedilmelidir. Eğer bu istek başarılı olursa, güvenlik kurallarınız ÇALIŞMIYOR demektir. Adımları baştan kontrol edin.

---
*Güvenlik kurallarını Docker Compose servislerini canlıya almadan hemen önce veya alır almaz uygulamayı unutmayın. Güvenli siber dünyalar dileriz!*