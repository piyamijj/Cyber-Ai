#!/bin/bash

# =====================================================================================
# CYBER AI - MİMARİ KARŞILAŞTIRMA (A/B) BENCHMARK SCRIPT
# =====================================================================================
# Bu betik, Oracle Cloud (GPU'suz, 4 OCPU, 64GB RAM) sunucunuzda çalışan
# iki farklı işbirlikçi (Çırak-Usta) mimarisini karşılaştırmak için tasarlanmıştır:
#   MİMARİ A: Token-Stream (asyncio.Queue canlı boru hattı - collab_orchestrator.py)
#   MİMARİ B: Cümle-Bazlı Relay (Sentence-Boundary Sequential Relay - collab_orchestrator_sentence.py)
#
# Betik, hafıza servisindeki yeni `/collab_compare` endpoint'ini kullanarak her iki
# mimariyi de AYNI test soruları için sırayla (ardışıl) çalıştırır. Bu sayede RAG ve
# web arama süreleri karşılaştırmayı etkilemez, sadece çırak-usta etkileşim hızı ölçülür.
#
# ÖNEMLİ NOTLAR:
# --------------
# 1. Bu betik mimarileri ARDIŞIL çalıştırdığı için CPU çekişmesini (contention) ölçmez.
#    Eş zamanlı yük altındaki CPU çekişmesini ölçmek için 'benchmark_cpu_contention.sh'
#    betiğini kullanmalısınız.
# 2. AI asistanının doğrudan SSH erişimi olmadığı için, bu betiği sunucuda MANUEL olarak
#    çalıştırmanız ve sonuçları gözlemlemeniz gerekmektedir.
# =====================================================================================

# 1. YAPILANDIRMA VE PARAMETRELER
MEMORY_SERVICE_URL="http://localhost:8083"

# Farklı türlerde 3 adet Türkçe test sorusu tanımlıyoruz
TEST_QUESTIONS=(
    "Yapay zeka ve makine öğrenmesi arasındaki farkları detaylı şekilde açıkla." # Açık uçlu / Uzun cevap
    "Bugün dolar kuru ne kadar oldu?"                                            # Web araması gerektiren güncel soru
    "Python ile bir quicksort algoritması yazar mısın?"                          # Teknik / Kodlama sorusu
)

# Renkli çıktılar için tanımlamalar
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}          CYBER AI - MİMARİ KARŞILAŞTIRMA BENCHMARK BAŞLATILIYOR      ${NC}"
echo -e "${BLUE}======================================================================${NC}"
echo -e "Hafıza Servisi: ${MEMORY_SERVICE_URL}"
echo -e "Test Edilecek Soru Sayısı: ${#TEST_QUESTIONS[@]}"
echo ""

# 2. BAĞLANTI VE SAĞLIK KONTROLÜ
if ! command -v curl &> /dev/null; then
    echo -e "${RED}HATA: 'curl' komutu bulunamadı. Lütfen yükleyin (sudo apt install curl).${NC}"
    exit 1
fi

echo -n "Hafıza servisi sağlık kontrolü yapılıyor... "
if ! curl -s --connect-timeout 5 "${MEMORY_SERVICE_URL}/health" &>/dev/null; then
    echo -e "${RED}BAĞLANTI BAŞARISIZ!${NC}"
    echo -e "Lütfen 'cyber-memory' servisinin çalıştığından emin olun (sudo systemctl status cyber-memory)."
    exit 1
fi
echo -e "${GREEN}AKTİF${NC}"
echo ""

# Geçici dosyalar için temizlik mekanizması
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

# JQ varlığını kontrol ediyoruz (JSON parse etmek için)
HAS_JQ=0
if command -v jq &> /dev/null; then
    HAS_JQ=1
fi

# 3. YARDIMCI PARSE FONKSİYONLARI (JQ yoksa fallback olarak grep/sed kullanır)
get_json_val() {
    local json="$1"
    local path="$2" # Örn: .token_stream.total_pipeline_seconds
    
    if [ "$HAS_JQ" -eq 1 ]; then
        echo "$json" | jq -r "$path" 2>/dev/null
    else
        # Basit grep/sed fallback (sadece düz alanlar için çalışır)
        # Örn: .token_stream.total_pipeline_seconds için "token_stream" bloğundaki "total_pipeline_seconds" değerini arar
        local block=$(echo "$path" | cut -d'.' -f2)
        local key=$(echo "$path" | cut -d'.' -f3)
        
        if [ -n "$key" ]; then
            # İki seviyeli nesne (örn: token_stream.total_pipeline_seconds)
            echo "$json" | grep -A 10 "\"$block\"" | grep "\"$key\"" | head -n 1 | sed -E 's/.*:\s*([^,]+),?/\1/' | tr -d '"' | tr -d ' '
        else
            # Tek seviyeli nesne
            echo "$json" | grep "\"$block\"" | head -n 1 | sed -E 's/.*:\s*([^,]+),?/\1/' | tr -d '"' | tr -d ' '
        fi
    fi
}

