#!/bin/bash

# =====================================================================================
# CYBER AI - CPU CONTENTION (ÇEKİŞME) BENCHMARK SCRIPT
# =====================================================================================
# Bu betik, Oracle Cloud (GPU'suz, 4 OCPU, 64GB RAM) sunucunuzda çalışan
# "Usta" (14B, port 8082) ve "Çırak" (0.5B, port 8088) modellerinin GERÇEK anlamda
# paralel (eş zamanlı) çalıştırılmasının CPU çekişmesine (contention) yol açıp açmadığını
# deneysel olarak ölçmek için tasarlanmıştır.
#
# AI asistanının doğrudan SSH erişimi olmadığı için, bu betiği sunucuda MANUEL olarak
# çalıştırmanız ve sonuçları gözlemlemeniz gerekmektedir.
#
# Betik 3 farklı senaryoyu test eder:
#   Senaryo A: Sadece Usta model tek başına çalışır (Referans Gecikme).
#   Senaryo B: Sıralı Akış (Çırak taslağı bitirir, ardından Usta değerlendirir).
#   Senaryo C: Eş Zamanlı Akış (Çırak ve Usta aynı anda tetiklenir - Tam Paralel).
# =====================================================================================

# 1. YAPILANDIRMA VE PARAMETRELER
DRAFT_URL="http://localhost:8088"
USTA_URL="http://localhost:8082"
DRAFT_MODEL="models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
USTA_MODEL="models/qwen2.5-14b.gguf"

# Ölçümlerin tutarlı olması için sabit bir test sorusu ve token sınırı kullanıyoruz
TEST_PROMPT="Yapay zeka ve makine öğrenmesi arasındaki farkları detaylı şekilde açıkla."
MAX_TOKENS=200

# Renkli çıktılar için tanımlamalar
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}          CYBER AI - CPU CONTENTION BENCHMARK BAŞLATILIYOR            ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "Sunucu Özellikleri: 4 OCPU (ARM/x86), GPU YOK."
echo -e "Test Parametreleri: max_tokens=${MAX_TOKENS}, prompt_length=${#TEST_PROMPT} karakter."
echo ""

# 2. BAĞLANTI VE GEREKSİNİM KONTROLLERİ
if ! command -v curl &> /dev/null; then
    echo -e "${RED}HATA: 'curl' komutu bulunamadı. Lütfen yükleyin (sudo apt install curl).${NC}"
    exit 1
fi

# Sunucuların ayakta olup olmadığını kontrol ediyoruz
echo -n "Çırak model sunucusu kontrol ediliyor (port 8088)... "
if ! curl -s --connect-timeout 3 "${DRAFT_URL}/health" &>/dev/null && ! curl -s --connect-timeout 3 "${DRAFT_URL}/v1/models" &>/dev/null; then
    echo -e "${RED}BAĞLANTI BAŞARISIZ!${NC}"
    echo -e "Lütfen 'cyber-llama-draft' servisinin çalıştığından emin olun (sudo systemctl status cyber-llama-draft)."
    exit 1
fi
echo -e "${GREEN}AKTİF${NC}"

echo -n "Usta model sunucusu kontrol ediliyor (port 8082)... "
if ! curl -s --connect-timeout 3 "${USTA_URL}/health" &>/dev/null && ! curl -s --connect-timeout 3 "${USTA_URL}/v1/models" &>/dev/null; then
    echo -e "${RED}BAĞLANTI BAŞARISIZ!${NC}"
    echo -e "Lütfen usta model llama-server sürecinin çalıştığından emin olun (port 8082)."
    exit 1
fi
echo -e "${GREEN}AKTİF${NC}"

# Geçici dosyalar için temizlik mekanizması
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

# 3. YARDIMCI FONKSİYONLAR
time_request() {
    local url="$1"
    local model="$2"
    local label="$3"
    local out_file="$4"

    local start_time=$(date +%s.%N)
    
    # İstek gövdesini hazırlıyoruz
    local payload=$(cat <<EOF
{
  "model": "$model",
  "messages": [
    {"role": "user", "content": "$TEST_PROMPT"}
  ],
  "temperature": 0.2,
  "max_tokens": $MAX_TOKENS,
  "stream": false
}
EOF
)

    # curl ile isteği atıp toplam süreyi ölçüyoruz
    local http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${url}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --connect-timeout 10 \
        --max-time 180)

    local end_time=$(date +%s.%N)
    local duration=$(echo "$end_time - $start_time" | bc 2>/dev/null || awk "BEGIN {print $end_time - $start_time}")

    if [ "$http_code" -ne 200 ]; then
        echo -e "${RED}[$label] HATA! HTTP Kodu: $http_code${NC}" >&2
        echo "ERROR" > "$out_file"
    else
        echo "$duration" > "$out_file"
    fi
}

