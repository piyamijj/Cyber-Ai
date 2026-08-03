#!/bin/bash

# =====================================================================================
# CYBER AI - QDRANT HAFIZA VERİ TAŞIMA (MIGRATION) BETİĞİ
# =====================================================================================
# Bu betik, Sunucu 1'deki (79.76.63.191) mevcut Qdrant vektör veritabanınızda biriken
# tüm geçmiş RAG hafıza kayıtlarını (cyber_memory koleksiyonu), Sunucu 2'deki (79.76.38.185)
# yeni boş Qdrant veritabanına güvenli bir şekilde taşımak (kopyalamak) için tasarlanmıştır.
#
# ÖNEMLİ ÇALIŞTIRMA NOTU:
# -----------------------
# - BU BETİK SUNUCU 1 ÜZERİNDE ÇALIŞTIRILMALIDIR!
# - Çalıştırmadan önce Sunucu 2'deki Docker Compose servislerinin (docker-compose up -d)
#   başlatılmış ve Sunucu 2'deki Qdrant'ın (port 6333) erişilebilir olduğundan emin olun.
# - Bu betik Sunucu 1'deki verileri KESİNLİKLE SİLMEZ, sadece bir kopyasını Sunucu 2'ye aktarır.
# =====================================================================================

# 1. YAPILANDIRMA VE ORTAM DEĞİŞKENLERİ
# Eğer aynı dizinde .env dosyası varsa değişkenleri oradan okuyoruz
if [ -f .env ]; then
    source .env
fi

# Varsayılan değerler (eğer .env dosyasında tanımlanmadıysa)
COLLECTION="${QDRANT_MIGRATION_COLLECTION:-cyber_memory}"
SERVER2_IP="${SERVER2_IP:-79.76.38.185}"
SERVER2_PORT="6333"
SOURCE_URL="http://localhost:6333"
DEST_URL="http://${SERVER2_IP}:${SERVER2_PORT}"

# Renkli çıktılar için tanımlamalar
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}          CYBER AI - QDRANT VERİ TAŞIMA İŞLEMİ BAŞLATILIYOR           ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "Kaynak Qdrant (Sunucu 1): ${SOURCE_URL}"
echo -e "Hedef Qdrant (Sunucu 2): ${DEST_URL}"
echo -e "Taşınacak Koleksiyon: ${COLLECTION}"
echo ""

# 2. BAĞLANTI VE GEREKSİNİM KONTROLLERİ
if ! command -v curl &> /dev/null; then
    echo -e "${RED}HATA: 'curl' komutu bulunamadı. Lütfen yükleyin (sudo apt install curl).${NC}"
    exit 1
fi

HAS_JQ=0
if command -v jq &> /dev/null; then
    HAS_JQ=1
fi

# Kaynak Qdrant kontrolü
echo -n "Kaynak Qdrant (Sunucu 1) kontrol ediliyor... "
if ! curl -s --connect-timeout 3 "${SOURCE_URL}/healthz" &>/dev/null; then
    echo -e "${RED}BAĞLANTI BAŞARISIZ!${NC}"
    echo -e "Sunucu 1'deki yerel Qdrant veritabanının çalıştığından emin olun."
    exit 1
fi
echo -e "${GREEN}AKTİF${NC}"

# Kaynak koleksiyon kontrolü
echo -n "Kaynak koleksiyon ('${COLLECTION}') kontrol ediliyor... "
if ! curl -s -f "${SOURCE_URL}/collections/${COLLECTION}" &>/dev/null; then
    echo -e "${RED}BULUNAMADI!${NC}"
    echo -e "Sunucu 1'de '${COLLECTION}' adında bir koleksiyon bulunamadı. Lütfen adı kontrol edin."
    exit 1
fi
echo -e "${GREEN}MEVCUT${NC}"

# Hedef Qdrant kontrolü
echo -n "Hedef Qdrant (Sunucu 2) kontrol ediliyor... "
if ! curl -s --connect-timeout 5 "${DEST_URL}/healthz" &>/dev/null; then
    echo -e "${RED}BAĞLANTI BAŞARISIZ!${NC}"
    echo -e "Sunucu 2'deki Qdrant veritabanına ulaşılamadı. Lütfen şunları kontrol edin:"
    echo -e "  1. Sunucu 2'de Docker konteynerlerinin ayakta olduğunu (docker ps)."
    echo -e "  2. Sunucu 2'nin güvenlik duvarında (veya Oracle Cloud Security List) 6333 portunun"
    echo -e "     Sunucu 1'in IP'sine (${SERVER1_IP:-açık}) izin verdiğini."
    exit 1
fi
echo -e "${GREEN}AKTİF${NC}"
echo ""

# 3. TAŞIMA ADIMLARI (SNAPSHOT METODU)
echo -e "${YELLOW}--- ADIM 1: Sunucu 1'de Koleksiyon Snapshot'ı Oluşturuluyor ---${NC}"
# Qdrant REST API ile kaynak sunucuda anlık bir yedek (snapshot) oluşturuyoruz
snapshot_response=$(curl -s -X POST "${SOURCE_URL}/collections/${COLLECTION}/snapshots")

if [ -z "$snapshot_response" ] || echo "$snapshot_response" | grep -q "error"; then
    echo -e "${RED}HATA: Snapshot oluşturulamadı!${NC}"
    echo "Yanıt: $snapshot_response"
    exit 1
fi

# Snapshot adını JSON'dan ayıklıyoruz
SNAPSHOT_NAME=""
if [ "$HAS_JQ" -eq 1 ]; then
    SNAPSHOT_NAME=$(echo "$snapshot_response" | jq -r ".result.name")
