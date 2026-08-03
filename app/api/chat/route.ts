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

    // 4. ADIM A: RAG (Geçmiş Hafıza) Araması
    // Kullanıcıyı bekletmemek için bu aramaya maksimum 4 saniyelik bir zaman aşımı (timeout) koyuyoruz.
    // Eğer hafıza servisi kapalıysa veya yavaşsa, RAG adımı sessizce atlanır ve sohbet normal şekilde devam eder.
    let ragContext: string | null = null;
    if (latestUserMessage) {
      const ragController = new AbortController();
      const ragTimeoutId = setTimeout(() => ragController.abort(), 4000);

      try {
        const ragResponse = await fetch(`${memoryServiceUrl}/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: latestUserMessage, top_k: 4 }),
          signal: ragController.signal,
        });

        clearTimeout(ragTimeoutId);

        if (ragResponse.ok) {
          const ragData = await ragResponse.json();
          if (ragData.found && ragData.results && ragData.results.length > 0) {
            const memories = ragData.results.map((r: any) => `- ${r.text}`).join("\n");
            ragContext = `Aşağıda geçmiş konuşmalardan hatırlanan, bu soruyla alakalı olabilecek bilgiler var. Eğer alakalıysa kullan, değilse görmezden gel:\n${memories}`;
            console.log("RAG Bağlamı başarıyla enjekte edildi.");
          }
        } else {
          console.warn(`Hafıza servisi hata döndürdü: ${ragResponse.status}`);
        }
      } catch (ragError: any) {
        clearTimeout(ragTimeoutId);
        console.warn("Hafıza araması (RAG) adımı atlandı veya zaman aşımına uğradı:", ragError.message || ragError);
      }
    }

    // 5. ADIM B: Sistem Mesajlarını Hazırla
    // Cyber AI kimliğini tanımlayan ana sistem mesajı
    const identitySystemMessage = {
      role: "system",
      content: "Sen Cyber AI'sın (veya kısaca Cyber). Oracle Cloud üzerinde çalışan, yüksek performanslı ve özel bir yapay zeka asistanısın. Kim olduğun sorulduğunda asla 'Qwen' veya 'Alibaba' olduğunu söyleme; kendini her zaman 'Cyber AI' olarak tanıt. Türkçe konuş."
    };

    // Mesaj listesini oluşturuyoruz: [Kimlik Sistem Mesajı, RAG Sistem Mesajı (varsa), ...Kullanıcı Mesajları]
    const formattedMessages = [identitySystemMessage];
    if (ragContext) {
      formattedMessages.push({
        role: "system",
        content: ragContext
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