# 4. SENARYO A: REFERANS GECİKME (Sadece Usta Model Tek Başına)
echo -e "${YELLOW}--- SENARYO A: Referans Gecikme Ölçülüyor (Sadece Usta Model Tek Başına) ---${NC}"
echo "Usta model (14B) tek başına 3 kez çalıştırılacak..."
sum_usta_alone=0
for i in {1..3}; do
    echo -n "  Deneme $i/3... "
    time_request "$USTA_URL" "$USTA_MODEL" "Usta-Alone" "${TEMP_DIR}/usta_alone_$i"
    val=$(cat "${TEMP_DIR}/usta_alone_$i")
    if [ "$val" = "ERROR" ]; then
        echo -e "${RED}Başarısız!${NC}"
        exit 1
    fi
    echo -e "${GREEN}${val} saniye${NC}"
    sum_usta_alone=$(echo "$sum_usta_alone + $val" | bc 2>/dev/null || awk "BEGIN {print $sum_usta_alone + $val}")
done
avg_usta_alone=$(echo "scale=3; $sum_usta_alone / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_usta_alone / 3}")
echo -e "=> ${BLUE}Usta Model Tek Başına Ortalama Süre: ${avg_usta_alone}sn${NC}"
echo ""

# 5. SENARYO B: SIRALI AKIŞ (Önce Çırak, Sonra Usta)
echo -e "${YELLOW}--- SENARYO B: Sıralı Akış Ölçülüyor (Önce Çırak, Sonra Usta) ---${NC}"
echo "Çırak (0.5B) çalışıp bitecek, ardından Usta (14B) başlayacak (3 deneme)..."
sum_seq_total=0
for i in {1..3}; do
    echo -n "  Deneme $i/3... "
    
    # Çırak çalıştırılıyor
    time_request "$DRAFT_URL" "$DRAFT_MODEL" "Seq-Draft" "${TEMP_DIR}/seq_draft_$i"
    val_draft=$(cat "${TEMP_DIR}/seq_draft_$i")
    
    # Usta çalıştırılıyor
    time_request "$USTA_URL" "$USTA_MODEL" "Seq-Usta" "${TEMP_DIR}/seq_usta_$i"
    val_usta=$(cat "${TEMP_DIR}/seq_usta_$i")
    
    if [ "$val_draft" = "ERROR" ] || [ "$val_usta" = "ERROR" ]; then
        echo -e "${RED}Başarısız!${NC}"
        exit 1
    fi
    
    total_seq_round=$(echo "$val_draft + $val_usta" | bc 2>/dev/null || awk "BEGIN {print $val_draft + $val_usta}")
    echo -e "${GREEN}${total_seq_round} saniye${NC} (Çırak: ${val_draft}sn, Usta: ${val_usta}sn)"
    sum_seq_total=$(echo "$sum_seq_total + $total_seq_round" | bc 2>/dev/null || awk "BEGIN {print $sum_seq_total + $total_seq_round}")
done
avg_seq_total=$(echo "scale=3; $sum_seq_total / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_seq_total / 3}")
echo -e "=> ${BLUE}Sıralı Akış Ortalama Toplam Süre: ${avg_seq_total}sn${NC}"
echo ""

# 6. SENARYO C: EŞ ZAMANLI AKIŞ (Çırak ve Usta Aynı Anda)
echo -e "${YELLOW}--- SENARYO C: Eş Zamanlı Akış Ölçülüyor (Tam Paralel) ---${NC}"
echo "Çırak (0.5B) ve Usta (14B) aynı anda tetikleniyor (3 deneme)..."
sum_concurrent_total=0
sum_concurrent_usta_only=0

for i in {1..3}; do
    echo -n "  Deneme $i/3... "
    
    start_round=$(date +%s.%N)
    
    # İki isteği de arka planda paralel başlatıyoruz
    time_request "$DRAFT_URL" "$DRAFT_MODEL" "Con-Draft" "${TEMP_DIR}/con_draft_$i" &
    pid_draft=$!
    
    time_request "$USTA_URL" "$USTA_MODEL" "Con-Usta" "${TEMP_DIR}/con_usta_$i" &
    pid_usta=$!
    
    # İkisinin de bitmesini bekliyoruz
    wait $pid_draft $pid_usta
    
    end_round=$(date +%s.%N)
    
    val_draft=$(cat "${TEMP_DIR}/con_draft_$i")
    val_usta=$(cat "${TEMP_DIR}/con_usta_$i")
    
    if [ "$val_draft" = "ERROR" ] || [ "$val_usta" = "ERROR" ]; then
        echo -e "${RED}Başarısız!${NC}"
        exit 1
    fi
    
    total_con_round=$(echo "$end_round - $start_round" | bc 2>/dev/null || awk "BEGIN {print $end_round - $start_round}")
    echo -e "${GREEN}${total_con_round} saniye${NC} (Çırak: ${val_draft}sn, Usta: ${val_usta}sn)"
    
    sum_concurrent_total=$(echo "$sum_concurrent_total + $total_con_round" | bc 2>/dev/null || awk "BEGIN {print $sum_concurrent_total + $total_con_round}")
    sum_concurrent_usta_only=$(echo "$sum_concurrent_usta_only + $val_usta" | bc 2>/dev/null || awk "BEGIN {print $sum_concurrent_usta_only + $val_usta}")