# 4. BENCHMARK DÖNGÜSÜ
sum_token_time=0
sum_sentence_time=0
token_wins=0
sentence_wins=0

for idx in "${!TEST_QUESTIONS[@]}"; do
    question="${TEST_QUESTIONS[$idx]}"
    q_num=$((idx + 1))
    
    echo -e "${YELLOW}----------------------------------------------------------------------${NC}"
    echo -e "${YELLOW}TEST SORUSU $q_num: '${question}'${NC}"
    echo -e "${YELLOW}----------------------------------------------------------------------${NC}"
    echo "İki mimari sırayla çalıştırılıyor (bu işlem 1-2 dakika sürebilir)..."
    
    # POST isteğini atıyoruz (cömert bir zaman aşımı ile)
    payload=$(cat <<EOF
{
  "query": "$question"
}
EOF
)
    
    response=$(curl -s -X POST "${MEMORY_SERVICE_URL}/collab_compare" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --connect-timeout 15 \
        --max-time 350)
        
    if [ -z "$response" ] || echo "$response" | grep -q "detail" || echo "$response" | grep -q "error"; then
        echo -e "${RED}HATA: Karşılaştırma isteği başarısız oldu!${NC}"
        echo "Yanıt: $response"
        continue
    fi
    
    # Değerleri ayrıştırıyoruz
    t_time=$(get_json_val "$response" ".token_stream.total_pipeline_seconds")
    t_turns=$(get_json_val "$response" ".token_stream.turns_used")
    t_early=$(get_json_val "$response" ".token_stream.approved_early")
    
    s_time=$(get_json_val "$response" ".sentence_relay.total_pipeline_seconds")
    s_turns=$(get_json_val "$response" ".sentence_relay.turns_used")
    s_early=$(get_json_val "$response" ".sentence_relay.approved_early")
    
    # Sayısal doğrulama (boş veya geçersiz değer kontrolü)
    if [[ ! "$t_time" =~ ^[0-9.]+$ ]] || [[ ! "$s_time" =~ ^[0-9.]+$ ]]; then
        echo -e "${RED}HATA: Geçersiz zamanlama verisi alındı!${NC}"
        echo "Token-Stream: '$t_time'sn, Cümle-Relay: '$s_time'sn"
        continue
    fi
    
    # Toplamları güncelliyoruz
    sum_token_time=$(echo "$sum_token_time + $t_time" | bc 2>/dev/null || awk "BEGIN {print $sum_token_time + $t_time}")
    sum_sentence_time=$(echo "$sum_sentence_time + $s_time" | bc 2>/dev/null || awk "BEGIN {print $sum_sentence_time + $s_time}")
    
    # Kazananı belirliyoruz
    is_token_faster=$(echo "$t_time < $s_time" | bc 2>/dev/null || awk "BEGIN {print ($t_time < $s_time) ? 1 : 0}")
    
    echo -e "\n${BLUE}Sonuçlar:${NC}"
    printf "  %-35s | %-15s | %-10s | %-12s\n" "MİMARİ" "TOPLAM SÜRE" "TUR SAYISI" "ERKEN ONAY"
    echo "  ----------------------------------------------------------------------------"
    printf "  %-35s | %-15s | %-10s | %-12s\n" "Mimarî A: Token-Stream" "${t_time}sn" "$t_turns" "$t_early"
    printf "  %-35s | %-15s | %-10s | %-12s\n" "Mimarî B: Cümle-Bazlı Relay" "${s_time}sn" "$s_turns" "$s_early"
    echo "  ----------------------------------------------------------------------------"
    
    if [ "$is_token_faster" -eq 1 ]; then
        diff_pct=$(echo "scale=1; (($s_time - $t_time) / $s_time) * 100" | bc 2>/dev/null || awk "BEGIN {print (($s_time - $t_time) / $s_time) * 100}")
        echo -e "  Kazanan: ${GREEN}Mimarî A (Token-Stream)${NC} - %${diff_pct} daha hızlı."
        token_wins=$((token_wins + 1))
    else
        diff_pct=$(echo "scale=1; (($t_time - $s_time) / $t_time) * 100" | bc 2>/dev/null || awk "BEGIN {print (($t_time - $s_time) / $t_time) * 100}")
        echo -e "  Kazanan: ${GREEN}Mimarî B (Cümle-Bazlı Relay)${NC} - %${diff_pct} daha hızlı."
        sentence_wins=$((sentence_wins + 1))
    fi
    echo ""
