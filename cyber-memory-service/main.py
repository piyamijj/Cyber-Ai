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
# "Çırak" (draft/küçük) model — qwen2.5-0.5b-instruct, ayrı bir llama-server sürecinde,
# ayrı bir portta (varsayılan 8088) bağımsız olarak çalışır. Bu model /decide endpoint'i
# için kullanılır: kullanıcı mesajının güncel/harici bilgi gerektirip gerektirmediğine
# hızlıca (düşük max_tokens ile ~1-2sn içinde) karar verir. Ana (14B, "usta") modelden
# tamamen bağımsızdır — kaynak çekişmesine girmez, bu yüzden ana sohbet gecikmesini artırmaz.
DRAFT_LLAMA_SERVER_URL = os.getenv("DRAFT_LLAMA_SERVER_URL", "http://localhost:8088")
EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"

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

            results.append(WebSearchResult(
                title=title[:300],
                snippet=snippet[:600],
                url=url
            ))

        found = len(results) > 0
        logger.info(f"Web araması (Tavily) tamamlandı. Sorgu: '{request.query[:40]}...' | Bulunan sonuç: {len(results)}")

        return WebSearchResponse(results=results, found=found)

    except Exception as e:
        logger.error(f"Web araması (Tavily) sırasında hata: {e}", exc_info=True)
        # Web arama hatası ana sohbet akışını çökertmesin diye boş sonuç dönüyoruz
        return WebSearchResponse(results=[], found=False)


