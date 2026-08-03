import os
import time
import json
import logging
import asyncio
import httpx
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, AsyncGenerator, Callable

# 1. LOGGING AYARLARI
# Çırak-Usta (Draft-Critique) işbirlikçi akışını izlemek için özel bir logger tanımlıyoruz.
logger = logging.getLogger("cyber-memory-service.collab")

# 2. YAPILANDIRMA VE SABİTLER
# Kod seviyesinde kesin max tur sınırı (varsayılan 2, en fazla 5 olacak şekilde sınırlandırılır)
MAX_REVISION_TURNS = int(os.getenv("COLLAB_MAX_REVISION_TURNS", "2"))
MAX_REVISION_TURNS = max(1, min(MAX_REVISION_TURNS, 5))  # Savunma amaçlı 1-5 arasına sıkıştırıyoruz

# Usta modelin 'değişiklik gerekmiyor, sonuç mükemmel' onayını bildiren token
APPROVAL_TOKEN = os.getenv("COLLAB_APPROVAL_TOKEN", "APPROVAL_OK")

# Çırak modelin stream chunk'larını usta modele canlı beslemek için kullanılan kuyruk boyutu
DRAFT_STREAM_CHUNK_QUEUE_MAXSIZE = 256

# Çırak modelin (0.5B) usta model (14B) ile CPU çekişmesini azaltmak için önerilen nice değeri.
# Bu değer systemd servis dosyasında veya taskset yapılandırmasında referans olarak kullanılır.
CPU_NICE_LEVEL_FOR_DRAFT = int(os.getenv("COLLAB_DRAFT_NICE", "10"))

# Usta modelin, çırak modelin taslağını tamamen bitirmesini beklemeden,
# belirli bir karakter sınırına ulaştığı an (erken tetikleme) çalışmaya başlaması için sınır.
# Varsayılan 0'dır (yani taslağın tamamen bitmesini bekler).
# CPU contention (çekişme) riski nedeniyle GPU'suz sunucularda 0 kalması önerilir.
CRITIQUE_EARLY_START_CHARS = int(os.getenv("COLLAB_EARLY_START_CHARS", "0"))


@dataclass
class SharedContext:
    """
    Kullanıcı sorusu geldiğinde bir kez çekilen ve hem çırak hem usta modelin
    ortaklaşa okuduğu 'Shared Context' (Tek Seferlik RAG ve Web Arama) veri yapısı.
    Bu yapı, modellerin kendi başlarına tekrar RAG araması yapmasını engeller ve
    llama.cpp'nin prompt caching mekanizmasıyla mükemmel uyum sağlar.
    """
    user_query: str
    rag_text: str = ""
    web_text: str = ""
    decide_meta: Dict[str, Any] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)

    def to_system_blocks(self) -> List[Dict[str, str]]:
        """
        Shared Context verilerini llama.cpp prompt caching özelliğine uygun,
        sabit ve her turda değişmeyen sistem mesajı blokları haline getirir.
        Bu sayede ilk turda önbelleğe alınan bağlam, sonraki turlarda 0ms'ye yakın sürede işlenir.
        """
        blocks = []
        
        # 1. RAG (Geçmiş Hafıza) Bağlamı
        if self.rag_text.strip():
            blocks.append({
                "role": "system",
                "content": f"Kullanıcının geçmiş hafızasından alınan alakalı bilgiler (sadece gerekliyse kullan):\n{self.rag_text.strip()}"
            })
            
        # 2. Web Arama Bağlamı
        if self.web_text.strip():
            blocks.append({
                "role": "system",
                "content": (
                    "GERÇEK ZAMANLI GÜNCEL WEB ARAMA SONUÇLARI (MUTLAKA KULLAN, ASLA 'güncel veri sağlayamam' deme, "
                    f"ASLA rakam/tarih uydurma — SADECE aşağıdaki bilgiyi kullan):\n{self.web_text.strip()}"
                )
            })
            
        return blocks


