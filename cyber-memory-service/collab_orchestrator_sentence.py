import os
import re
import time
import json
import logging
import asyncio
import httpx
from typing import List, Dict, Any, Optional

# collab_orchestrator.py modülünden ortak bileşenleri ve yapılandırmaları içe aktarıyoruz.
# Bu sayede kod tekrarını önlüyor ve tek bir doğruluk kaynağı (single source of truth) sağlıyoruz.
from collab_orchestrator import (
    SharedContext,
    fetch_shared_context,
    MAX_REVISION_TURNS,
    APPROVAL_TOKEN,
    REPEAT_PENALTY,
    REPEAT_LAST_N
)

# 1. LOGGING AYARLARI
logger = logging.getLogger("cyber-memory-service.collab.sentence")

# 2. YAPILANDIRMA VE SABİTLER
# Cümle bazlı onay için usta modelin 'bu cümle mükemmel' onayını bildiren hafif token
SENTENCE_APPROVAL_TOKEN = os.getenv("COLLAB_SENTENCE_APPROVAL_TOKEN", "SENTENCE_OK")

# Yüksek reddedilme oranı eşiği. Eğer çırağın ürettiği cümlelerin %30'undan fazlası
# usta tarafından reddedilip düzeltildiyse, bu turun genel kalitesi düşük kabul edilir
# ve bir sonraki tam revizyon turuna (turn) geçilir.
HIGH_REJECTION_RATIO_THRESHOLD = float(os.getenv("COLLAB_HIGH_REJECTION_RATIO", "0.3"))

# Türkçe ve genel noktalama işaretlerine göre cümle sınırlarını tespit eden regex.
# Nokta, ünlem, soru işareti veya üç noktadan sonra gelen boşlukları yakalar.
# Noktalama işaretini cümlenin içinde tutacak şekilde tasarlanmıştır.
SENTENCE_BOUNDARY_REGEX = re.compile(r'([^.!?\s]+(?:[^.!?]*[.!?]+)+)(?=\s|$)')


class SentenceBuffer:
    """
    Gelen stream chunk'larını biriktiren ve Türkçe cümle sınırlarını
    pragmatik olarak tespit eden tampon bellek sınıfı.
    
    NOT: Bu sınıf dilbilimsel olarak kusursuz bir NLP tokenizer değildir;
    ancak gerçek zamanlı akışlarda cümle sınırlarını yakalamak için son derece
    hızlı, hafif ve pratik bir heuristiktir.
    """
    def __init__(self):
        self.buffer = ""

    def feed(self, chunk: str) -> List[str]:
        """
        Yeni gelen metin parçasını tampona ekler ve tamamlanmış cümleleri döner.
        Tamamlanmamış son cümle tamponda kalmaya devam eder.
        """
        self.buffer += chunk
        sentences = []
        
        # Regex ile tamamlanmış cümleleri buluyoruz
        matches = list(SENTENCE_BOUNDARY_REGEX.finditer(self.buffer))
        if not matches:
            return sentences

        last_end = 0
        for match in matches:
            sentence = match.group(1).strip()
            if sentence:
                sentences.append(sentence)
            last_end = match.end()

        # Tamamlanmış cümleleri tampondan temizliyoruz
        self.buffer = self.buffer[last_end:]
        return sentences

    def flush(self) -> Optional[str]:
        """
        Akış bittiğinde tamponda kalan son metin parçasını (noktalama işareti
        olmasa bile) son cümle olarak döner ve tamponu temizler.
        """
        remaining = self.buffer.strip()
        self.buffer = ""
        return remaining if remaining else None