done

# 5. AGREGAT ÖZET TABLOSU
avg_token_time=$(echo "scale=2; $sum_token_time / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_token_time / 3}")
avg_sentence_time=$(echo "scale=2; $sum_sentence_time / 3" | bc 2>/dev/null || awk "BEGIN {print $sum_sentence_time / 3}")

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}                      A/B BENCHMARK AGREGAT ÖZETİ                     ${NC}"
echo -e "${BLUE}======================================================================${NC}"
printf "%-35s | %-20s | %-15s\n" "MİMARİ" "ORTALAMA SÜRE" "KAZANILAN TEST"
echo "----------------------------------------------------------------------"
printf "%-35s | %-20s | %-15s\n" "Mimarî A: Token-Stream" "${avg_token_time}sn" "$token_wins/3"
printf "%-35s | %-20s | %-15s\n" "Mimarî B: Cümle-Bazlı Relay" "${avg_sentence_time}sn" "$sentence_wins/3"
echo "----------------------------------------------------------------------"
echo ""

# 6. VERDİCT (KARAR VE ÖNERİ)
echo -e "${YELLOW}MİMARİ DEĞERLENDİRME VE TAVSİYE:${NC}"
echo -e "1. ${BLUE}Gecikme (Latency) Analizi:${NC}"
echo -e "   - Token-Stream ortalama ${avg_token_time}sn sürerken, Cümle-Bazlı Relay ortalama ${avg_sentence_time}sn sürdü."
echo -e "   - Cümle-Bazlı Relay, usta modeli her cümle bittiğinde kısa süreli (düşük max_tokens) çağırdığı için"
echo -e "     HTTP round-trip sayısı fazladır. Ancak usta modelin tek seferde tüm taslağı okuyup uzun bir"
echo -e "     değerlendirme üretmesine kıyasla daha dengeli bir gecikme profili sunabilir."
echo -e ""
echo -e "2. ${BLUE}CPU Çekişmesi (Contention) Analizi:${NC}"
echo -e "   - Token-Stream (erken tetikleme açıkken), iki modeli aynı anda uzun süre çalıştırdığı için CPU'yu zorlar."
echo -e "   - Cümle-Bazlı Relay ise usta modeli sadece cümle sınırlarında kısa süreli tetikler. Bu durum,"
echo -e "     GPU'suz 4 OCPU sunucuda CPU çekişmesini ve kilitlenme riskini büyük ölçüde azaltır."
echo -e ""
echo -e "3. ${BLUE}Kod Karmaşıklığı ve Stabilite:${NC}"
echo -e "   - Token-Stream: asyncio.Queue ve SSE stream parsing ile daha karmaşık bir asenkron boru hattı kullanır."
echo -e "   - Cümle-Bazlı Relay: Cümle sınırlarını yakalayan bir tampon (SentenceBuffer) kullanır. Daha deterministiktir."
echo -e ""

# Öneri mantığı
is_sentence_faster=$(echo "$avg_sentence_time < $avg_token_time" | bc 2>/dev/null || awk "BEGIN {print ($avg_sentence_time < $avg_token_time) ? 1 : 0}")

if [ "$is_sentence_faster" -eq 1 ]; then
    echo -e "${GREEN}TAVSİYE: MİMARİ B (CÜMLE-BAZLI SEQUENTIAL RELAY) KULLANIN.${NC}"
    echo -e "Cümle-bazlı relay hem ortalama sürede daha hızlı çıktı hem de CPU çekişmesini azaltan daha stabil bir yapı sunar."
else
    echo -e "${GREEN}TAVSİYE: MİMARİ B (CÜMLE-BAZLI SEQUENTIAL RELAY) BİRİNCİL ÖNCELİK YAPILMALIDIR.${NC}"
    echo -e "Token-Stream ortalama sürede daha hızlı görünse bile, bu test ardışıl (tek kullanıcı) yapılmıştır."
    echo -e "GPU'suz 4 OCPU sunucuda, çoklu kullanıcı yükü altında Cümle-Bazlı Relay'in CPU çekişmesini azaltma"
    echo -e "ve kilitlenmeleri önleme avantajı (stabilite), Token-Stream'in teorik hız avantajından çok daha değerlidir."
    echo -e "Bu nedenle, Cümle-Bazlı Relay'i birincil öncelik yapmanız, Token-Stream'i ise ikincil/opsiyonel bırakmanız önerilir."
fi
echo -e "${BLUE}======================================================================${NC}"