else
    # JQ yoksa regex/sed fallback
    SNAPSHOT_NAME=$(echo "$snapshot_response" | grep -o '"name":"[^"]*' | head -n 1 | cut -d'"' -f4)
fi

if [ -z "$SNAPSHOT_NAME" ] || [ "$SNAPSHOT_NAME" = "null" ]; then
    echo -e "${RED}HATA: Snapshot adı alınamadı!${NC}"
    echo "Yanıt: $snapshot_response"
    exit 1
fi

echo -e "${GREEN}Başarılı! Snapshot oluşturuldu: ${SNAPSHOT_NAME}${NC}"
echo ""

echo -e "${YELLOW}--- ADIM 2: Snapshot Dosyası Sunucu 1'e İndiriliyor ---${NC}"
# Oluşturulan snapshot dosyasını yerel diske indiriyoruz
snapshot_file="${COLLECTION}_migration.snapshot"
echo "İndiriliyor: ${snapshot_file}..."

curl_code=$(curl -s -w "%{http_code}" -o "$snapshot_file" "${SOURCE_URL}/collections/${COLLECTION}/snapshots/${SNAPSHOT_NAME}")

if [ "$curl_code" -ne 200 ]; then
    echo -e "${RED}HATA: Snapshot dosyası indirilemedi! HTTP Kodu: $curl_code${NC}"
    rm -f "$snapshot_file"
    exit 1
fi

echo -e "${GREEN}Başarılı! Snapshot dosyası yerel olarak kaydedildi (Yedek olarak saklanacaktır).${NC}"
echo ""

echo -e "${YELLOW}--- ADIM 3: Snapshot Sunucu 2'ye Yükleniyor ve Geri Yükleniyor (Restore) ---${NC}"
# İndirdiğimiz snapshot dosyasını Sunucu 2'deki Qdrant'a multipart upload ile yüklüyoruz.
# Qdrant'ın '/snapshots/upload' endpoint'i dosyayı alır, koleksiyonu otomatik oluşturur ve verileri yazar.
echo "Sunucu 2'ye yükleniyor (bu işlem veri boyutuna göre biraz sürebilir)..."

restore_response=$(curl -s -X POST "${DEST_URL}/collections/${COLLECTION}/snapshots/upload?priority=snapshot" \
    -H "Content-Type: multipart/form-data" \
    -F "snapshot=@${snapshot_file}")

if [ -z "$restore_response" ] || echo "$restore_response" | grep -q "error" || ! echo "$restore_response" | grep -q "ok"; then
    echo -e "${RED}HATA: Sunucu 2'ye geri yükleme (restore) başarısız oldu!${NC}"
    echo "Yanıt: $restore_response"
    echo -e "${YELLOW}İpucu: Eğer Sunucu 2'de '${COLLECTION}' koleksiyonu zaten varsa çakışma olmuş olabilir.${NC}"
    exit 1
fi

echo -e "${GREEN}Başarılı! Veriler Sunucu 2'deki Qdrant'a başarıyla aktarıldı.${NC}"
echo ""

# 4. DOĞRULAMA VE KARŞILAŞTIRMA
echo -e "${YELLOW}--- ADIM 4: Veri Tutarlılığı Doğrulanıyor ---${NC}"

# Kaynak ve hedefteki toplam kayıt (point) sayılarını çekiyoruz
get_points_count() {
    local url="$1"
    local res=$(curl -s "${url}/collections/${COLLECTION}")
    if [ "$HAS_JQ" -eq 1 ]; then
        echo "$res" | jq -r ".result.points_count" 2>/dev/null
    else
        echo "$res" | grep -o '"points_count":[^,]*' | head -n 1 | cut -d':' -f2 | tr -d ' '
    fi
}

source_count=$(get_points_count "$SOURCE_URL")
dest_count=$(get_points_count "$DEST_URL")

echo -e "Kaynak Sunucu (Sunucu 1) Toplam Kayıt Sayısı: ${BLUE}${source_count}${NC}"
echo -e "Hedef Sunucu (Sunucu 2) Toplam Kayıt Sayısı:  ${BLUE}${dest_count}${NC}"
echo ""

if [[ "$source_count" =~ ^[0-9]+$ ]] && [[ "$dest_count" =~ ^[0-9]+$ ]] && [ "$source_count" -eq "$dest_count" ]; then
    echo -e "${GREEN}======================================================================${NC}"
    echo -e "${GREEN}   ✅ DOĞRULAMA BAŞARILI: TÜM VERİLER EKSİKSİZ VE TUTARLI TAŞINDI!    ${NC}"
    echo -e "${GREEN}======================================================================${NC}"
    echo -e "Artık Sunucu 1'deki 'cyber-memory' servisinizin QDRANT_HOST adresini"
    echo -e "Sunucu 2'nin IP'si (${SERVER2_IP}) olarak güncelleyip servisi restart edebilirsiniz."
else
    echo -e "${RED}======================================================================${NC}"
    echo -e "${RED}   ⚠️ UYARI: VERİ SAYILARI EŞLEŞMİYOR VEYA BİR HATA OLUŞTU!           ${NC}"
    echo -e "${RED}======================================================================${NC}"
    echo -e "Lütfen hedef sunucudaki Qdrant loglarını kontrol edin (docker logs cyber-qdrant)."
    echo -e "Sunucu 1'deki verileri KESİNLİKLE silmeyin, eski servisi yönlendirmeyin."
fi

echo ""
echo -e "Yerel yedek dosyası: ${YELLOW}${snapshot_file}${NC} (Güvenlik için silinmemiştir)."
echo -e "${BLUE}======================================================================${NC}"