done

avg_concurrent_total=$(echo "scale=3; $sum_concurrent_total / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_concurrent_total / 3}")
avg_concurrent_usta_only=$(echo "scale=3; $sum_concurrent_usta_only / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_concurrent_usta_only / 3}")

echo -e "=> ${BLUE}Eş Zamanlı Akış Ortalama Toplam Süre: ${avg_concurrent_total}sn${NC}"
echo -e "=> ${BLUE}Eş Zamanlı Akışta Usta Modelin Kendi Süresi: ${avg_concurrent_usta_only}sn${NC}"
echo ""

# 7. ANALİZ VE KARŞILAŞTIRMA TABLOSU
# Usta modelin paralel çalışırken ne kadar yavaşladığını hesaplıyoruz (CPU Contention Oranı)
slowdown_pct=$(echo "scale=1; (($avg_concurrent_usta_only - $avg_usta_alone) / $avg_usta_alone) * 100" | bc 2>/dev/null || awk "BEGIN {print (($avg_concurrent_usta_only - $avg_usta_alone) / $avg_usta_alone) * 100}")

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}                      BENCHMARK SONUÇ ÖZETİ                           ${NC}"
echo -e "${BLUE}======================================================================${NC}"
printf "%-45s | %-15s\n" "SENARYO" "ORTALAMA SÜRE"
echo "----------------------------------------------------------------------"
printf "%-45s | %-15s\n" "Senaryo A: Sadece Usta (Referans)" "${avg_usta_alone}sn"
printf "%-45s | %-15s\n" "Senaryo B: Sıralı Akış (Çırak -> Usta)" "${avg_seq_total}sn"
printf "%-45s | %-15s\n" "Senaryo C: Eş Zamanlı Akış (Tam Paralel)" "${avg_concurrent_total}sn"
echo "----------------------------------------------------------------------"
echo -e "Usta Modelin CPU Çekişmesi Nedeniyle Yavaşlama Oranı: ${RED}${slowdown_pct}%${NC}"
echo ""

# 8. VERDİCT (KARAR VE ÖNERİ)
echo -e "${YELLOW}ÖNERİ VE DEĞERLENDİRME:${NC}"
is_slow=$(echo "$slowdown_pct > 15.0" | bc 2>/dev/null || awk "BEGIN {print ($slowdown_pct > 15.0) ? 1 : 0}")

if [ "$is_slow" -eq 1 ]; then
    echo -e "${RED}⚠️ DİKKAT: CPU ÇEKİŞMESİ TESPİT EDİLDİ!${NC}"
    echo -e "Usta model, Çırak modelle aynı anda çalışırken %${slowdown_pct} oranında yavaşlıyor."
    echo -e "Bu durum, GPU'suz 4 OCPU sunucuda CPU çekirdeklerinin iki model arasında paylaşılamamasından kaynaklanır."
    echo -e ""
    echo -e "${YELLOW}Önerilen Yapılandırma:${NC}"
    echo -e "  1. Kod seviyesinde ${GREEN}CRITIQUE_EARLY_START_CHARS=0${NC} (Sıralı Akış) modunu koruyun."
    echo -e "     Bu modda toplam süre biraz daha uzun görünse de, ana modelin (Usta) token üretim hızı"
    echo -e "     ve pürüzsüzlüğü korunur, CPU kilitlenmeleri önlenir."
    echo -e "  2. Eğer paralel çalışmayı zorunlu kılmak istiyorsanız, Çırak modelin systemd servisine"
    echo -e "     ${GREEN}Nice=15${NC} ekleyerek önceliğini düşürün veya ${GREEN}taskset -c 0,1${NC} ile Çırak'ı ilk iki çekirdeğe,"
    echo -e "     Usta'yı ise ${GREEN}taskset -c 2,3${NC} ile son iki çekirdeğe sabitleyin."
else
    echo -e "${GREEN}✅ CPU ÇEKİŞMESİ TOLERE EDİLEBİLİR SEVİYEDE!${NC}"
    echo -e "Usta modelin yavaşlama oranı (%${slowdown_pct}) %15'in altında."
    echo -e "Bu sunucuda Çırak ve Usta modelleri paralel olarak çalıştırılabilir."
    echo -e "Dilerseniz ${GREEN}CRITIQUE_EARLY_START_CHARS=150${NC} gibi bir değerle erken tetiklemeyi aktif edebilirsiniz."
fi
echo -e "${BLUE}======================================================================${NC}"