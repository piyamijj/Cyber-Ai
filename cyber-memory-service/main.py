import os
import re
import html
import logging
import datetime
import uuid
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from sentence_transformers import SentenceTransformer

# 1. LOGGING AYARLARI
# Servisin çalışmasını ve arka plandaki kararları takip edebilmek için loglama yapısını kuruyoruz.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("cyber-memory-service")

# 2. YAPILANDIRMA VE ÇEVRE DEĞİŞKENLERİ
# Oracle sunucusundaki diğer servislerin adreslerini ve Qdrant ayarlarını tanımlıyoruz.
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "cyber_memory")
LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://localhost:8082")
EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"

# Arama için varsayılan parametreler
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "5"))
# Cosine similarity için eşik değer. Alakasız geçmiş bilgileri elemek için kullanılır.
# NOT: BAAI/bge-base-en-v1.5 İngilizce odaklı bir model olduğu için Türkçe metinlerde
# gerçek eşleşmeler bile daha düşük skorlar üretebilir (ör. 0.3-0.5 aralığı). Bu yüzden
# eşiği düşük tutuyoruz; çok fazla alakasız sonuç gelirse ileride yükseltilebilir.
SEARCH_SCORE_THRESHOLD = float(os.getenv("SEARCH_SCORE_THRESHOLD", "0.2"))

# Web araması için varsayılan sonuç sayısı ve zaman aşımı
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "4"))
# DuckDuckGo'nun JavaScript gerektirmeyen, API anahtarı istemeyen HTML arama sayfası.
# Resmi bir API değildir (ücretsiz, kayıt gerektirmeyen bir alternatif olduğu için tercih edildi);
# DuckDuckGo'nun HTML yapısı değişirse bu ayrıştırma mantığının güncellenmesi gerekebilir.
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"

# 3. GLOBAL MODEL VE İSTEMCİ BAŞLATMALARI
# Servis her istekte modeli baştan yüklemesin diye global olarak bir kez başlatıyoruz.
logger.info(f"Embedding modeli yükleniyor: {EMBEDDING_MODEL_NAME}...")
try:
    embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    logger.info("Embedding modeli başarıyla yüklendi.")
except Exception as e:
    logger.error(f"Embedding modeli yüklenirken hata oluştu: {e}")
    embedding_model = None

logger.info(f"Qdrant istemcisi başlatılıyor ({QDRANT_HOST}:{QDRANT_PORT})...")
try:
    qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    logger.info("Qdrant istemcisi başarıyla başlatıldı.")
except Exception as e:
    logger.error(f"Qdrant bağlantısı kurulurken hata oluştu: {e}")
    qdrant_client = None

# 4. FASTAPI UYGULAMASI VE CORS AYARLARI
app = FastAPI(
    title="Cyber AI Memory & Search Service",
    description="Cyber AI için RAG arama ve akıllı otomatik hafıza kaydı mikroservisi.",
    version="1.0.0"
)

# Bu servis sadece sunucu tarafında (Vercel proxy backend) çağrılacağı için CORS kısıtlamalarını esnek tutuyoruz.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 5. PYDANTIC VERİ MODELLERİ
class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = None

class SearchResult(BaseModel):
    text: str
    score: float
    timestamp: Optional[str] = None

class SearchResponse(BaseModel):
    results: List[SearchResult]
    found: bool

class RememberRequest(BaseModel):
    user_message: str
    assistant_message: str

class RememberResponse(BaseModel):
    saved: bool
    reason: str
    saved_text: Optional[str] = None

class WebSearchRequest(BaseModel):
    query: str
    max_results: Optional[int] = None

class WebSearchResult(BaseModel):
    title: str
    snippet: str
    url: str

class WebSearchResponse(BaseModel):
    results: List[WebSearchResult]
    found: bool

class HealthResponse(BaseModel):
    status: str
    qdrant_connected: bool
    collection_exists: bool
    embedding_model_loaded: bool
    collection_points_count: Optional[int] = None

