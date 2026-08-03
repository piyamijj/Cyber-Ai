import os
import re
import json
import html
import logging
import datetime
import uuid
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn
import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from sentence_transformers import SentenceTransformer

# YENİ MİMARİ: Streaming Çırak-Usta (Draft-Critique) işbirlikçi boru hattı.
# Bu modül; Shared Context (tek seferlik RAG), asyncio.Queue tabanlı canlı streaming pipe,
# kod seviyesinde kesin max-tur sınırı ve approval_token ile early-exit mantığını içerir.
import collab_orchestrator as collab

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
# "Çırak" (draft/küçük) model — qwen2.5-0.5b-instruct, ayrı bir llama-server sürecinde,
# ayrı bir portta (varsayılan 8088) bağımsız olarak çalışır. Bu model /decide endpoint'i
# için kullanılır: kullanıcı mesajının güncel/harici bilgi gerektirip gerektirmediğine
# hızlıca (düşük max_tokens ile ~1-2sn içinde) karar verir. Ana (14B, "usta") modelden
# tamamen bağımsızdır — kaynak çekişmesine girmez, bu yüzden ana sohbet gecikmesini artırmaz.
DRAFT_LLAMA_SERVER_URL = os.getenv("DRAFT_LLAMA_SERVER_URL", "http://localhost:8088")
EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"

# YENİ MİMARİ: Çırak-Usta modellerin gerçek model dosya adları (llama-server'a /v1/chat/completions
# çağrılırken "model" alanında gönderilir). Ortam değişkeninden override edilebilir.
DRAFT_MODEL_NAME = os.getenv("DRAFT_MODEL_NAME", "models/qwen2.5-0.5b-instruct-q4_k_m.gguf")
USTA_MODEL_NAME = os.getenv("USTA_MODEL_NAME", "models/qwen2.5-14b.gguf")

# /decide endpoint'i için İKİNCİ SAVUNMA KATMANI: küçük model (0.5B) HAYIR derse bile, mesajda
# bu belirgin anahtar kelimelerden biri geçiyorsa karar EVET'e çevrilir. Küçük modeller bazen
# açıkça güncel bir soruyu (ör. "bugün dolar kuru kaç") yanlış sınıflandırabiliyor; bu liste
# ucuz ve hızlı bir ek güvenlik ağı olarak çalışır, LLM kararının tek başına yeterli olmadığı
# durumlarda devreye girer.
REALTIME_OVERRIDE_KEYWORDS = [
    "güncel", "bugün", "bu gün", "şu an", "şu anda", "son durum", "son dakika",
    "haber", "haberler", "ne oldu", "ne durumda", "kim oldu", "kim kazandı",
    "seçim", "kriz", "bu hafta", "bu ay", "bu yıl", "geçen hafta", "dün", "yarın",
    "yeni açıklama", "son gelişme", "kaç oldu", "fiyatı ne", "kuru ne", "kuru kaç",
    "dolar kuru", "euro kuru", "borsa", "bitcoin", "kripto", "hava durumu",
    "cumhurbaşkanı", "başbakan", "hangi parti", "yeni parti", "maç", "skor", "deprem"
]

# Arama için varsayılan parametreler
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "5"))
# Cosine similarity için eşik değer. Alakasız geçmiş bilgileri elemek için kullanılır.
# NOT: BAAI/bge-base-en-v1.5 İngilizce odaklı bir model olduğu için Türkçe metinlerde
# gerçek eşleşmeler bile daha düşük skorlar üretebilir (ör. 0.3-0.5 aralığı). Bu yüzden
# eşiği düşük tutuyoruz; çok fazla alakasız sonuç gelirse ileride yükseltilebilir.
SEARCH_SCORE_THRESHOLD = float(os.getenv("SEARCH_SCORE_THRESHOLD", "0.2"))

# Web araması için varsayılan sonuç sayısı ve zaman aşımı
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "4"))
# Tavily API — resmi, API key'li arama servisi. DuckDuckGo'nun ücretsiz HTML kazıma
# yöntemi sık sık CAPTCHA/bot-engellemesine takıldığı için (güvenilmez sonuç, bazen
# gecikme) buna geçildi. Tavily özellikle LLM/agent kullanım senaryoları için tasarlanmış,
# CAPTCHA riski taşımaz.
TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

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