@app.post("/decide", response_model=DecideResponse)
async def decide_needs_realtime_info(request: DecideRequest):
    """
    'Çırak-usta' mimarisinin karar adımı: kullanıcının sorusunu KÜÇÜK modele (qwen2.5-0.5b,
    ayrı port 8088'de, ana 14B modelden bağımsız çalışır) verip, bu sorunun güncel/harici/
    gerçek-zamanlı bilgi gerektirip gerektirmediğine SADECE EVET/HAYIR ile karar verdiriyoruz.

    Bu adım eskiden (route.ts içinde) basit bir anahtar kelime taramasıydı (isLikelyTimeSensitiveQuery)
    — hem isabetsizdi hem de RAG/web search'ün gereksiz yere çalışıp büyük modele (CPU'da yavaş)
    ekstra gecikme eklemesine yol açıyordu. Küçük model AYRI bir süreçte/portta çalıştığı için
    ana modelle kaynak çekişmesine girmez; max_tokens çok düşük tutulduğu için (~5-10) cevap
    tipik olarak 1-2 saniyede döner.

    Küçük model servisine ulaşılamazsa veya beklenmeyen bir çıktı dönerse, GÜVENLİ TARAFTA
    kalıyoruz: needs_realtime_info=True döndürüyoruz (yani eskisi gibi RAG+web search çalışsın) —
    böylece karar mekanizması arızalansa bile en kötü ihtimalle "gereksiz yere biraz gecikme"
    yaşanır, ama güncel bilgi gerektiren bir soru asla sessizce atlanmaz.

    NOT (v2 — few-shot iyileştirmesi): İlk sürümde prompt açıklama tabanlıydı ve 0.5B model
    "bugün dolar kuru kaç, güncel durum ne?" gibi AÇIKÇA güncel veri gerektiren bir soruyu bile
    HAYIR olarak yanlış sınıflandırabiliyordu. Küçük modeller soyut tanımlardan çok somut
    örneklerden (few-shot) çok daha iyi genelleme yapar; bu yüzden prompt'a net EVET/HAYIR
    örnekleri eklendi. Ayrıca ikinci bir savunma katmanı olarak, küçük model HAYIR derse bile
    mesajda güncel/finansal/zamana-duyarlı belirgin anahtar kelimeler varsa karar EVET'e
    çevriliyor (bkz. aşağıdaki REALTIME_OVERRIDE_KEYWORDS kontrolü) — böylece tek bir küçük
    modelin kararına tamamen bağımlı kalınmıyor.
    """
    decision_prompt = (
        "Bir kullanıcı mesajının GÜNCEL/HARİCİ/GERÇEK-ZAMANLI bilgi (döviz kuru, hava durumu, "
        "haberler, son dakika, fiyatlar, skorlar, bugünün tarihi/olayları vb.) gerektirip "
        "gerektirmediğine karar veren bir sınıflandırıcısın. SADECE 'EVET' veya 'HAYIR' yaz.\n\n"
        "Örnekler:\n"
        "Mesaj: bugün dolar kuru kaç?\nCevap: EVET\n\n"
        "Mesaj: bugün hava nasıl olacak?\nCevap: EVET\n\n"
        "Mesaj: dün haberlerde ne oldu?\nCevap: EVET\n\n"
        "Mesaj: son dakika deprem oldu mu?\nCevap: EVET\n\n"
        "Mesaj: bitcoin fiyatı şu an ne kadar?\nCevap: EVET\n\n"
        "Mesaj: yarın İstanbul'da maç var mı, skor ne olur?\nCevap: EVET\n\n"
        "Mesaj: sen kimsin?\nCevap: HAYIR\n\n"
        "Mesaj: python nedir, nasıl öğrenilir?\nCevap: HAYIR\n\n"
        "Mesaj: merhaba, nasılsın?\nCevap: HAYIR\n\n"
        "Mesaj: bana bir şiir yazar mısın?\nCevap: HAYIR\n\n"
        "Mesaj: 2. dünya savaşı ne zaman bitti?\nCevap: HAYIR\n\n"
        "Mesaj: bir fonksiyonu python'da nasıl tanımlarım?\nCevap: HAYIR\n\n"
        f"Şimdi bu mesajı sınıflandır:\nMesaj: {request.query}\nCevap:"
    )

    try:
        request_timeout = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{DRAFT_LLAMA_SERVER_URL}/v1/chat/completions",
                json={
                    "model": "models/qwen2.5-0.5b-instruct-q4_k_m.gguf",
                    "messages": [
                        {"role": "system", "content": "Sen sadece EVET veya HAYIR yazan kısa bir sınıflandırıcısın. Örnekleri dikkatlice takip et."},
                        {"role": "user", "content": decision_prompt}
                    ],
                    "temperature": 0.0,
                    "max_tokens": 8,
                    "stream": False
                }
            )

        if response.status_code != 200:
            logger.warning(f"/decide: Çırak model beklenmeyen durum kodu döndürdü: {response.status_code}. Güvenli taraf: EVET.")
            return DecideResponse(needs_realtime_info=True, raw_output="", fallback_used=True)

        raw_output = response.json()["choices"][0]["message"]["content"].strip()
        normalized = raw_output.upper()

        if "HAYIR" in normalized and "EVET" not in normalized:
            needs_realtime_info = False
        elif "EVET" in normalized:
            needs_realtime_info = True
        else:
            # Beklenmeyen/çok belirsiz çıktı — güvenli tarafta kal
            logger.warning(f"/decide: Çırak modelden belirsiz çıktı: '{raw_output}'. Güvenli taraf: EVET.")
            needs_realtime_info = True

        # İKİNCİ SAVUNMA KATMANI: Küçük model HAYIR dedi ama mesajda güncel/zamana-duyarlı
        # bilgiye açıkça işaret eden belirgin anahtar kelimeler varsa, kararı EVET'e çeviriyoruz.
        # Bu, tek bir 0.5B modelin olası yanlış sınıflandırmasına karşı ucuz bir güvenlik ağıdır.
        override_triggered = False
        if not needs_realtime_info:
            text_lower = request.query.lower()
            if any(keyword in text_lower for keyword in REALTIME_OVERRIDE_KEYWORDS):
                needs_realtime_info = True
                override_triggered = True

        override_note = " [ANAHTAR KELIME OVERRIDE ILE EVET'E CEVRILDI]" if override_triggered else ""
        logger.info(
            f"/decide: Sorgu: '{request.query[:50]}...' | Çırak model çıktısı: '{raw_output}' | "
            f"Karar: {'EVET' if needs_realtime_info else 'HAYIR'}{override_note}"
        )
        return DecideResponse(needs_realtime_info=needs_realtime_info, raw_output=raw_output, fallback_used=False)

    except Exception as e:
        logger.warning(f"/decide: Çırak model servisine ulaşılamadı ({DRAFT_LLAMA_SERVER_URL}): {e}. Güvenli taraf: EVET.")
        return DecideResponse(needs_realtime_info=True, raw_output="", fallback_used=True)


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