async def fetch_shared_context(
    user_query: str,
    memory_search_fn: Callable[[str], Any],
    web_search_fn: Callable[[str], Any],
    decide_fn: Callable[[str], Any]
) -> SharedContext:
    """
    Kullanıcı sorusunu aldığı ilk anda TÜM gerekli doküman parçacıklarını (RAG ve Web Arama)
    BİR SEFERDE çeker ve ortak bir Shared Context alanına koyar.
    
    Asenkron yapısı sayesinde, karar mekanizması (/decide) ve yerel RAG araması (/search)
    eş zamanlı (paralel) olarak tetiklenir. Web araması ise karara bağlı olarak ardışıl çalışır.
    """
    logger.info(f"Shared Context oluşturuluyor. Sorgu: '{user_query[:50]}...'")
    start_time = time.monotonic()

    # 1. Aşama: Karar mekanizması ve Yerel RAG aramasını paralel başlatıyoruz
    decide_task = asyncio.create_task(decide_fn(user_query))
    rag_task = asyncio.create_task(memory_search_fn(user_query))

    # İki işlemin de tamamlanmasını bekliyoruz
    decide_result, rag_result = await asyncio.gather(decide_task, rag_task)

    # RAG metnini hazırlıyoruz
    rag_text = ""
    if rag_result and rag_result.get("found") and rag_result.get("results"):
        rag_text = "\n".join([f"- {r['text']}" for r in rag_result["results"]])

    # Karar sonucunu analiz ediyoruz
    needs_realtime = decide_result.get("needs_realtime_info", False) if isinstance(decide_result, dict) else False
    search_query = decide_result.get("search_query", user_query) if isinstance(decide_result, dict) else user_query
    
    # 2. Aşama: Eğer karar mekanizması web araması gerekli dediyse web aramasını tetikliyoruz
    web_text = ""
    decide_meta = {
        "needs_realtime_info": needs_realtime,
        "search_query": search_query,
        "fallback_used": decide_result.get("fallback_used", False) if isinstance(decide_result, dict) else True
    }

    if needs_realtime:
        logger.info(f"Web araması gerekli görüldü. Optimize sorgu: '{search_query}'")
        try:
            web_result = await web_search_fn(search_query)
            if web_result and web_result.get("found") and web_result.get("results"):
                web_text = "\n".join([f"- {r['title']}: {r['snippet']}" for r in web_result["results"]])
        except Exception as e:
            logger.error(f"Shared Context için web araması yapılırken hata: {e}")
    else:
        logger.info("Web araması gerekli görülmedi, atlanıyor.")

    elapsed = time.monotonic() - start_time
    logger.info(f"Shared Context başarıyla tamamlandı. Süre: {elapsed:.3f}sn | RAG: {bool(rag_text)} | Web: {bool(web_text)}")

    return SharedContext(
        user_query=user_query,
        rag_text=rag_text,
        web_text=web_text,
        decide_meta=decide_meta
    )


async def stream_llm_to_queue(
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    messages: List[Dict[str, str]],
    queue: asyncio.Queue,
    max_tokens: int,
    temperature: float,
    stop: Optional[List[str]] = None
) -> str:
    """
    llama-server'dan gelen SSE stream chunk'larını canlı olarak okur ve
    eş zamanlı olarak asyncio.Queue'ya besler. Bu sayede usta model,
    çırağın cümlesi bitmeden akışı canlı olarak tüketebilir.
    """
    full_text = ""
    endpoint = f"{base_url}/v1/chat/completions"
    
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    if stop:
        payload["stop"] = stop

    try:
        # SSE akışını başlatıyoruz
        async with client.stream("POST", endpoint, json=payload, timeout=120.0) as response:
            if response.status_code != 200:
                err_msg = f"LLM sunucusu hata döndürdü: {response.status_code}"
                logger.error(err_msg)
                raise RuntimeError(err_msg)

            # Satır satır SSE verisini okuyoruz
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        parsed = json.loads(data_str)
                        content = parsed["choices"][0]["delta"].get("content", "")
                        if content:
                            full_text += content
                            # Chunk'ı anında kuyruğa gönderiyoruz (Usta modelin canlı okuması için)
                            await queue.put(content)
                    except Exception as parse_err:
                        # Bozuk veya kısmi JSON satırlarını yoksayıyoruz
                        continue
                        
    except Exception as e:
        logger.error(f"Stream sırasında hata oluştu ({model_name}): {e}")
        # Hata durumunda bile kuyruğun kilitlenmesini önlemek için None gönderiyoruz
        raise e
    finally:
        # Akışın bittiğini belirten sentinel değeri kuyruğa ekliyoruz
        await queue.put(None)
        
    return full_text