class DecideRequest(BaseModel):
    query: str

class DecideResponse(BaseModel):
    needs_realtime_info: bool
    search_query: str = ""
    raw_output: str
    fallback_used: bool = False

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
    Tavily API kullanır (resmi, ücretli/API-key'li servis) — DuckDuckGo HTML kazıma
    yöntemi CAPTCHA/bot-engellemesine sık takıldığı için (sık sık boş/güvenilmez sonuç)
    Tavily'ye geçildi; bu API doğrudan LLM/agent kullanım senaryoları için tasarlanmıştır
    ve CAPTCHA riski taşımaz.

    ÖNEMLİ: Bu endpoint'in döndürdüğü sonuçlar KALICI OLARAK Qdrant'a KAYDEDİLMEZ.
    Güncel bilgi zamanla bayatlar; kalıcı hafızaya yazılırsa ileride yanıltıcı olur.
    Sonuçlar sadece o anki soruya cevap vermek için kullanılıp hemen atılır.
    """
    max_results = request.max_results or WEB_SEARCH_MAX_RESULTS

    if not TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY tanımlı değil, web araması atlanıyor.")
        return WebSearchResponse(results=[], found=False)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                TAVILY_API_URL,
                headers={
                    "Authorization": f"Bearer {TAVILY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": request.query,
                    "max_results": max_results,
                },
            )

        if response.status_code != 200:
            logger.warning(f"Tavily API beklenmeyen durum kodu döndürdü: {response.status_code} | {response.text[:200]}")
            return WebSearchResponse(results=[], found=False)

        data = response.json()
        raw_results = data.get("results", [])

        results = []
        for item in raw_results[:max_results]:
            title = (item.get("title") or "").strip()
            # Tavily "content" alanında snippet benzeri bir özet döner
            snippet = (item.get("content") or "").strip()
            url = item.get("url") or ""

            if not title or not snippet:
                continue

            # NOT: snippet 600 -> 350 karaktere düşürüldü. Amaç: büyük modele (14B, CPU'da
            # prompt işleme süresi token sayısıyla orantılı) giden toplam prompt boyutunu
            # küçültüp gecikmeyi azaltmak. 350 karakter çoğu güncel bilgi (kur, hava durumu,
            # haber özeti) için hâlâ yeterli bağlam sağlar.
            results.append(WebSearchResult(
                title=title[:300],
                snippet=snippet[:350],
                url=url
            ))

        found = len(results) > 0
        logger.info(f"Web araması (Tavily) tamamlandı. Sorgu: '{request.query[:40]}...' | Bulunan sonuç: {len(results)}")

        return WebSearchResponse(results=results, found=found)

    except Exception as e:
        logger.error(f"Web araması (Tavily) sırasında hata: {e}", exc_info=True)
        # Web arama hatası ana sohbet akışını çökertmesin diye boş sonuç dönüyoruz
        return WebSearchResponse(results=[], found=False)


# "Çırak-usta" karar adımının sistem/rol prompt'u — KULLANICI TARAFINDAN TASARLANDI, birebir
# kullanılıyor. Küçük model (qwen2.5-0.5b) bu talimatla SADECE geçerli bir JSON objesi üretir:
# {"search_required": bool, "search_query": str}. search_required=true ise search_query alanı,
# ham kullanıcı mesajı yerine Tavily'ye gönderilecek OPTİMİZE EDİLMİŞ arama sorgusunu içerir.
DECIDE_SYSTEM_PROMPT = """# ROL VE GÖREV
Sen sadece kullanıcının girdisini analiz ederek gerçek zamanlı bir internet araması (Google, Bing vb.) gerekip gerekmediğini tespit eden, yüksek hassasiyetli bir karar mekanizmasısın. Görevin sadece aşağıdaki kurallara göre kurumsal bir JSON çıktısı üretmektir.

# KESİN KURALLAR
1. Sadece ve sadece geçerli bir JSON objesi döndür.
2. JSON dışında asla selamlama, açıklama, markdown işareti (```json gibi) veya ek metin ekleme.
3. Kararsız kaldığın her durumda riske girmemek adına "search_required": true olarak karar ver.

# TETİKLEME KRİTERLERİ

## [search_required = true] Olacak Durumlar (ARAMA GEREKLİ):
1. ZAMAN DUYARLI: Güncel tarih, saat, yıl, resmi tatiller veya takvimsel durumlar.
2. FİNANS/HAVA DURUMU: Döviz kurları, borsa verileri, kripto fiyatları, anlık veya haftalık hava tahminleri.
3. MEDYA VE EĞLENCE: Yeni çıkan, vizyona giren, henüz yayınlanmamış veya devam eden filmler, diziler, yeni sezon tarihleri, müzik albümleri, liste başı şarkılar, kitaplar, ödül törenleri ve magazin haberleri.
4. GÜNDEM/SPOR: Son dakika haberleri, güncel siyasi olaylar, yeni kabine/başkan kararları, canlı maç skorları, puan durumları ve transfer gelişmeleri.
5. TEKNOLOJİ/TİCARET: Yazılım kütüphanelerinin en son sürümleri, yeni donanım (GPU/CPU/Telefon) özellikleri ve e-ticaret sitelerindeki anlık ürün fiyat karşılaştırmaları.

## [search_required = false] Olacak Durumlar (ARAMA GEREKSİZ):
1. SOHBET VE YARATICILIK: "Merhaba", "Nasılsın?", hikaye yazımı, şiir üretimi veya kişisel tavsiye istekleri.
2. STATİK BİLGİ/KLASİKLER: Matematik formülleri, felsefi teoriler, klasik tarih (Örn: "İstanbul ne zaman fethedildi?"), eski ve tamamlanmış klasik sanat eserlerinin özetleri.
3. KODLAMA VE DİL: Algoritma mantığı açıklamaları, kod optimizasyonları veya yabancı dil çevirileri.

# ÇIKTI FORMATI
{
  "search_required": boolean (true veya false),
  "search_query": "Arama motoruna yazılacak en optimize, yalın anahtar kelimeler. search_required false ise boş string yani \"\" olmalıdır."
}

# ÖRNEK SENARYOLAR (FEW-SHOT EXAMPLES)

Girdi: "Dolar bugün ne kadar oldu?"
Çıktı: {"search_required": true, "search_query": "güncel dolar kuru"}

Girdi: "Bana python ile bir quicksort algoritması yazar mısın?"
Çıktı: {"search_required": false, "search_query": ""}

Girdi: "House of the Dragon dizisinin 3. sezonu ne zaman çıkacak?"
Çıktı: {"search_required": true, "search_query": "House of the Dragon 3. sezon yayın tarihi"}

Girdi: "Şu an vizyonda hangi filmler var sinemada?"
Çıktı: {"search_required": true, "search_query": "vizyondaki filmler"}

Girdi: "Dostoyevski'nin Suç ve Ceza kitabının konusu nedir?"
Çıktı: {"search_required": false, "search_query": ""}

Girdi: "Geçen haftaki Galatasaray maçının özeti ve golleri kim attı?"
Çıktı: {"search_required": true, "search_query": "Galatasaray son maç sonucu goller"}"""


def _extract_json_object(raw_text: str) -> Optional[dict]:
    """
    Küçük modelin döndürdüğü metinden ilk geçerli JSON objesini çıkarmaya çalışır.
    Modeller bazen talimata rağmen markdown code fence (```json ... ```) veya ekstra
    boşluk/metin ekleyebilir; bu yüzden doğrudan json.loads yerine önce ham metni,
    olmazsa metin içindeki ilk {...} bloğunu ayıklayıp parse ediyoruz. İkisi de
    başarısız olursa None döner (çağıran taraf bunu güvenli taraf sinyali olarak kullanır).
    """
    text = raw_text.strip()
    # 1. Doğrudan parse dene
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 2. Markdown code fence temizle (```json ... ``` veya ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    # 3. Metin içindeki ilk {...} bloğunu regex ile yakala (en dıştaki süslü parantez çifti)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


@app.post("/decide", response_model=DecideResponse)
async def decide_needs_realtime_info(request: DecideRequest):
    """
    'Çırak-usta' mimarisinin karar adımı: kullanıcının sorusunu KÜÇÜK modele (qwen2.5-0.5b,
    ayrı port 8088'de, ana 14B modelden bağımsız çalışır) verip, bu sorunun güncel/harici/
    gerçek-zamanlı bilgi gerektirip gerektirmediğine karar verdiriyoruz. Küçük model AYRI bir
    süreçte/portta çalıştığı için ana modelle kaynak çekişmesine girmez.

    v3 — YAPILANDIRILMIŞ JSON ÇIKTI: Küçük modelden artık düz EVET/HAYIR metni yerine
    {"search_required": bool, "search_query": str} biçiminde bir JSON isteniyor (prompt
    kullanıcı tarafından tasarlandı, DECIDE_SYSTEM_PROMPT içinde birebir kullanılıyor).
    search_required=true olduğunda search_query alanı, ham kullanıcı mesajı yerine Tavily'ye
    gönderilecek OPTİMİZE EDİLMİŞ arama sorgusunu taşır — böylece web araması hem daha isabetli
    hem de daha alakalı sonuçlar döndürür.

    Güvenlik katmanları (üç kademeli):
    1. JSON parse başarısız olursa veya "search_required" alanı eksik/geçersizse → güvenli
       tarafta kal (needs_realtime_info=True), ham kullanıcı mesajını search_query olarak kullan.
    2. Küçük model servisine hiç ulaşılamazsa (timeout/bağlantı hatası) → aynı şekilde güvenli
       tarafta kal.
    3. Küçük model search_required=false derse bile, mesajda güncel/finansal/zamana-duyarlı
       belirgin anahtar kelimeler (REALTIME_OVERRIDE_KEYWORDS) varsa karar EVET'e zorlanır —
       tek bir 0.5B modelin kararına tam bağımlı kalınmaz.
    """
    try:
        request_timeout = httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{DRAFT_LLAMA_SERVER_URL}/v1/chat/completions",
                json={
                    "model": "models/qwen2.5-0.5b-instruct-q4_k_m.gguf",
                    "messages": [
                        {"role": "system", "content": DECIDE_SYSTEM_PROMPT},
                        {"role": "user", "content": request.query}
                    ],
                    "temperature": 0.0,
                    "max_tokens": 120,
                    "stream": False
                }
            )

        if response.status_code != 200:
            logger.warning(f"/decide: Çırak model beklenmeyen durum kodu döndürdü: {response.status_code}. Güvenli taraf: EVET.")
            return DecideResponse(needs_realtime_info=True, search_query=request.query, raw_output="", fallback_used=True)

        raw_output = response.json()["choices"][0]["message"]["content"].strip()
        parsed = _extract_json_object(raw_output)

        if parsed is None or "search_required" not in parsed or not isinstance(parsed.get("search_required"), bool):
            # JSON parse edilemedi veya beklenen alan/tip yok — güvenli tarafta kal
            logger.warning(f"/decide: Çırak modelden geçersiz/parse edilemeyen JSON çıktısı: '{raw_output}'. Güvenli taraf: EVET.")
            return DecideResponse(needs_realtime_info=True, search_query=request.query, raw_output=raw_output, fallback_used=True)

        needs_realtime_info = parsed["search_required"]
        search_query = parsed.get("search_query") or ""
        if not isinstance(search_query, str):
            search_query = ""
        search_query = search_query.strip()

        # search_required=true ama search_query boşsa (model kuralı tam takip etmemiş olabilir),
        # ham kullanıcı mesajını arama sorgusu olarak kullan.
        if needs_realtime_info and not search_query:
            search_query = request.query

        # İKİNCİ SAVUNMA KATMANI: Küçük model HAYIR dedi ama mesajda güncel/zamana-duyarlı
        # bilgiye açıkça işaret eden belirgin anahtar kelimeler varsa, kararı EVET'e çeviriyoruz.
        # Bu, tek bir 0.5B modelin olası yanlış sınıflandırmasına karşı ucuz bir güvenlik ağıdır.
        override_triggered = False
        if not needs_realtime_info:
            text_lower = request.query.lower()
            if any(keyword in text_lower for keyword in REALTIME_OVERRIDE_KEYWORDS):
                needs_realtime_info = True
                search_query = request.query
                override_triggered = True

        override_note = " [ANAHTAR KELIME OVERRIDE ILE EVET'E CEVRILDI]" if override_triggered else ""
        logger.info(
            f"/decide: Sorgu: '{request.query[:50]}...' | Çırak model JSON çıktısı: '{raw_output}' | "
            f"Karar: {'EVET' if needs_realtime_info else 'HAYIR'} | search_query: '{search_query}'{override_note}"
        )
        return DecideResponse(
            needs_realtime_info=needs_realtime_info,
            search_query=search_query,
            raw_output=raw_output,
            fallback_used=False
        )

    except Exception as e:
        logger.warning(f"/decide: Çırak model servisine ulaşılamadı ({DRAFT_LLAMA_SERVER_URL}): {e}. Güvenli taraf: EVET.")
        return DecideResponse(needs_realtime_info=True, search_query=request.query, raw_output="", fallback_used=True)


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

# =====================================================================================
# YENİ MİMARİ: STREAMING ÇIRAK-USTA (DRAFT-CRITIQUE) İŞBİRLİKÇİ BORU HATTI
# =====================================================================================
# Bu bölüm, kullanıcının doğrudan taleplerine göre inşa edilen yeni mimariyi uygular:
#   1. Shared Context: RAG + web search bir kez çekilir, hem çırak hem usta bunu okur.
#   2. Streaming pipe: Çırak (0.5B) taslak üretirken chunk'lar asyncio.Queue üzerinden
#      usta (14B) modele canlı akıtılır (bkz. collab_orchestrator.py).
#   3. Kod seviyesinde kesin max-tur sınırı (COLLAB_MAX_REVISION_TURNS, varsayılan 2).
#   4. approval_token ile early-exit: usta "APPROVAL_OK" derse döngü ANINDA kesilir.
#
# NOT (CPU ÇEKİŞMESİ - dürüst uyarı): Sunucuda GPU yok, 4 OCPU var. İki modeli aynı anda
# tam paralel token üretecek şekilde çalıştırmak CPU çekirdeklerini paylaştırır. Bu yüzden
# varsayılan davranış (COLLAB_EARLY_START_CHARS=0) SIRALI çalışır: çırak taslağı bitirir,
# usta ardından değerlendirir. Gerçek paralel/iç-içe (interleaved) mod isteğe bağlı olarak
# COLLAB_EARLY_START_CHARS>0 ile açılabilir, ancak ölçümlerimiz (bkz. rapor) CPU çekişmesi
# nedeniyle bunun net bir kazanç sağlamadığını gösteriyor — ayrıntılar README'de.


class CollabRequest(BaseModel):
    query: str
    # Vercel proxy'sinden gelen tam konuşma geçmişi (opsiyonel; yoksa sadece query kullanılır)
    conversation_history: Optional[List[dict]] = None


async def _memory_search_adapter(query: str) -> dict:
    """collab_orchestrator.fetch_shared_context için /search endpoint'inin mantığını
    doğrudan (HTTP round-trip yapmadan, aynı process içinde) çağıran adapter."""
    try:
        response = await search_memory(SearchRequest(query=query, top_k=2))
        return response.model_dump()
    except Exception as e:
        logger.error(f"Shared Context için RAG araması başarısız: {e}")
        return {"results": [], "found": False}


async def _web_search_adapter(query: str) -> dict:
    """collab_orchestrator.fetch_shared_context için /web_search endpoint'inin mantığını
    doğrudan (aynı process içinde) çağıran adapter."""
    try:
        response = await web_search(WebSearchRequest(query=query, max_results=2))
        return response.model_dump()
    except Exception as e:
        logger.error(f"Shared Context için web araması başarısız: {e}")
        return {"results": [], "found": False}


async def _decide_adapter(query: str) -> dict:
    """collab_orchestrator.fetch_shared_context için /decide endpoint'inin mantığını
    doğrudan (aynı process içinde) çağıran adapter."""
    try:
        response = await decide_needs_realtime_info(DecideRequest(query=query))
        return response.model_dump()
    except Exception as e:
        logger.error(f"Shared Context için /decide çağrısı başarısız: {e}")
        return {"needs_realtime_info": True, "search_query": query, "fallback_used": True}


@app.post("/collab_stream")
async def collab_stream(request: CollabRequest):
    """
    YENİ MİMARİ ANA ENDPOINT'İ: Streaming Çırak-Usta işbirlikçi cevap üretimi.

    Akış:
      1. Shared Context bir kez çekilir (RAG + gerekiyorsa web search).
      2. run_collaborative_pipeline çağrılır: çırak taslak üretir, usta değerlendirir,
         approval_token görülürse ANINDA break, yoksa en fazla MAX_REVISION_TURNS tur döner.
      3. Sonuç, tarayıcıya Server-Sent Events (SSE) formatında akıtılır:
         - Her turun çırak taslağı "draft" event'i olarak,
         - Usta eleştirisi/nihai cevabı "critique" event'i olarak,
         - Pipeline bitince "final" event'i (nihai cevap metni) ve zamanlama bilgisi gönderilir.

    Bu endpoint, mevcut /decide + /search + /web_search akışının YERİNE GEÇMEK ÜZERE
    tasarlanmıştır (route.ts bu endpoint'i çağıracak şekilde güncellenmelidir). Eski
    endpoint'ler geriye dönük uyumluluk ve olası rollback için olduğu gibi bırakıldı.
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="query alanı boş olamaz.")

    # Gelen conversation_history her öğesinin role/content içerdiğini doğruluyoruz;
    # aksi halde 400 ile net bir hata döndürüyoruz (pipeline içinde geç ve belirsiz bir
    # runtime hatası almak yerine).
    if request.conversation_history is not None:
        for i, item in enumerate(request.conversation_history):
            if not isinstance(item, dict) or "role" not in item or "content" not in item:
                raise HTTPException(
                    status_code=400,
                    detail=f"conversation_history[{i}] geçersiz: 'role' ve 'content' alanları gerekli."
                )

    conversation_messages = request.conversation_history or [
        {"role": "user", "content": request.query}
    ]

    async def event_generator():
        # SSE formatında bir event gönderen yardımcı fonksiyon
        def sse(event_name: str, data: dict) -> str:
            return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            # 1. ADIM: Shared Context'i BİR SEFERDE çek (RAG + web search)
            shared_context = await collab.fetch_shared_context(
                user_query=request.query,
                memory_search_fn=_memory_search_adapter,
                web_search_fn=_web_search_adapter,
                decide_fn=_decide_adapter
            )
            yield sse("shared_context_ready", {
                "rag_used": bool(shared_context.rag_text),
                "web_used": bool(shared_context.web_text),
                "decide_meta": shared_context.decide_meta
            })

            # 2. ADIM: Çırak-Usta işbirlikçi boru hattını çalıştır
            request_timeout = httpx.Timeout(connect=10.0, read=200.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                pipeline_result = await collab.run_collaborative_pipeline(
                    client=client,
                    draft_url=DRAFT_LLAMA_SERVER_URL,
                    usta_url=LLAMA_SERVER_URL,
                    draft_model_name=DRAFT_MODEL_NAME,
                    usta_model_name=USTA_MODEL_NAME,
                    shared_context=shared_context,
                    conversation_messages=conversation_messages
                )

            # 3. ADIM: Her turun sonucunu (taslak + eleştiri + zamanlama) sırayla akıt
            for round_result in pipeline_result["rounds"]:
                yield sse("draft", {
                    "turn_index": round_result["turn_index"],
                    "text": round_result["draft_text"]
                })
                yield sse("critique", {
                    "turn_index": round_result["turn_index"],
                    "text": round_result["critique_text"],
                    "approved": round_result["approved"],
                    "timing": round_result["timing"]
                })

            # 4. ADIM: Nihai cevabı ve pipeline özetini gönder
            yield sse("final", {
                "text": pipeline_result["final_answer"],
                "approved_early": pipeline_result["approved_early"],
                "turns_used": pipeline_result["turns_used"],
                "total_pipeline_seconds": pipeline_result["total_pipeline_seconds"]
            })

        except Exception as e:
            logger.error(f"/collab_stream sırasında hata: {e}", exc_info=True)
            yield sse("error", {"message": str(e)})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# 8. DOĞRUDAN ÇALIŞTIRMA DESTEĞİ
if __name__ == "__main__":
    logger.info("Cyber AI Hafıza Servisi port 8083 üzerinde başlatılıyor...")
    uvicorn.run(app, host="0.0.0.0", port=8083)