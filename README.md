# Cyber AI — Sade Sohbet Arayüzü

Next.js tabanlı, Oracle Cloud üzerinde çalışan Qwen2.5-14B (llama.cpp server) modelini kullanan, sade ve karanlık temalı modern bir web sohbet arayüzü.

## Nasıl Çalışır?

1. **Next.js Arayüzü:** Kullanıcı dostu, hızlı ve tamamen karanlık temalı (cyberpunk esintili) bir sohbet ekranı sunar.
2. **Güvenli Proxy Backend (/api/chat):** Tarayıcınız asla doğrudan Oracle sunucunuza (`79.76.63.191:8082`) istek atmaz. Bunun yerine istekler Vercel üzerinde çalışan güvenli bir API rotasına (`/api/chat`) gönderilir. Bu proxy:
   - Oracle sunucunuzun IP adresini tarayıcı konsolundan ve ağ isteklerinden gizler.
   - Vercel'in HTTPS (güvenli) sayfası üzerinden HTTP (güvensiz) bir IP adresine istek atarken tarayıcıların uyguladığı **Mixed Content** (Karışık İçerik) engelini tamamen aşar.
   - OpenAI-uyumlu streaming (akış) desteğini doğrudan tarayıcıya iletir.
3. **Yerel Depolama (localStorage):** Sohbet geçmişiniz tamamen tarayıcınızın `localStorage` alanında saklanır. Ağır bir veritabanı kurulumu gerektirmez, verileriniz cihazınızda kalır.
4. **RAG & Akıllı Hafıza (opsiyonel, `../cyber-memory-service`):** Oracle sunucunuzda ayrıca çalışan bir hafıza/arama servisi varsa, proxy her soru öncesi bu servise danışıp geçmişte kaydedilmiş alakalı bilgileri bulur ve modele ekler; cevap bittikten sonra da arka planda konuşmayı analiz ettirip önemli bilgileri otomatik olarak hafızaya kaydeder. Bu servis kapalıysa veya yanıt vermiyorsa sohbet normal şekilde (RAG'siz) çalışmaya devam eder — asla sohbeti bloklamaz.

## Yerel Geliştirme (Local Development)

Projeyi kendi bilgisayarınızda çalıştırmak için:

```bash
# Bağımlılıkları yükleyin
npm install

# Geliştirme sunucusunu başlatın
npm run dev
```

Tarayıcınızda `http://localhost:3000` adresini açarak arayüzü kullanabilirsiniz.

## Ortam Değişkenleri (Environment Variables)

Projede kullanılan tek bir opsiyonel ortam değişkeni vardır:

- `LLAMA_SERVER_URL`: Oracle Cloud üzerinde çalışan llama.cpp sunucunuzun adresi.
  - *Varsayılan Değer:* `http://79.76.63.191:8082` (Belirtilmezse otomatik olarak bu IP kullanılır).
  - Sunucu adresiniz veya portunuz değişirse, kodu güncellemek yerine Vercel panelinden bu değişkeni yeni adresle güncellemeniz yeterlidir.
- `MEMORY_SERVICE_URL`: Oracle sunucunuzdaki RAG/hafıza mikroservisinin adresi (bkz. `../cyber-memory-service`).
  - *Varsayılan Değer:* `http://79.76.63.191:8083`.
  - Bu servis yoksa veya kapalıysa hiçbir şey bozulmaz, sohbet RAG'siz devam eder.

## Vercel'e Deploy Etme

Projeyi Vercel üzerinde canlıya almak son derece basittir:

1. Bu GitHub reposunu kendi GitHub hesabınıza forklayın veya aktarın.
2. [Vercel](https://vercel.com) paneline gidin ve **Add New > Project** seçeneğini seçin.
3. GitHub hesabınızı bağlayıp `Cyber-Ai` reposunu bulun ve **Import** butonuna tıklayın.
4. (Opsiyonel) **Environment Variables** bölümünde `LLAMA_SERVER_URL` anahtarını ekleyip değerine Oracle sunucu adresinizi yazabilirsiniz. Eklemezseniz varsayılan IP kullanılacaktır.
5. **Deploy** butonuna tıklayın. Vercel projeyi otomatik olarak Next.js olarak algılayacak, derleyecek ve size canlı bir HTTPS bağlantısı verecektir.

## Not

Bu arayüz, kullanıcının tercihi doğrultusunda herhangi bir şifreleme veya kimlik doğrulama (auth) mekanizması içermez. Canlıya alınan Vercel linkine sahip olan herkes sohbet arayüzünü doğrudan kullanabilir.