async def relay_sentence_to_usta(
    client: httpx.AsyncClient,
    usta_url: str,
    usta_model_name: str,
    shared_context: SharedContext,
    sentences_so_far: List[str],
    candidate_sentence: str
) -> Dict[str, Any]:
    """
    Çırağın ürettiği TEK BİR cümleyi değerlendirmesi için usta modele gönderir.
    
    Usta model, bu cümleyi Shared Context (RAG ve Web Arama) verileri, KULLANICININ ASIL
    SORUSU ve o ana kadar biriken cümlelerin bağlamsal akışına göre inceler.
    """
    start_time = time.monotonic()
    
    # Usta modelin tek bir cümleyi değerlendirmesi için sistem talimatı
    #
    # KALİTE GUARDRAIL'İ 1 (tekrar kontrolü): benchmark_architectures.sh testlerinde çırak
    # modelin (0.5B) AYNI CÜMLEYİ 6-7 kez art arda ürettiği ve usta modelin bunu FARK
    # ETMEDEN her tekrarı ayrı ayrı "SENTENCE_OK" ile onayladığı gözlemlendi — çünkü usta
    # her cümleyi İZOLE değerlendiriyordu, "bu cümle önceki cümlelerle BİREBİR AYNI mı"
    # diye bakmıyordu. Kural 1'i (tekrar kontrolü) EN BAŞA ve EN VURGULU şekilde ekliyoruz.
    #
    # KALİTE GUARDRAIL'İ 2 (alakalılık kontrolü — canlı site testinde bulunan KRİTİK EKSİK):
    # Kullanıcı canlı sitede "Bugünkü haber başlıklarından bana birkaçını ver" diye sorduğunda,
    # usta modelin ÜRETTİĞİ nihai cevabın SONUNA tamamen alakasız bir "güvenlik" cümlesi
    # eklendiği ve haber listesinin bozuk/anlamsız başlıklar içerdiği gözlemlendi. Kök neden:
    # bu fonksiyon önceden usta modele KULLANICININ NE SORDUĞUNU HİÇ BİLDİRMİYORDU — usta
    # her cümleyi sadece "önceki cümleler" bağlamıyla, sorudan bağımsız olarak değerlendiriyordu.
    # Bu yüzden usta, konu dışı/alakasız bir cümleyi ASLA yakalayamazdı (yakalayacak bir
    # referansı yoktu). Düzeltme: (a) kullanıcının asıl sorusu artık [KULLANICI SORUSU] bloğu
    # olarak usta'ya iletiliyor, (b) Kural 2 olarak açık bir ALAKALILIK kontrolü eklendi.
    usta_sentence_system_prompt = (
        "Sen Cyber AI projesinin 'Usta' (Critique/Editor) modelisin. Görevin, çırak modelin ürettiği "
        "tek bir aday cümleyi, KULLANICININ ASIL SORUSU, Shared Context (RAG ve Web Arama) verileri "
        "ve o ana kadar yazılmış önceki cümlelerin akışı doğrultusunda titizlikle incelemektir.\n\n"
        "KURALLAR (SIRAYLA UYGULA):\n"
        "1. TEKRAR KONTROLÜ (ÖNCELİKLİ): Aday cümle, '[ÖNCEKİ CÜMLELER]' listesindeki herhangi bir "
        "cümleyle BİREBİR AYNI veya ANLAMCA NEREDEYSE AYNI mı? Eğer öyleyse bu bir model hatasıdır "
        "(dejenere/repetition çıktı) — ASLA SENTENCE_OK ile onaylama. Bunun yerine BOŞ bir metin "
        "('') döndür (bu, bu tekrarlanan cümlenin nihai cevaptan tamamen çıkarılacağı anlamına gelir).\n"
        "2. ALAKALILIK KONTROLÜ (KRİTİK): Aday cümle, '[KULLANICI SORUSU]' ile GERÇEKTEN ilgili mi? "
        "Cümle, sorulan konudan FARKLI bir konuya (örn. soru haberlerle ilgiliyken cümle güvenlik/başka "
        "bir konudan bahsediyorsa, ya da RAG'den gelen alakasız bir geçmiş kayıt sızmışsa) mi ait? "
        "Eğer cümle konu dışıysa/alakasızsa, ASLA SENTENCE_OK ile onaylama — BOŞ bir metin ('') döndür.\n"
        "3. BÜTÜNLÜK/OKUNABİLİRLİK KONTROLÜ: Aday cümle yarım, anlamsız, dilbilgisi açısından bozuk "
        "veya ham/işlenmemiş veri gibi mi görünüyor (örn. bir web arama sonucunun başlığı ile snippet'i "
        "birbirine karışmış, cümle tamamlanmamış)? Eğer öyleyse düzeltilmiş, akıcı bir cümle üret.\n"
        "4. DİL KONTROLÜ: Aday cümle, '[KULLANICI SORUSU]' ile AYNI dilde mi yazılmış? Kullanıcı "
        "Türkçe sorduysa cümle SADECE Türkçe olmalı — eğer cümlede İngilizce (veya başka bir dil) "
        "kelime/ifade varsa (örn. 'Hello!', 'Sure', 'Here is...'), cümleyi TAMAMEN Türkçeye çevirerek "
        "düzeltilmiş halini üret; ASLA olduğu gibi onaylama.\n"
        f"5. Eğer aday cümle yukarıdaki 4 kontrolden de geçiyorsa (tekrar değil, alakalı, akıcı, doğru "
        f"dilde) VE tamamen doğru ve kaliteliyse, SADECE '{SENTENCE_APPROVAL_TOKEN}' yaz.\n"
        "6. Eğer cümlede bilgi hatası, anlatım bozukluğu, dil hatası veya eksiklik varsa (ama tekrar/"
        "alakasız değilse), düzeltilmiş nihai cümleyi doğrudan yaz.\n"
        "7. Asla açıklama, selamlama veya ek metin ekleme. Sadece onay token'ını, boş metni veya "
        "düzeltilmiş cümlenin kendisini döndür."
    )

    # Bağlamı korumak için o ana kadar onaylanmış/düzeltilmiş cümleleri de usta modele iletiyoruz
    context_history = "\n".join([f"- {s}" for s in sentences_so_far]) if sentences_so_far else "Henüz cümle yazılmadı."

    messages = (
        shared_context.to_system_blocks() +
        [{"role": "system", "content": usta_sentence_system_prompt}] +
        [
            {"role": "system", "content": f"[KULLANICI SORUSU]:\n{shared_context.user_query}"},
            {"role": "system", "content": f"[ÖNCEKİ CÜMLELER (BAĞLAM)]:\n{context_history}"},
            {"role": "user", "content": f"[ADAY CÜMLE]:\n{candidate_sentence}"}
        ]
    )

    try:
        response = await client.post(
            f"{usta_url}/v1/chat/completions",
            json={
                "model": usta_model_name,
                "messages": messages,
                "temperature": 0.1,  # Kararlılık için çok düşük sıcaklık
                "max_tokens": 256,   # Tek bir cümle için fazlasıyla yeterli
                "stream": False,
                "repeat_penalty": REPEAT_PENALTY,
                "repeat_last_n": REPEAT_LAST_N
            },
            timeout=60.0
        )

        if response.status_code != 200:
            logger.error(f"Usta cümle değerlendirme hatası: {response.status_code}")
            # Hata durumunda güvenli tarafta kalıp çırağın cümlesini olduğu gibi kabul ediyoruz
            return {
                "accepted": True,
                "final_sentence": candidate_sentence,
                "raw_usta_output": f"ERROR_{response.status_code}",
                "duration": time.monotonic() - start_time
            }

        raw_output = response.json()["choices"][0]["message"]["content"].strip()
        
        # Onay durumunu kontrol ediyoruz
        accepted = SENTENCE_APPROVAL_TOKEN in raw_output
        final_sentence = candidate_sentence if accepted else raw_output

        logger.info(
            f"Cümle Değerlendirmesi: Accepted={accepted} | "
            f"Aday: '{candidate_sentence[:40]}...' | "
            f"Final: '{final_sentence[:40]}...' | Süre: {time.monotonic() - start_time:.3f}sn"
        )

        return {
            "accepted": accepted,
            "final_sentence": final_sentence,
            "raw_usta_output": raw_output,
            "duration": time.monotonic() - start_time
        }

    except Exception as e:
        logger.error(f"Usta cümle değerlendirme çağrısı sırasında beklenmeyen hata: {e}")
        return {
            "accepted": True,
            "final_sentence": candidate_sentence,
            "raw_usta_output": f"EXCEPTION_{str(e)}",
            "duration": time.monotonic() - start_time
        }


