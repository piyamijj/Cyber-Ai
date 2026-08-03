import { NextRequest, NextResponse } from "next/server";
import { waitUntil } from "@vercel/functions";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
// Ana sohbet cevabı (120s) + arka planda hafıza kararı için LLM çağrısı (14B model CPU'da
// yavaş olabileceğinden ~150s'ye kadar sürebilir) toplamda fonksiyonun canlı kalması gereken
// süreyi aşabilir. Bu yüzden fonksiyonun maksimum çalışma süresini güvenli bir şekilde artırıyoruz.
export const maxDuration = 290;

/**
 * Cyber AI - Server-Side Proxy API Route (RAG & Akıllı Hafıza Entegrasyonlu)
 * 
 * Neden bu proxy'yi kullanıyoruz?
 * 1. GÜVENLİK: Tarayıcı konsolunda veya ağ isteklerinde Oracle sunucunuzun IP adresi (79.76.63.191) görünmez.
 * 2. MIXED CONTENT ENGELİ: Vercel siteniz HTTPS (güvenli) üzerinden çalışırken, tarayıcılar doğrudan HTTP (güvensiz)
 *    bir IP adresine istek atılmasını engeller (Mixed Content Block). Bu proxy sunucu tarafında (Vercel Serverless)
 *    çalıştığı için bu engeli tamamen aşar.
 * 3. ESNEKLİK: Sunucu adresi veya portu değişirse, kodu değiştirmeden Vercel panelinden LLAMA_SERVER_URL
 *    çevre değişkenini (Environment Variable) güncellemeniz yeterlidir.
 * 4. RAG & HAFIZA: Kullanıcı mesajı geldiğinde önce arka planda hafıza araması (RAG) yapılır, alakalı geçmiş bilgiler
 *    sistem mesajına eklenir. Cevap tamamlandığında ise arka planda (kullanıcıyı bekletmeden) konuşma analiz edilip
 *    önemli bilgiler otomatik olarak hafızaya (Qdrant) kaydedilir.
 */

const DEFAULT_UPSTREAM_URL = "http://79.76.63.191:8082";
const DEFAULT_MEMORY_SERVICE_URL = "http://79.76.63.191:8083";

/**
 * "ÇIRAK-USTA" KARAR MİMARİSİ
 * ---------------------------
 * Eskiden burada basit bir anahtar kelime taraması (isLikelyTimeSensitiveQuery) vardı.
 * Hem isabetsizdi (birçok güncel soruyu kaçırıyor ya da alakasız sorularda gereksiz
 * yere tetikleniyordu) hem de tetiklendiğinde RAG+Tavily sonucu büyük modele (14B, CPU'da
 * yavaş) sistem mesajı olarak enjekte edilse bile büyük model bunu sık sık görmezden gelip
 * "güncel veri veremem" diyerek uydurma cevaplar üretiyordu.
 *
 * Yeni mimaride karar, KÜÇÜK bir modele ("çırak" — qwen2.5-0.5b, ayrı bir llama-server
 * sürecinde, ayrı bir portta, ana modelden bağımsız çalışır) verdiriliyor. Bu model hafıza
 * servisindeki (main.py) /decide endpoint'i üzerinden çağrılıyor; SADECE EVET/HAYIR üretecek
 * şekilde kısıtlanmış, max_tokens düşük tutulmuş, bu yüzden tipik olarak 1-2 saniyede döner.
 * Küçük model ayrı bir süreçte olduğu için ana ("usta") modelle kaynak çekişmesine girmez.
 *
 * HAYIR ise: RAG ve web search TAMAMEN atlanır, istek doğrudan ana modele gider — gecikme
 * neredeyse sıfıra iner.
 * EVET ise: RAG + web search (Tavily) çalıştırılır — web search artık ham kullanıcı mesajı
 * yerine küçük modelin ürettiği OPTİMİZE EDİLMİŞ arama sorgusuyla (searchQuery) yapılır — ve
 * sonuç ana modele GÜÇLÜ ve ZORLAYICI bir sistem talimatıyla iletilir (bkz. aşağıdaki
 * webSearchContext hazırlığı).
 *
 * v3 NOTU: /decide artık düz EVET/HAYIR metni yerine yapılandırılmış JSON döndürüyor:
 * { needs_realtime_info: boolean, search_query: string }. search_query, küçük model
 * tarafından üretilen ve Tavily'ye gönderilecek optimize edilmiş arama ifadesidir (ham
 * kullanıcı mesajından daha kısa/öz olabilir) — bu sayede web araması daha isabetli sonuç verir.
 */