async def consume_queue_as_live_context(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """
    asyncio.Queue'dan gelen verileri canlı olarak tüketen asenkron jeneratör.
    None sentinel değerini gördüğü an temiz bir şekilde durur.
    """
    while True:
        chunk = await queue.get()
        if chunk is None:
            queue.task_done()
            break
        yield chunk
        queue.task_done()


async def run_draft_critique_round(
    client: httpx.AsyncClient,
    draft_url: str,
    usta_url: str,
    draft_model_name: str,
    usta_model_name: str,
    shared_context: SharedContext,
    conversation_messages: List[Dict[str, str]],
    turn_index: int
) -> Dict[str, Any]:
    """
    Tek bir Çırak-Usta (Draft-Critique) döngüsünü çalıştırır.
    
    MİMARİ DETAY VE CPU ÇEKİŞMESİ (CONTENTION) UYARISI:
    --------------------------------------------------
    GPU'suz, sadece 4 OCPU'lu bir sunucuda iki modeli GERÇEK anlamda eş zamanlı (paralel)
    çalıştırmak, CPU çekirdekleri için yoğun bir çekişmeye (contention) yol açar.
    Bu durum, iki modelin de token üretim hızını (tokens per second) dramatik şekilde düşürebilir.
    
    Bu kod, mimari olarak hem tam paralel (interleaved) hem de sıralı (sequential) çalışmayı destekler:
    - CRITIQUE_EARLY_START_CHARS > 0 ise: Çırak belirli bir karakter ürettiği an Usta paralel tetiklenir.
    - CRITIQUE_EARLY_START_CHARS = 0 ise: Çırak taslağı tamamen bitirir, ardından Usta değerlendirmeye başlar.
    
    Ölçümlerimize göre CPU sunucularda en yüksek verim sıralı (0) veya hafif gecikmeli paralel modda alınır.
    """
    logger.info(f"--- Çırak-Usta Döngüsü Başlıyor (Tur: {turn_index}) ---")
    round_start = time.monotonic()

    # 1. Çırak (Draft) için mesajları hazırlıyoruz
    draft_system_prompt = (
        "Sen Cyber AI projesinin 'Çırak' (Draft) modelisin. Görevin, kullanıcının sorusuna "
        "hızlı, ham ve kapsamlı bir ilk taslak cevap üretmektir. Mükemmel olmaya çalışma, "
        "detayları usta modele bırak. Türkçe cevap ver."
    )
    
    draft_messages = (
        shared_context.to_system_blocks() + 
        [{"role": "system", "content": draft_system_prompt}] + 
        conversation_messages
    )

    # Chunk'ların akacağı boru hattı (Queue)
    chunk_queue = asyncio.Queue(maxsize=DRAFT_STREAM_CHUNK_QUEUE_MAXSIZE)

    # Çırak modelin stream görevini arka planda başlatıyoruz (Eş zamanlı üretim başlar)
    draft_task = asyncio.create_task(
        stream_llm_to_queue(
            client=client,
            base_url=draft_url,
            model_name=draft_model_name,
            messages=draft_messages,
            queue=chunk_queue,
            max_tokens=1024,
            temperature=0.6
        )
    )

    draft_start_time = time.monotonic()
    accumulated_draft_text = ""
    usta_task = None
    usta_started = False
    usta_start_time = 0.0
    usta_end_time = 0.0

    # Usta (Critique) için sistem talimatı
    usta_system_prompt = (
        "Sen Cyber AI projesinin 'Usta' (Critique/Editor) modelisin. Görevin, çırak modelin ürettiği "
        "taslak cevabı, Shared Context (RAG ve Web Arama) verileri doğrultusunda titizlikle incelemek, "
        "hataları düzeltmek ve nihai mükemmel cevabı üretmektir.\n\n"
        "Eğer çırağın taslağı tamamen doğru, eksiksiz ve mükemmelse, cevabına SADECE VE SADECE "
        f"'{APPROVAL_TOKEN}' yazarak onay ver. Ekstra hiçbir açıklama ekleme.\n"
        "Eğer taslakta düzeltilmesi gereken yerler varsa, düzeltilmiş nihai cevabı doğrudan üret."
    )

    # Kuyruktan gelen verileri canlı olarak okuyoruz
    async for chunk in consume_queue_as_live_context(chunk_queue):
        accumulated_draft_text += chunk
        
        # Eğer erken tetikleme aktifse ve usta henüz başlatılmadıysa
        if (CRITIQUE_EARLY_START_CHARS > 0 and 
            len(accumulated_draft_text) >= CRITIQUE_EARLY_START_CHARS and 
            not usta_started):
            
            logger.info(f"Usta model erken tetikleniyor ({len(accumulated_draft_text)} karakter ulaşıldı)...")
            usta_started = True
            usta_start_time = time.monotonic()
            
            # Usta için o anki kısmi taslakla prompt oluşturuyoruz
            usta_messages = (
                shared_context.to_system_blocks() +
                [{"role": "system", "content": usta_system_prompt}] +
                conversation_messages +
                [{"role": "system", "content": f"[KISMİ TASLAK - Çırak akışı devam ediyor]:\n{accumulated_draft_text}"}]
            )
            
            # Usta modelin çağrısını asenkron başlatıyoruz
            usta_task = asyncio.create_task(
                client.post(
                    f"{usta_url}/v1/chat/completions",
                    json={
                        "model": usta_model_name,
                        "messages": usta_messages,
                        "temperature": 0.2,
                        "max_tokens": 2048,
                        "stream": False
                    },
                    timeout=150.0
                )
            )

    # Çırak modelin tamamen bitmesini bekliyoruz.
    # NOT: Erken tetikleme modunda (CRITIQUE_EARLY_START_CHARS>0) usta_task da bu noktada
    # arka planda çalışıyor olabilir. Çırak hata verirse, usta_task'ı sarkıtmamak (dangling
    # task / "exception never retrieved" riski) için burada da güvenli şekilde iptal ediyoruz.
    try:
        draft_text = await draft_task
    except Exception:
        if usta_task is not None and not usta_task.done():
            usta_task.cancel()
            try:
                await usta_task
            except (asyncio.CancelledError, Exception):
                pass
        raise
    draft_end_time = time.monotonic()
    draft_duration = draft_end_time - draft_start_time
    logger.info(f"Çırak model taslağı tamamladı ({len(draft_text)} karakter, {draft_duration:.3f}sn).")

    # Eğer usta model erken tetiklenmediyse (varsayılan güvenli akış), şimdi başlatıyoruz
    if not usta_started:
        logger.info("Usta model taslağın tamamlanmasının ardından başlatılıyor (Sıralı Akış)...")
        usta_started = True
        usta_start_time = time.monotonic()
        
        usta_messages = (
            shared_context.to_system_blocks() +
            [{"role": "system", "content": usta_system_prompt}] +
            conversation_messages +
            [{"role": "system", "content": f"[ÇIRAK TASLAĞI]:\n{draft_text}"}]
        )
        
        usta_task = asyncio.create_task(
            client.post(
                f"{usta_url}/v1/chat/completions",
                json={
                    "model": usta_model_name,
                    "messages": usta_messages,
                    "temperature": 0.2,
                    "max_tokens": 2048,
                    "stream": False
                },
                timeout=150.0
            )
        )

    # Usta modelin yanıtını bekliyoruz
    usta_response = await usta_task
    usta_end_time = time.monotonic()
    usta_duration = usta_end_time - usta_start_time

    if usta_response.status_code != 200:
        err_msg = f"Usta model hata döndürdü: {usta_response.status_code}"
        logger.error(err_msg)
        raise RuntimeError(err_msg)

    critique_text = usta_response.json()["choices"][0]["message"]["content"].strip()
    logger.info(f"Usta model değerlendirmeyi tamamladı ({len(critique_text)} karakter, {usta_duration:.3f}sn).")

    # Onay durumunu kontrol ediyoruz
    # Usta model APPROVAL_TOKEN'ı tek başına veya metin içinde net şekilde döndürdüyse onaylanmıştır.
    approved = APPROVAL_TOKEN in critique_text
    
    # Zamanlama çakışmasını (overlap) hesaplıyoruz
    total_duration = time.monotonic() - round_start
    overlap = 0.0
    if CRITIQUE_EARLY_START_CHARS > 0 and usta_start_time < draft_end_time:
        overlap = min(draft_end_time, usta_end_time) - usta_start_time

    logger.info(
        f"Döngü Sonucu: Approved={approved} | Toplam Süre: {total_duration:.3f}sn "
        f"(Çırak: {draft_duration:.3f}sn, Usta: {usta_duration:.3f}sn, Çakışma: {overlap:.3f}sn)"
    )

    return {
        "draft_text": draft_text,
        "critique_text": critique_text,
        "approved": approved,
        "turn_index": turn_index,
        "early_start_used": CRITIQUE_EARLY_START_CHARS > 0,
        "timing": {
            "draft_seconds": draft_duration,
            "usta_seconds": usta_duration,
            "total_seconds": total_duration,
            "concurrent_overlap_seconds": overlap
        }
    }


async def run_collaborative_pipeline(
    client: httpx.AsyncClient,
    draft_url: str,
    usta_url: str,
    draft_model_name: str,
    usta_model_name: str,
    shared_context: SharedContext,
    conversation_messages: List[Dict[str, str]]
) -> Dict[str, Any]:
    """
    Çırak-Usta işbirlikçi boru hattının (pipeline) tamamını yöneten ana giriş noktası.
    
    - Belirlenen MAX_REVISION_TURNS sınırına kadar döngüyü çalıştırır.
    - Usta model 'APPROVAL_OK' (APPROVAL_TOKEN) döndürdüğü an döngüyü break ile keser (Early-exit).
    - Her turda usta eleştirisini bir sonraki turun çırağına girdi olarak besler.
    """
    pipeline_start = time.monotonic()
    rounds = []
    approved_early = False
    turns_used = 0
    
    # Çalışma boyunca güncellenecek konuşma geçmişi kopyası
    current_conversation = list(conversation_messages)
    
    final_answer = ""

    for turn in range(1, MAX_REVISION_TURNS + 1):
        turns_used = turn
        
        # Turu çalıştır
        round_result = await run_draft_critique_round(
            client=client,
            draft_url=draft_url,
            usta_url=usta_url,
            draft_model_name=draft_model_name,
            usta_model_name=usta_model_name,
            shared_context=shared_context,
            conversation_messages=current_conversation,
            turn_index=turn
        )
        
        rounds.append(round_result)
        
        if round_result["approved"]:
            logger.info(f"Usta model {turn}. turda onay verdi! Erken çıkış yapılıyor.")
            approved_early = True
            # Onay durumunda çırağın son taslağı nihai cevaptır
            final_answer = round_result["draft_text"]
            break
        else:
            # Onay verilmediyse, ustanın düzeltilmiş cevabı nihai cevap adayıdır
            final_answer = round_result["critique_text"]
            
            # Eğer daha turlarımız varsa, ustanın eleştirisini bir sonraki tura besliyoruz
            if turn < MAX_REVISION_TURNS:
                logger.info("Usta onay vermedi. Eleştiri bir sonraki tura aktarılıyor.")
                current_conversation.append({
                    "role": "system",
                    "content": (
                        f"Önceki taslağa yönelik usta eleştirisi ve düzeltmesi:\n{round_result['critique_text']}\n\n"
                        "Lütfen bu eleştiriyi ve düzeltmeleri dikkate alarak taslağı yeniden yaz."
                    )
                })

    total_pipeline_seconds = time.monotonic() - pipeline_start
    logger.info(
        f"İşbirlikçi Boru Hattı Tamamlandı. Kullanılan Tur: {turns_used}/{MAX_REVISION_TURNS} | "
        f"Erken Onay: {approved_early} | Toplam Süre: {total_pipeline_seconds:.3f}sn"
    )

    return {
        "rounds": rounds,
        "final_answer": final_answer,
        "approved_early": approved_early,
        "turns_used": turns_used,
        "total_pipeline_seconds": total_pipeline_seconds
    }