# 6. BAŞLANGIÇ KONTROLLERİ
@app.on_event("startup")
async def startup_event():
    """Servis başlarken Qdrant koleksiyonunun varlığını doğrular."""
    if qdrant_client:
        try:
            collections = qdrant_client.get_collections()
            exists = any(c.name == COLLECTION_NAME for c in collections.collections)
            if exists:
                logger.info(f"Doğrulandı: '{COLLECTION_NAME}' koleksiyonu Qdrant üzerinde mevcut.")
            else:
                logger.warning(
                    f"DİKKAT: '{COLLECTION_NAME}' koleksiyonu Qdrant üzerinde bulunamadı! "
                    "Lütfen koleksiyonun doğru oluşturulduğundan emin olun."
                )
        except Exception as e:
            logger.error(f"Başlangıçta Qdrant koleksiyon kontrolü başarısız oldu: {e}")

# 7. ENDPOINT'LER

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Servisin ve bağlı olduğu bileşenlerin (Qdrant, Embedding) sağlık durumunu döner."""
    qdrant_connected = False
    collection_exists = False
    points_count = None
    embedding_loaded = embedding_model is not None

    if qdrant_client:
        try:
            collections = qdrant_client.get_collections()
            qdrant_connected = True
            collection_exists = any(c.name == COLLECTION_NAME for c in collections.collections)
            
            if collection_exists:
                # Koleksiyondaki toplam kayıt sayısını alıyoruz
                collection_info = qdrant_client.get_collection(collection_name=COLLECTION_NAME)
                points_count = collection_info.points_count
        except Exception as e:
            logger.error(f"Sağlık kontrolü sırasında Qdrant hatası: {e}")

    status = "ok" if (qdrant_connected and collection_exists and embedding_loaded) else "degraded"

    return HealthResponse(
        status=status,
        qdrant_connected=qdrant_connected,
        collection_exists=collection_exists,
        embedding_model_loaded=embedding_loaded,
        collection_points_count=points_count
    )

@app.post("/search", response_model=SearchResponse)
async def search_memory(request: SearchRequest):
    """
    Kullanıcının sorusuna göre Qdrant üzerinde anlamsal (vektörel) arama yapar.
    BGE-v1.5 modelinin önerisi doğrultusunda arama sorgusuna özel ön ek eklenir.
    """
    if not embedding_model:
        logger.error("Arama başarısız: Embedding modeli yüklü değil.")
        return SearchResponse(results=[], found=False)
    
    if not qdrant_client:
        logger.error("Arama başarısız: Qdrant istemcisi başlatılmamış.")
        return SearchResponse(results=[], found=False)

    try:
        # BAAI/bge-base-en-v1.5 için en iyi arama performansı için sorgu ön eki ekliyoruz
        query_text = f"Represent this sentence for searching relevant passages: {request.query}"
        
        # Sorguyu vektöre çeviriyoruz
        query_vector = embedding_model.encode(query_text).tolist()
        
        limit = request.top_k or SEARCH_TOP_K

        # Qdrant üzerinde vektörel arama yapıyoruz.
        # NOT: qdrant-client'ın yeni sürümlerinde eski `.search()` metodu kaldırıldı,
        # yerine `.query_points()` geldi. Bu metod bir `QueryResponse` nesnesi döner,
        # asıl sonuç listesi ise onun `.points` alanındadır (eski `.search()`'ün
        # döndürdüğü düz listeden farklı olarak).
        query_response = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=limit,
            score_threshold=SEARCH_SCORE_THRESHOLD
        )
        search_results = query_response.points

        results = []
        for hit in search_results:
            payload = hit.payload or {}
            results.append(SearchResult(
                text=payload.get("text", ""),
                score=hit.score,
                timestamp=payload.get("timestamp")
            ))

        found = len(results) > 0

        # KALİBRASYON LOGU: eşik uygulanmadan en iyi skorun ne olduğunu da görelim.
        # Bu sayede SEARCH_SCORE_THRESHOLD değerini ileride doğru ayarlayabiliriz.
        if not found:
            try:
                raw_query_response = qdrant_client.query_points(
                    collection_name=COLLECTION_NAME,
                    query=query_vector,
                    limit=1,
                    score_threshold=None
                )
                raw_results = raw_query_response.points
                if raw_results:
                    logger.info(
                        f"KALİBRASYON: Eşik altında kalan en yüksek skor: {raw_results[0].score:.4f} "
                        f"(mevcut eşik: {SEARCH_SCORE_THRESHOLD}) | Metin: '{(raw_results[0].payload or {}).get('text', '')[:60]}...'"
                    )
            except Exception as calib_err:
                logger.warning(f"Kalibrasyon sorgusu başarısız oldu: {calib_err}")

        logger.info(
            f"Hafıza araması tamamlandı. Sorgu: '{request.query[:30]}...' "
            f"| Bulunan kayıt sayısı: {len(results)} | En yüksek skor: {results[0].score if found else 'Yok'}"
        )
        
        return SearchResponse(results=results, found=found)

    except Exception as e:
        logger.error(f"Hafıza araması sırasında beklenmeyen hata: {e}", exc_info=True)
        # Arama hatası ana sohbet akışını çökertmesin diye boş sonuç dönüyoruz
        return SearchResponse(results=[], found=False)

# Bu kelimelerle başlayan/çok kısa mesajlar neredeyse hiçbir zaman hafızaya değer
# bir bilgi içermez (selamlaşma, teşekkür, kısa onay vb.). Bunları LLM'e hiç
# sormadan eleyerek modelin gereksiz yere meşgul olmasını (ve bu yüzden gerçek
# sohbet mesajlarının sırada beklemesini) önlüyoruz.
TRIVIAL_MESSAGE_PATTERNS = [
    "merhaba", "selam", "naber", "nasılsın", "iyi misin", "günaydın", "iyi akşamlar",
    "iyi geceler", "teşekkür", "sağol", "tamam", "ok", "evet", "hayır", "peki",
    "görüşürüz", "hoşça kal", "kimsin", "ne yapıyorsun"
]
MIN_MEANINGFUL_LENGTH = 25  # karakter cinsinden - bundan kısa mesajlar genelde önemsiz sohbettir


def is_likely_trivial(user_message: str) -> bool:
    """
    Ucuz, hızlı bir ön filtre: mesaj kısa VEYA tipik bir selamlaşma/kısa yanıt kalıbıyla
    başlıyorsa, muhtemelen hafızaya değer bir bilgi taşımıyordur. Bu durumda pahalı LLM
    çağrısını hiç yapmayarak hem hız kazanıyoruz hem de modelin gerçek sohbet istekleriyle
    çakışmasını (kaynak çekişmesi/kuyruklanma) azaltıyoruz.
    """
    text = user_message.strip().lower()
    if len(text) < MIN_MEANINGFUL_LENGTH:
        return True
    for pattern in TRIVIAL_MESSAGE_PATTERNS:
        if text.startswith(pattern):
            return True
    return False


@app.post("/web_search", response_model=WebSearchResponse)
async def web_search(request: WebSearchRequest):
    """
    Güncel/zamana-duyarlı sorular için gerçek zamanlı web araması yapar.
    DuckDuckGo'nun ücretsiz, API anahtarı gerektirmeyen HTML arama sayfasını kullanır.

    ÖNEMLİ: Bu endpoint'in döndürdüğü sonuçlar KALICI OLARAK Qdrant'a KAYDEDİLMEZ.
    Güncel bilgi zamanla bayatlar; kalıcı hafızaya yazılırsa ileride yanıltıcı olur.
    Sonuçlar sadece o anki soruya cevap vermek için kullanılıp hemen atılır.
    """
    max_results = request.max_results or WEB_SEARCH_MAX_RESULTS

    try:
        # DuckDuckGo'nun HTML arama sayfasına normal bir tarayıcı gibi görünen
        # bir User-Agent ile istek atıyoruz (bazı botlara engelleme yapılabiliyor).
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }

        async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
            response = await client.post(
                DUCKDUCKGO_HTML_URL,
                data={"q": request.query}
            )

        if response.status_code != 200:
            logger.warning(f"Web arama isteği başarısız oldu. Durum kodu: {response.status_code}")
            return WebSearchResponse(results=[], found=False)

        raw_html = response.text

        # DuckDuckGo'nun HTML sayfasındaki sonuç bloklarını basit bir regex ile ayrıştırıyoruz
        # (ağır bir HTML parser kütüphanesi eklemeden, standart kütüphane ile).
        # Her sonuç şu yapıda: <a class="result__a" href="...">BAŞLIK</a> ... <a class="result__snippet" ...>ÖZET</a>
        result_blocks = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'class="result__snippet"[^>]*>(.*?)</a>',
            raw_html,
            re.DOTALL
        )

        results = []
        for url_match, title_html, snippet_html in result_blocks[:max_results]:
            # HTML etiketlerini temizle ve HTML entity'lerini (ör. &amp;) çöz
            title_clean = html.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
            snippet_clean = html.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()

            if title_clean and snippet_clean:
                results.append(WebSearchResult(
                    title=title_clean,
                    snippet=snippet_clean,
                    url=url_match
                ))

        found = len(results) > 0
        logger.info(f"Web araması tamamlandı. Sorgu: '{request.query[:40]}...' | Bulunan sonuç: {len(results)}")

        return WebSearchResponse(results=results, found=found)

    except Exception as e:
        logger.error(f"Web araması sırasında hata: {e}", exc_info=True)
        # Web arama hatası ana sohbet akışını çökertmesin diye boş sonuç dönüyoruz
        return WebSearchResponse(results=[], found=False)


@app.post("/remember", response_model=RememberResponse)
async def remember_conversation(request: RememberRequest):
    """
    Konuşmayı analiz eder, LLM'e danışarak hafızaya değer bir bilgi olup olmadığına karar verir.
    Eğer önemli bir bilgi ise özetleyip Qdrant'a kaydeder.
    """
    if not embedding_model or not qdrant_client:
        return RememberResponse(saved=False, reason="Sistem bileşenleri (Qdrant/Embedding) hazır değil.")

    # ÖN FİLTRE: Mesaj açıkça önemsizse, LLM'i hiç meşgul etmeden erken çıkış yapıyoruz.
    # Bu, ana sohbet akışının modelle kaynak çekişmesine girme ihtimalini büyük ölçüde azaltır.
    if is_likely_trivial(request.user_message):
        logger.info(f"Ön filtre: Mesaj önemsiz görünüyor, LLM'e sorulmadan atlandı: '{request.user_message[:40]}...'")
        return RememberResponse(saved=False, reason="Mesaj kısa/sıradan bir sohbet olduğu için LLM'e sorulmadan atlandı.")

    try:
        # 1. LLM'e karar verdirmek için prompt hazırlıyoruz
        decision_prompt = (
            "Aşağıdaki kullanıcı ve asistan arasındaki son konuşmayı analiz et.\n"
            "Bu konuşma, kullanıcının gelecekte hatırlanmasını isteyeceği önemli bir kişisel bilgisini, "
            "tercihini, kalıcı bir gerçeği veya özel bir talimatını içeriyor mu?\n"
            "Sıradan sohbetleri, selamlaşmaları, geçici soruları veya genel bilgi aramalarını (kod yazma, genel tarih vb.) hafızaya KAYDETME.\n\n"
            "Konuşma:\n"
            f"Kullanıcı: {request.user_message}\n"
            f"Asistan: {request.assistant_message}\n\n"
            "YALNIZCA aşağıdaki formatta cevap ver, başka hiçbir şey yazma:\n"
            "1. Satır: Eğer kaydedilmeye değer kalıcı bir bilgi varsa 'EVET', yoksa 'HAYIR'\n"
            "2. Satır: (Sadece EVET ise) Bu bilginin gelecekte arandığında bulunabilmesi için 1-2 cümlelik net, "
            "üçüncü şahıs ağzından yazılmış Türkçe bir özet cümlesi (Örn: 'Kullanıcı Python dilini tercih ediyor ve web projeleri geliştiriyor.').\n"
        )

        # 2. llama.cpp sunucusuna kısa bir istek atıyoruz
        # NOT: 14B model CPU üzerinde çalıştığı için, kısa bir karar isteği bile
        # bazen 1-2 dakikaya kadar sürebilir (daha önceki testlerde gözlemlendi).
        # Bu yüzden zaman aşımını cömert tutuyoruz (connect kısa, read uzun).
        request_timeout = httpx.Timeout(connect=10.0, read=150.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{LLAMA_SERVER_URL}/v1/chat/completions",
                json={
                    "model": "models/qwen2.5-14b.gguf",
                    "messages": [
                        {"role": "system", "content": "Sen sadece verilen formata göre çıktı üreten kararlı bir analizörsün."},
                        {"role": "user", "content": decision_prompt}
                    ],
                    "temperature": 0.1, # Kararlılık için düşük sıcaklık
                    "max_tokens": 80, # Sadece EVET/HAYIR + kısa bir özet yeterli; CPU'da üretim süresini kısaltmak için düşük tutuyoruz
                    "stream": False
                }
            )

        if response.status_code != 200:
            logger.error(f"Hafıza kararı için LLM çağrısı başarısız oldu. Durum kodu: {response.status_code}")
            return RememberResponse(saved=False, reason=f"LLM sunucusu hata döndürdü: {response.status_code}")

        llm_output = response.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"LLM Hafıza Analiz Çıktısı:\n{llm_output}")

        # 3. LLM çıktısını ayrıştırıyoruz
        lines = [line.strip() for line in llm_output.split("\n") if line.strip()]
        if not lines:
            return RememberResponse(saved=False, reason="LLM boş yanıt döndürdü.")

        decision = lines[0].upper()
        
        if "EVET" in decision:
            if len(lines) < 2:
                return RememberResponse(saved=False, reason="LLM 'EVET' dedi ancak özet cümlesi üretmedi.")
            
            summary_text = " ".join(lines[1:])
            
            # 4. Özet cümlesini vektöre çevirip Qdrant'a kaydediyoruz
            # Kayıt (passage) vektörleri için ön ek EKLEMİYORUZ, doğrudan ham metni embed ediyoruz
            vector = embedding_model.encode(summary_text).tolist()
            
            point_id = str(uuid.uuid4())
            timestamp_str = datetime.datetime.utcnow().isoformat() + "Z"

            qdrant_client.upsert(
                collection_name=COLLECTION_NAME,
                points=[
                    qmodels.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload={
                            "text": summary_text,
                            "timestamp": timestamp_str,
                            "source": "auto_memory",
                            "raw_user_msg": request.user_message[:200],
                            "raw_assistant_msg": request.assistant_message[:200]
                        }
                    )
                ]
            )

            logger.info(f"YENİ HAFIZA KAYDEDİLDİ: '{summary_text}' | ID: {point_id}")
            return RememberResponse(saved=True, reason="Önemli bilgi tespit edildi ve hafızaya kaydedildi.", saved_text=summary_text)
        
        else:
            logger.info("Konuşma hafızaya değer bir bilgi içermediği için kaydedilmedi.")
            return RememberResponse(saved=False, reason="Konuşma hafızaya değer kalıcı bir bilgi içermiyor.")

    except Exception as e:
        logger.error(f"Hafıza kaydı işlemi sırasında hata: {e}", exc_info=True)
        return RememberResponse(saved=False, reason=f"Sistem hatası: {str(e)}")

# 8. DOĞRUDAN ÇALIŞTIRMA DESTEĞİ
if __name__ == "__main__":
    logger.info("Cyber AI Hafıza Servisi port 8083 üzerinde başlatılıyor...")
    uvicorn.run(app, host="0.0.0.0", port=8083)