interface DecideResult {
  needsRealtimeInfo: boolean;
  searchQuery: string;
}

// Çırak (küçük) modelin karar servisi başarısız olur/zaman aşımına uğrarsa güvenli tarafta
// kalıyoruz: eskisi gibi RAG+web search çalışsın (needsRealtimeInfo=true varsayılır), ve arama
// sorgusu olarak ham kullanıcı mesajını kullanırız (optimize sorgu üretilemediği için).
async function decideNeedsRealtimeInfo(userMessage: string, memoryServiceUrl: string): Promise<DecideResult> {
  const controller = new AbortController();
  // Çırak model ayrı bir süreçte, düşük max_tokens ile çalıştığı için hızlı olmalı.
  // Yine de ağ/servis gecikmesine karşı makul ama sıkı bir zaman aşımı koyuyoruz.
  const timeoutId = setTimeout(() => controller.abort(), 8000);

  try {
    const response = await fetch(`${memoryServiceUrl}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: userMessage }),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      console.warn(`/decide çağrısı hata döndürdü: ${response.status}. Güvenli taraf: EVET (RAG+web search çalışacak).`);
      return { needsRealtimeInfo: true, searchQuery: userMessage };
    }

    const data = await response.json();
    const needsRealtimeInfo = data.needs_realtime_info !== false; // beklenmeyen alan/tip durumunda da güvenli tarafta kal
    const searchQuery = (typeof data.search_query === "string" && data.search_query.trim().length > 0)
      ? data.search_query.trim()
      : userMessage;

    console.log(`Çırak-usta kararı: ${needsRealtimeInfo ? "EVET (RAG+web search çalışacak)" : "HAYIR (RAG+web search atlanacak)"} | Çırak çıktısı: '${data.raw_output}' | search_query: '${searchQuery}'${data.fallback_used ? " [FALLBACK]" : ""}`);
    return { needsRealtimeInfo, searchQuery };
  } catch (error: any) {
    clearTimeout(timeoutId);
    console.warn("/decide adımı atlandı veya zaman aşımına uğradı, güvenli taraf: EVET (RAG+web search çalışacak):", error.message || error);
    return { needsRealtimeInfo: true, searchQuery: userMessage };
  }
}

export async function POST(req: NextRequest) {
  try {
    // 1. İstek gövdesini ayrıştır ve doğrula
    let body;
    try {
      body = await req.json();
    } catch (e) {
      return NextResponse.json(
        { error: "BAD_REQUEST", message: "Geçersiz JSON gövdesi." },
        { status: 400 }
      );
    }

    const { messages } = body;
    if (!messages || !Array.isArray(messages)) {
      return NextResponse.json(
        { error: "BAD_REQUEST", message: "İstek gövdesinde geçerli bir 'messages' dizisi bulunmalıdır." },
        { status: 400 }
      );
    }

    // 2. Çevre değişkenlerini oku veya varsayılan adresleri kullan
    const upstreamBaseUrl = process.env.LLAMA_SERVER_URL || DEFAULT_UPSTREAM_URL;
    const upstreamEndpoint = `${upstreamBaseUrl}/v1/chat/completions`;
    const memoryServiceUrl = process.env.MEMORY_SERVICE_URL || DEFAULT_MEMORY_SERVICE_URL;

    // 3. En son kullanıcı mesajını bul (RAG araması ve hafıza kaydı için kullanılacak)
    let latestUserMessage: string | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") {
        latestUserMessage = messages[i].content;
        break;
      }
    }

    // 3.5. ADIM: ÇIRAK-USTA KARAR ADIMI
    // RAG ve web search'ü çalıştırıp çalıştırmayacağımıza küçük ("çırak") model karar verir.
    // HAYIR ise aşağıdaki iki blok (RAG + web search) TAMAMEN atlanır — istek doğrudan ana
    // modele gider, gecikme neredeyse sıfıra iner. EVET ise ikisi de normal şekilde çalışır ve
    // web search, küçük modelin ürettiği optimize edilmiş searchQuery ile yapılır.
    let needsRealtimeInfo = false;
    let searchQuery: string = latestUserMessage || "";
    if (latestUserMessage) {
      const decideResult = await decideNeedsRealtimeInfo(latestUserMessage, memoryServiceUrl);
      needsRealtimeInfo = decideResult.needsRealtimeInfo;
      searchQuery = decideResult.searchQuery;
    }

    // 4. ADIM A: RAG (Geçmiş Hafıza) Araması
    // Kullanıcıyı bekletmemek için bu aramaya maksimum 4 saniyelik bir zaman aşımı (timeout) koyuyoruz.
    // Eğer hafıza servisi kapalıysa veya yavaşsa, RAG adımı sessizce atlanır ve sohbet normal şekilde devam eder.
    let ragContext: string | null = null;
    if (needsRealtimeInfo && latestUserMessage) {
      const ragController = new AbortController();
      const ragTimeoutId = setTimeout(() => ragController.abort(), 4000);

      try {
        // NOT: top_k 4'ten 2'ye düşürüldü — büyük modele giden prompt'un boyutunu küçültüp
        // (14B model CPU'da prompt işleme süresi token sayısıyla doğrudan orantılı) toplam
        // gecikmeyi azaltmak için. RAG zaten sadece "alakalıysa kullan" diyen yumuşak bir ipucu;
        // 2 sonuç çoğu durumda yeterli bağlamı sağlar.
        const ragResponse = await fetch(`${memoryServiceUrl}/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: latestUserMessage, top_k: 2 }),
          signal: ragController.signal,
        });

        clearTimeout(ragTimeoutId);

        if (ragResponse.ok) {
          const ragData = await ragResponse.json();
          if (ragData.found && ragData.results && ragData.results.length > 0) {
            const memories = ragData.results.map((r: any) => `- ${r.text}`).join("\n");
            // NOT: Metin kısaltıldı (önceki sürüm daha uzun bir açıklama cümlesi kullanıyordu) —
            // prompt boyutunu küçültmek için, aynı anlamı daha az token ile veriyoruz.
            ragContext = `Geçmiş hafızadan alakalı olabilecek notlar (alakalıysa kullan, değilse yoksay):\n${memories}`;
            console.log("RAG Bağlamı başarıyla enjekte edildi.");
          }
        } else {
          console.warn(`Hafıza servisi hata döndürdü: ${ragResponse.status}`);
        }
      } catch (ragError: any) {
        clearTimeout(ragTimeoutId);
        console.warn("Hafıza araması (RAG) adımı atlandı veya zaman aşımına uğradı:", ragError.message || ragError);
      }
    } else if (!needsRealtimeInfo) {
      console.log("Çırak-usta kararı HAYIR: RAG adımı atlandı.");
    }

    // 4.5. ADIM A2: Web Araması (Güncel/Zamana-Duyarlı Sorular İçin)
    // Küçük model (çırak) bu sorunun güncel/gerçek-zamanlı bilgi gerektirdiğine karar verdiyse
    // (needsRealtimeInfo === true), gerçek zamanlı bir web araması yapıyoruz. Bu sonuçlar SADECE
    // bu cevap için kullanılır — hafızaya (Qdrant'a) KESİNLİKLE kaydedilmez, çünkü güncel bilgi
    // zamanla bayatlar ve kalıcı olarak saklanırsa ileride yanıltıcı olur. Kullanıcıyı bekletmemek
    // için kısa bir zaman aşımı koyuyoruz; başarısız olursa sessizce atlanır, sohbet normal devam eder.
    let webSearchContext: string | null = null;
    const WEB_SEARCH_ENABLED = true;
    if (WEB_SEARCH_ENABLED && needsRealtimeInfo && latestUserMessage) {
      const webSearchController = new AbortController();
      const webSearchTimeoutId = setTimeout(() => webSearchController.abort(), 6000);

      try {
        // NOT: Artık ham latestUserMessage yerine, küçük modelin ürettiği optimize edilmiş
        // searchQuery gönderiliyor (ör. "bugün dolar kuru kaç, güncel durum ne?" yerine
        // "güncel dolar kuru") — bu, Tavily'den daha isabetli/alakalı sonuç dönmesini sağlar.
        // max_results 4'ten 2'ye düşürüldü — büyük modele giden prompt boyutunu küçültüp
        // (14B model CPU'da prompt işleme süresi token sayısıyla doğrudan orantılı) toplam
        // gecikmeyi azaltmak için; 2 net/güvenilir sonuç genelde yeterlidir.
        const webSearchResponse = await fetch(`${memoryServiceUrl}/web_search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: searchQuery, max_results: 2 }),
          signal: webSearchController.signal,
        });

        clearTimeout(webSearchTimeoutId);

        if (webSearchResponse.ok) {
          const webSearchData = await webSearchResponse.json();
          if (webSearchData.found && webSearchData.results && webSearchData.results.length > 0) {
            // GÜVENLİK KATMANI (savunma derinliği): Hafıza servisi zaten sıkı doğrulama yapıyor,
            // ama burada da her sonucun makul bir başlık/özet içerdiğini ve aşırı uzun/şüpheli
            // olmadığını kontrol ediyoruz. Herhangi bir sonuç bu kritere uymuyorsa TÜM web arama
            // bağlamını atlıyoruz (kısmi/bozuk veriyle modele gitmektense hiç gitmemesi daha güvenli).
            const validResults = webSearchData.results.filter((r: any) =>
              typeof r.title === "string" && typeof r.snippet === "string" &&
              r.title.trim().length > 0 && r.snippet.trim().length > 0 &&
              r.title.length <= 300 && r.snippet.length <= 350
            );

            if (validResults.length > 0 && validResults.length === webSearchData.results.length) {
              const webResults = validResults
                .map((r: any) => `- ${r.title}: ${r.snippet}`)
                .join("\n");
              // ÖNEMLİ: Bu talimat kasıtlı olarak sert ve zorlayıcı — büyük model önceden bu tür
              // sonuçları görmezden gelip "güncel veri sağlayamam" diyerek uydurma rakamlar
              // üretiyordu. v4'te aynı katılıkta ama DAHA ÖZ bir ifadeyle yazıldı (token tasarrufu
              // için — 14B model CPU'da prompt işleme süresi token sayısıyla orantılı).
              webSearchContext =
                "GERÇEK ZAMANLI GÜNCEL WEB ARAMA SONUÇLARI (MUTLAKA KULLAN, ASLA 'güncel veri " +
                "sağlayamam' deme, ASLA rakam/tarih uydurma — SADECE aşağıdaki bilgiyi kullan):\n" +
                webResults;
              console.log("Web arama bağlamı başarıyla enjekte edildi.");
            } else {
              console.warn("Web arama sonuçlarından bazıları doğrulamayı geçemedi, güvenlik için tüm bağlam atlandı.");
            }
          }
        } else {
          console.warn(`Web arama servisi hata döndürdü: ${webSearchResponse.status}`);
        }
      } catch (webSearchError: any) {
        clearTimeout(webSearchTimeoutId);
        console.warn("Web arama adımı atlandı veya zaman aşımına uğradı:", webSearchError.message || webSearchError);
      }
    }

    // 5. ADIM B: Sistem Mesajlarını Hazırla
    // Cyber AI kimliğini tanımlayan ana sistem mesajı
    const identitySystemMessage = {
      role: "system",
      // NOT: Metin öz tutuldu (token tasarrufu) — bu mesaj HER istekte gönderildiği için
      // (RAG/web search olsun olmasın) boyutu genel gecikmeyi doğrudan etkiler.
      content: "Sen Cyber AI'sın (kısaca Cyber), Oracle Cloud üzerinde çalışan özel bir yapay zeka asistanısın. Asla 'Qwen'/'Alibaba' olduğunu söyleme, hep 'Cyber AI' de. Türkçe konuş. Ayrı bir sistem mesajıyla güncel web arama sonucu verilmişse bunu gerçek/geçerli kabul et, 'güncel veriye erişimim yok' deme — bu kısıtlama SADECE web arama sonucu verilmediğinde geçerlidir."
    };

    // Mesaj listesini oluşturuyoruz: [Kimlik Sistem Mesajı, RAG Sistem Mesajı (varsa),
    // Web Arama Sistem Mesajı (varsa), ...Kullanıcı Mesajları]
    const formattedMessages = [identitySystemMessage];
    if (ragContext) {
      formattedMessages.push({
        role: "system",
        content: ragContext
      });
    }
    if (webSearchContext) {
      formattedMessages.push({
        role: "system",
        content: webSearchContext
      });
    }
    formattedMessages.push(...messages);

    // 6. ADIM C: llama.cpp Sunucusuna İstek Gönder
    // Bağlantı için 120 saniyelik zaman aşımı tanımlıyoruz.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 120000);

    try {
      const upstreamResponse = await fetch(upstreamEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "models/qwen2.5-14b.gguf",
          messages: formattedMessages,
          stream: true,
          temperature: 0.7,
          max_tokens: 8192,
        }),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!upstreamResponse.ok) {
        let errorText = "";
        try {
          errorText = await upstreamResponse.text();
        } catch (_) {}

        return NextResponse.json(
          {
            error: "UPSTREAM_ERROR",
            status: upstreamResponse.status,
            message: `Oracle sunucusu hata döndürdü: ${errorText || upstreamResponse.statusText}`,
          },
          { status: upstreamResponse.status }
        );
      }

      // 7. ADIM D: Akış (Stream) Yanıtını İlet ve Arka Planda Hafızaya Kaydet
      // Tarayıcıya veriyi gecikmesiz (real-time) iletirken, aynı zamanda asistanın ürettiği tüm cevabı
      // arka planda biriktiriyoruz. Akış bittiğinde bu cevabı hafıza servisine gönderip analiz ettireceğiz.
      if (upstreamResponse.body) {
        const decoder = new TextDecoder();
        let fullAssistantText = "";
        let sseBuffer = "";

        // ÖNEMLİ DÜZELTME: waitUntil()'i stream'in flush() callback'i İÇİNDEN çağırmak yerine,
        // burada (ana fonksiyon gövdesinde, senkron olarak) çağırıyoruz. Bu, waitUntil()'in
        // Vercel'in çalışma ortamıyla doğru şekilde entegre olacağını garanti eder — bir stream
        // callback'inin içinden (asenkron bir bağlamdan) çağırmanın bazı ortamlarda beklenmedik
        // davranışlara (örn. isteğin bitmesini gereksiz yere beklemesi) yol açma ihtimalini ortadan kaldırır.
        // Bunun için flush() sadece bir "resolver" fonksiyonunu tetikliyor; asıl waitUntil() çağrısı
        // ana akışta, stream oluşturulur oluşturulmaz yapılıyor.
        let resolveStreamDone: (text: string) => void;
        const streamDonePromise = new Promise<string>((resolve) => {
          resolveStreamDone = resolve;
        });

        const transformStream = new TransformStream({
          transform(chunk, controller) {
            // Gelen veriyi hiç bekletmeden doğrudan tarayıcıya (istemciye) yönlendiriyoruz
            controller.enqueue(chunk);

            // Aynı zamanda arka planda metni biriktirmek için çözümlüyoruz
            const text = decoder.decode(chunk, { stream: true });
            sseBuffer += text;
            const lines = sseBuffer.split("\n");
            sseBuffer = lines.pop() || ""; // Tamamlanmamış son satırı bir sonraki chunk için sakla

            for (const line of lines) {
              const trimmed = line.trim();
              if (trimmed.startsWith("data: ")) {
                const dataStr = trimmed.slice(6);
                if (dataStr === "[DONE]") continue;
                try {
                  const parsed = JSON.parse(dataStr);
                  const content = parsed.choices?.[0]?.delta?.content || "";
                  if (content) {
                    fullAssistantText += content;
                  }
                } catch (_) {
                  // Kısmi veya bozuk JSON satırlarını yoksay
                }
              }
            }
          },
          flush() {
            // Akış tamamen bittiğinde sadece topladığımız metni "resolve" ediyoruz.
            // Burada BAŞKA HİÇBİR ŞEY yapmıyoruz (fetch çağrısı, waitUntil çağrısı vb.) —
            // bu callback'in tek işi, biriken metni dışarıdaki (senkron bağlamdaki) koda iletmek.
            // Bu sayede flush() anında ve kesin olarak döner, stream'in kapanışını hiçbir şekilde geciktirmez.
            resolveStreamDone(fullAssistantText);
          }
        });

        // Upstream akışını transform süzgecinden geçirip istemciye dönüyoruz
        const transformedReadable = upstreamResponse.body.pipeThrough(transformStream);

        // GEÇİCİ OLARAK DEVRE DIŞI: /remember çağrısı, hafızaya değer mi diye karar vermek için
        // AYNI llama.cpp sunucusuna (aynı CPU, aynı model) ikinci bir istek atıyordu. Bu, ana
        // sohbet isteğiyle kaynak çekişmesine giriyor ve normal mesajların bile (örn. "merhaba")
        // dakikalarca gecikmesine yol açıyordu (gözlemlenen: httpcore.ReadTimeout ~126s sonra).
        // Kalıcı çözüm (ayrı bir slot/model ile karar verdirme) kurulana kadar bu adım tamamen
        // atlanıyor — hafıza kaydı durur ama sohbet hızı her zaman korunur.
        const MEMORY_SAVE_ENABLED = false;
        if (MEMORY_SAVE_ENABLED) {
          // waitUntil()'i burada, ANA FONKSİYON GÖVDESİNDE (senkron bağlamda) çağırıyoruz.
          // Bu Promise, stream tamamen bitene (flush() çalışana) kadar bekler, SONRA hafıza
          // kaydı isteğini atar — ama bu bekleme tamamen arka planda gerçekleşir, `return`
          // ile tarayıcıya döndürülen Response nesnesini hiçbir şekilde bloklamaz.
          waitUntil(
            streamDonePromise.then((finalText) => {
              if (latestUserMessage && finalText.trim().length > 0) {
                return saveToMemoryFireAndForget(latestUserMessage, finalText, memoryServiceUrl);
              }
            })
          );
        }

        return new Response(transformedReadable, {
          status: 200,
          headers: {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
          },
        });
      } else {
        return NextResponse.json(
          { error: "EMPTY_RESPONSE", message: "Oracle sunucusundan boş bir yanıt gövdesi alındı." },
          { status: 502 }
        );
      }

    } catch (fetchError: any) {
      clearTimeout(timeoutId);
      
      const isTimeout = fetchError.name === "AbortError";
      return NextResponse.json(
        {
          error: "UPSTREAM_UNREACHABLE",
          message: isTimeout 
            ? "Oracle sunucusuna bağlanırken zaman aşımı oluştu (120s)." 
            : "Oracle sunucusuna (79.76.63.191:8082) ulaşılamadı. Sunucu kapalı olabilir, port kapalı olabilir veya güvenlik duvarı engelliyor olabilir.",
          details: fetchError.message || String(fetchError),
        },
        { status: 502 }
      );
    }

  } catch (globalError: any) {
    return NextResponse.json(
      { error: "INTERNAL_SERVER_ERROR", message: "Sunucu tarafında beklenmeyen bir hata oluştu.", details: globalError.message },
      { status: 500 }
    );
  }
}

/**
 * Arka Planda Hafıza Kaydı Yapan Yardımcı Fonksiyon (Fire-and-Forget)
 * Bu fonksiyon çağrıldığında ana akış (POST) sonlanır ve tarayıcı cevabı almaya devam eder.
 * Bu işlem tamamen arka planda, kullanıcıyı bekletmeden çalışır.
 */
async function saveToMemoryFireAndForget(userMessage: string, assistantMessage: string, memoryServiceUrl: string) {
  const controller = new AbortController();
  // Hafıza servisi, kaydetmeye değip değmediğine karar vermek için 14B modele (CPU üzerinde,
  // yavaş olabilir) ayrı bir istek atıyor. Bu yüzden burada da cömert bir süre tanıyoruz.
  const timeoutId = setTimeout(() => controller.abort(), 180000);

  try {
    console.log("Arka planda hafıza analizi başlatılıyor...");
    const response = await fetch(`${memoryServiceUrl}/remember`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_message: userMessage,
        assistant_message: assistantMessage
      }),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (response.ok) {
      const data = await response.json();
      if (data.saved) {
        console.log(`[Hafıza Kaydedildi] Özet: "${data.saved_text}"`);
      } else {
        console.log(`[Hafıza Atlandı] Gerekçe: ${data.reason}`);
      }
    } else {
      console.error(`Hafıza kaydı servisi hata döndürdü: ${response.status}`);
    }
  } catch (error: any) {
    clearTimeout(timeoutId);
    console.error("Arka planda hafıza kaydı yapılırken hata oluştu:", error.message || error);
  }
}