async def run_sentence_relay_round(
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
    Tek bir Cümle-Bazlı Sequential Relay turunu çalıştırır.
    
    MİMARİ AVANTAJ VE CPU DEĞERLENDİRMESİ:
    -------------------------------------
    Bu mimari, tam token-stream mimarisine göre CPU çekişmesini (contention) büyük ölçüde azaltır.
    Çünkü usta model (14B) tüm çırak akışı boyunca kesintisiz çalışmak yerine, sadece çırak
    bir cümleyi tamamladığında kısa süreli (düşük max_tokens ile) tetiklenir.
    
    PRAGMATİK TERCİH (Çırak Yönlendirme Sınırı):
    -------------------------------------------
    Çırak model (0.5B) mid-flight (üretim esnasında) çalışırken, llama.cpp API kısıtlamaları
    nedeniyle usta modelin yaptığı düzeltmeleri anlık olarak "duyamaz" (üretim yarıda kesilip
    yeni promptla yönlendirilemez). Bu yüzden çırak kendi taslağını üretmeye devam ederken,
    usta arkadan gelerek düzeltilmiş cümleleri nihai cevaba ekler (splice-in). Bu, mevcut
    teknolojik kısıtlar altında en gerçekçi ve stabil "sequential relay" uygulamasıdır.
    """
    logger.info(f"--- Cümle-Bazlı Relay Döngüsü Başlıyor (Tur: {turn_index}) ---")
    round_start = time.monotonic()

    # KALİTE DÜZELTMESİ (canlı site testinde bulundu): "Türkçe cevap ver" talimatı tek
    # başına yeterli olmadı — küçük çırak model (0.5B) "Merhaba" gibi Türkçe bir mesaja
    # bazen "Hello!" gibi İngilizce cevap üretti. Talimat artık DAHA VURGULU ve AÇIK: hem
    # "SADECE Türkçe" diye net bir kısıtlama hem de "kullanıcı hangi dilde yazdıysa O dilde
    # cevap ver" genel kuralı eklendi (ileride başka bir dilde soru gelirse diye). İkinci
    # savunma katmanı olarak usta modelin değerlendirme kuralına da bir dil kontrolü eklendi
    # (aşağıda relay_sentence_to_usta içinde).
    draft_system_prompt = (
        "Sen Cyber AI projesinin 'Çırak' (Draft) modelisin. Görevin, kullanıcının sorusuna "
        "hızlı, ham ve kapsamlı bir ilk taslak cevap üretmektir. "
        "DİL KURALI (KESİN VE ZORUNLU): Kullanıcının mesajı hangi dildeyse SEN DE O DİLDE "
        "cevap vermelisin. Kullanıcı Türkçe yazdıysa cevabın SADECE VE TAMAMEN Türkçe olmalı "
        "— tek bir İngilizce kelime/cümle bile ekleme (örn. 'Hello!', 'Sure', 'Here is...' gibi "
        "İngilizce ifadelerle BAŞLAMA veya bunları KULLANMA)."
    )
    
    draft_messages = (
        shared_context.to_system_blocks() + 
        [{"role": "system", "content": draft_system_prompt}] + 
        conversation_messages
    )

    sentence_buffer = SentenceBuffer()
    
    # Usta modelin değerlendirme görevlerini takip edeceğimiz liste
    pending_evaluations: List[asyncio.Task] = []
    
    # O ana kadar biriken (onaylanmış/düzeltilmiş) nihai cümleler listesi.
    # Bu liste, usta modele her yeni cümlede bağlam sağlamak için kullanılır.
    sentences_so_far: List[str] = []
    
    # Çırağın ürettiği ham cümlelerin sırasını korumak için aday cümleler listesi
    candidate_sentences: List[str] = []

    # Çırak modelin stream akışını başlatıyoruz
    #
    # KALİTE DÜZELTMESİ (canlı site testinde bulundu — cevabın erken/yarım kesilmesi):
    # max_tokens önceden 1024 idi. Açık uçlu sorularda (örn. "haber başlıklarından birkaçını
    # ver") 6-7 madde + kısa açıklamalar 1024 token'ı kolayca aşabiliyor, model tam bu sınıra
    # ulaştığında (finish_reason="length") çıktı YARIDA/BİR CÜMLE ORTASINDA kesiliyordu ve bu
    # sessizce oluyordu (hiçbir loglama/telafi yoktu) — SentenceBuffer.flush() da bu yarım
    # kalan parçayı normal bir "cümle" gibi işleme aldığı için sonuç "yarım kesilmiş liste"
    # izlenimi veriyordu. 1024 -> 2048 yükseltildi (token-stream mimarisindeki draft çağrısıyla
    # da tutarlı hale getirildi, bkz. collab_orchestrator.py). Ayrıca finish_reason artık
    # yakalanıp logluyor (bkz. aşağıdaki stream okuma döngüsü) — ileride yine "length" ile
    # kesilirse bu artık GÖRÜNÜR olacak, sessizce geçilmeyecek.
    endpoint = f"{draft_url}/v1/chat/completions"
    payload = {
        "model": draft_model_name,
        "messages": draft_messages,
        "stream": True,
        "temperature": 0.6,
        "max_tokens": 2048,
        "repeat_penalty": REPEAT_PENALTY,
        "repeat_last_n": REPEAT_LAST_N
    }

    draft_text = ""
    draft_start_time = time.monotonic()
    draft_finish_reason: Optional[str] = None

    try:
        async with client.stream("POST", endpoint, json=payload, timeout=120.0) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Çırak model sunucusu hata döndürdü: {response.status_code}")

            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                
                try:
                    parsed = json.loads(data_str)
                    choice = parsed["choices"][0]
                    content = choice["delta"].get("content", "")
                    # finish_reason genelde sadece son chunk'ta dolu gelir (örn. "stop" veya
                    # "length"). "length" ise model max_tokens sınırına ulaştığı için kesildi
                    # demektir — bunu yakalayıp aşağıda logluyoruz (bkz. yukarıdaki not).
                    if choice.get("finish_reason"):
                        draft_finish_reason = choice["finish_reason"]
                    if content:
                        draft_text += content
                        
                        # Chunk'ı tampona besleyip tamamlanmış cümleleri alıyoruz
                        new_sentences = sentence_buffer.feed(content)
                        for sentence in new_sentences:
                            candidate_sentences.append(sentence)
                            
                            # Cümle tamamlandığı an usta modelin değerlendirme görevini
                            # arka planda (asenkron) başlatıyoruz. Çırak akışı BLOKE EDİLMEZ.
                            task = asyncio.create_task(
                                relay_sentence_to_usta(
                                    client=client,
                                    usta_url=usta_url,
                                    usta_model_name=usta_model_name,
                                    shared_context=shared_context,
                                    sentences_so_far=list(sentences_so_far), # o anki kopyası
                                    candidate_sentence=sentence
                                )
                            )
                            pending_evaluations.append(task)
                            
                            # Bağlam listesine şimdilik adayı ekliyoruz (usta düzeltirse güncellenecek)
                            sentences_so_far.append(sentence)
                            
                except Exception:
                    continue

        # Akış bittiğinde tamponda kalan son parçayı da işleme alıyoruz
        remaining = sentence_buffer.flush()
        if remaining:
            candidate_sentences.append(remaining)
            task = asyncio.create_task(
                relay_sentence_to_usta(
                    client=client,
                    usta_url=usta_url,
                    usta_model_name=usta_model_name,
                    shared_context=shared_context,
                    sentences_so_far=list(sentences_so_far),
                    candidate_sentence=remaining
                )
            )
            pending_evaluations.append(task)
            sentences_so_far.append(remaining)

    except Exception as e:
        logger.error(f"Çırak akışı sırasında hata: {e}")
        # Bekleyen görevleri iptal edip, sarkan (dangling) task / "exception never retrieved"
        # uyarılarını önlemek için iptal edilen görevlerin de tamamlanmasını bekliyoruz
        # (collab_orchestrator.py'deki run_draft_critique_round ile aynı savunmacı desen).
        for t in pending_evaluations:
            t.cancel()
        for t in pending_evaluations:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        raise e

    draft_duration = time.monotonic() - draft_start_time
    if draft_finish_reason == "length":
        logger.warning(
            f"Çırak akışı max_tokens (2048) sınırına ulaştığı için KESİLDİ (finish_reason=length) "
            f"— cevap eksik/yarım kalmış olabilir. Sorgu çok kapsamlıysa max_tokens'ın daha da "
            f"yükseltilmesi gerekebilir."
        )
    logger.info(
        f"Çırak akışı bitti. Toplam {len(candidate_sentences)} cümle üretildi "
        f"(finish_reason={draft_finish_reason or 'bilinmiyor'}). Usta değerlendirmeleri bekleniyor..."
    )

    # Tüm usta değerlendirme görevlerinin tamamlanmasını bekliyoruz
    eval_start_time = time.monotonic()
    eval_results = await asyncio.gather(*pending_evaluations, return_exceptions=True)
    eval_duration = time.monotonic() - eval_start_time

    # Sonuçları birleştirip nihai metni oluşturuyoruz
    final_sentences = []
    rejected_count = 0
    sentence_details = []

    for i, res in enumerate(eval_results):
        orig_sentence = candidate_sentences[i]
        
        if isinstance(res, Exception):
            logger.error(f"Cümle {i} değerlendirilirken hata oluştu: {res}")
            final_sentences.append(orig_sentence)
            sentence_details.append({
                "original": orig_sentence,
                "final": orig_sentence,
                "accepted": True,
                "error": str(res)
            })
        else:
            final_sentences.append(res["final_sentence"])
            if not res["accepted"]:
                rejected_count += 1
            sentence_details.append({
                "original": orig_sentence,
                "final": res["final_sentence"],
                "accepted": res["accepted"],
                "raw_usta": res["raw_usta_output"]
            })

    # NOT: Usta, tekrarlayan bir cümleyi tespit ettiğinde boş string ("") döndürür (bkz.
    # relay_sentence_to_usta'daki tekrar kontrolü guardrail'i) — bu cümleyi burada filtreleyip
    # birleştiriyoruz, aksi halde fazladan boşluklar nihai metinde görünür hale gelirdi.
    assembled_text = " ".join(s for s in final_sentences if s.strip())
    total_sentences = len(candidate_sentences)
    rejection_ratio = (rejected_count / total_sentences) if total_sentences > 0 else 0.0

    total_duration = time.monotonic() - round_start
    logger.info(
        f"Cümle-Bazlı Tur Tamamlandı. Toplam Cümle: {total_sentences} | "
        f"Reddedilen/Düzeltilen: {rejected_count} (Ratio: {rejection_ratio:.2f}) | "
        f"Toplam Süre: {total_duration:.3f}sn (Çırak: {draft_duration:.3f}sn, Usta Toplam Bekleme: {eval_duration:.3f}sn)"
    )

    return {
        "draft_text": draft_text,
        "assembled_text": assembled_text,
        "sentence_results": sentence_details,
        "rejection_ratio": rejection_ratio,
        "turn_index": turn_index,
        # Tanı görünürlüğü için: "length" ise çırak modelin max_tokens sınırına ulaştığı ve
        # cevabın kesilmiş olabileceği anlamına gelir (bkz. yukarıdaki draft_finish_reason notu).
        "draft_finish_reason": draft_finish_reason,
        "timing": {
            "draft_seconds": draft_duration,
            "usta_eval_seconds": eval_duration,
            "total_seconds": total_duration
        }
    }


async def run_sentence_relay_pipeline(
    client: httpx.AsyncClient,
    draft_url: str,
    usta_url: str,
    draft_model_name: str,
    usta_model_name: str,
    shared_context: SharedContext,
    conversation_messages: List[Dict[str, str]]
) -> Dict[str, Any]:
    """
    Cümle-Bazlı Sequential Relay boru hattının tamamını yöneten ana giriş noktası.
    
    - Belirlenen MAX_REVISION_TURNS sınırına kadar döngüyü çalıştırır.
    - Eğer usta modelin cümle reddetme oranı (rejection_ratio) belirlenen eşiğin
      altındaysa (HIGH_REJECTION_RATIO_THRESHOLD, %30), taslak başarılı kabul edilir
      ve erken çıkış (early-exit) yapılır.
    - Aksi halde, reddedilen cümlelerin eleştirileri birleştirilerek bir sonraki
      turdaki çırağa girdi olarak beslenir.
    """
    pipeline_start = time.monotonic()
    rounds = []
    approved_early = False
    turns_used = 0
    
    current_conversation = list(conversation_messages)
    final_answer = ""

    for turn in range(1, MAX_REVISION_TURNS + 1):
        turns_used = turn
        
        round_result = await run_sentence_relay_round(
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
        final_answer = round_result["assembled_text"]

        # Erken çıkış kontrolü: reddedilme oranı eşiğin altındaysa onaylanmış kabul edilir
        if round_result["rejection_ratio"] <= HIGH_REJECTION_RATIO_THRESHOLD:
            logger.info(
                f"Cümle reddedilme oranı (%{round_result['rejection_ratio']*100:.1f}) "
                f"eşik değerinin (%{HIGH_REJECTION_RATIO_THRESHOLD*100:.1f}) altında. Erken çıkış yapılıyor."
            )
            approved_early = True
            break
        else:
            if turn < MAX_REVISION_TURNS:
                logger.info("Yüksek reddedilme oranı tespit edildi. Eleştiriler bir sonraki tura aktarılıyor.")
                
                # Reddedilen cümlelerin orijinal ve düzeltilmiş hallerini birleştirip eleştiri özeti oluşturuyoruz
                corrections = []
                for idx, s_res in enumerate(round_result["sentence_results"]):
                    if not s_res["accepted"]:
                        corrections.append(
                            f"- Cümle {idx+1} Hatalı: '{s_res['original']}'\n"
                            f"  Doğrusu/Düzeltmesi: '{s_res['final']}'"
                        )
                
                corrections_summary = "\n\n".join(corrections)
                
                current_conversation.append({
                    "role": "system",
                    "content": (
                        f"Önceki taslakta usta model tarafından reddedilen ve düzeltilen cümleler:\n{corrections_summary}\n\n"
                        "Lütfen bu düzeltmeleri ve bağlamı dikkate alarak, baştan sona daha tutarlı ve "
                        "doğru bir taslak cevap üret."
                    )
                })

    total_pipeline_seconds = time.monotonic() - pipeline_start
    logger.info(
        f"Cümle-Bazlı Boru Hattı Tamamlandı. Kullanılan Tur: {turns_used}/{MAX_REVISION_TURNS} | "
        f"Erken Onay: {approved_early} | Toplam Süre: {total_pipeline_seconds:.3f}sn"
    )

    return {
        "rounds": rounds,
        "final_answer": final_answer,
        "approved_early": approved_early,
        "turns_used": turns_used,
        "total_pipeline_seconds": total_pipeline_seconds,
        "architecture": "sentence_relay"
    }