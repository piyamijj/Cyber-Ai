import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Çırak-Usta (Draft-Critique) işbirlikçi boru hattı CPU üzerinde 2 tam tur (taslak + eleştiri)
// çalışabileceğinden, Vercel Serverless fonksiyonunun maksimum çalışma süresini cömert tutuyoruz.
export const maxDuration = 290;

/**
 * Cyber AI - Server-Side Proxy API Route (Streaming Çırak-Usta Entegrasyonlu)
 * 
 * YENİ MİMARİ VE ÇALIŞMA PRENSİBİ:
 * --------------------------------
 * Bu proxy artık doğrudan port 8082'deki 14B modeline bağlanmak yerine, Oracle sunucusunda
 * port 8083 üzerinde çalışan hafıza servisinin yeni `/collab_stream` endpoint'ini çağırır.
 * 
 * `/collab_stream` arka planda şu adımları yönetir:
 *   1. Shared Context (Tek Seferlik RAG): Kullanıcı sorusu geldiği ilk anda RAG (Qdrant) ve
 *      gerekirse web araması (Tavily) BİR SEFERDE çekilir ve ortak bir bağlam alanına konur.
 *   2. Streaming Çırak-Usta Döngüsü: Çırak model (0.5B) taslak cevabı üretirken, ürettiği
 *      stream chunk'ları usta modele (14B) asyncio.Queue üzerinden canlı olarak beslenir.
 *   3. Kod Seviyesinde Kesin Sınır: Revizyon döngüsü en fazla 2 tur ile sınırlıdır.
 *   4. Early-Exit (Erken Onay): Usta model 'APPROVAL_OK' onay token'ını ürettiği an döngü kesilir.
 * 
 * BU PROXY'NİN GÖREVİ:
 * -------------------
 * `/collab_stream`'den gelen özel SSE akışını (event: draft, event: critique, event: final)
 * dinlemek ve bunu tarayıcıya OpenAI-uyumlu standart bir chat-completion SSE akışı olarak
 * (`data: {"choices":[{"delta":{"content": "..."}}]}\n\n`) yeniden iletmektir.
 * Bu sayede MEVCUT FRONTEND (`app/page.tsx`) ÜZERİNDE HİÇBİR DEĞİŞİKLİK YAPILMASI GEREKMEZ.
 * 
 * KULLANICI DENEYİMİ (UX) STRATEJİSİ:
 * ----------------------------------
 * 1. Çırak taslağı (`event: draft`) geldiği an, kullanıcının beklememesi için bu taslak metni
 *    tarayıcıya küçük parçalar halinde (simüle edilmiş yazma efektiyle) anında akıtılır.
 * 2. Nihai onaylı cevap (`event: final`) geldiğinde, eğer bu cevap çırağın taslağıyla birebir
 *    aynıysa (yani usta doğrudan onay verdiyse), metnin sonuna hafifçe "✅ onaylandı" notu eklenir.
 * 3. Eğer usta taslağı revize ettiyse, araya şık bir ayraç (`--- \n *Usta model tarafından gözden geçirilmiş...*`)
 *    eklenerek ustanın nihai cevabı da akışa dahil edilir. Böylece kullanıcı hem hızlı taslağı
 *    hem de ustanın titiz düzeltmesini şeffaf bir şekilde görür (metnin aniden değişmesi engellenir).
 * 
 * DÜRÜST SINIRLAMA NOTU (Simüle Edilmiş Yazma Efekti):
 * ---------------------------------------------------
 * Python mikroservisi, her turun taslak ve eleştiri metinlerini bütünsel SSE event'leri olarak
 * iletir (per-token delta olarak değil). Bu yüzden bu Next.js katmanı, gelen tam metin bloklarını
 * tarayıcıya saniyede belirli karakterler (`TYPING_CHUNK_SIZE`) halinde dilimleyerek gönderir.
 * Bu, tarayıcıda pürüzsüz bir "yazılıyor..." efekti yaratır. Gelecekte, Python tarafının da
 * per-token SSE delta üretmesi sağlanarak bu simülasyon gerçek zamanlı akışa dönüştürülebilir (TODO).
 */

const DEFAULT_MEMORY_SERVICE_URL = "http://79.76.63.191:8083";
const memoryServiceUrl = process.env.MEMORY_SERVICE_URL || DEFAULT_MEMORY_SERVICE_URL;

// Simüle edilmiş yazma efekti için her SSE çerçevesinde gönderilecek karakter sayısı
const TYPING_CHUNK_SIZE = 6;

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

    // 2. En son kullanıcı mesajını bul (Boru hattına ana girdi olarak verilecek)
    let latestUserMessage: string | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") {
        latestUserMessage = messages[i].content;
        break;
      }
    }

    if (!latestUserMessage) {
      return NextResponse.json(
        { error: "BAD_REQUEST", message: "Konuşma geçmişinde kullanıcıya ait bir mesaj bulunamadı." },
        { status: 400 }
      );
    }

    // 3. Oracle sunucusundaki /collab_stream endpoint'ine istek at
    const controller = new AbortController();
    // 280 saniyelik zaman aşımı (Vercel'in 290s sınırının hemen altında kalacak şekilde)
    const timeoutId = setTimeout(() => controller.abort(), 280000);

    let response;
    try {
      response = await fetch(`${memoryServiceUrl}/collab_stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: latestUserMessage,
          conversation_history: messages,
        }),
        signal: controller.signal,
      });
    } catch (fetchError: any) {
      clearTimeout(timeoutId);
      const isTimeout = fetchError.name === "AbortError";
      return NextResponse.json(
        {
          error: "UPSTREAM_UNREACHABLE",
          message: isTimeout
            ? "Oracle sunucusundaki işbirlikçi boru hattına bağlanırken zaman aşımı oluştu (280s)."
            : "Oracle sunucusundaki hafıza servisine (8083) ulaşılamadı. Servis kapalı olabilir.",
          details: fetchError.message || String(fetchError),
        },
        { status: 502 }
      );
    }

    clearTimeout(timeoutId);

    if (!response.ok) {
      let errorText = "";
      try {
        errorText = await response.text();
      } catch (_) {}
      return NextResponse.json(
        {
          error: "UPSTREAM_ERROR",
          status: response.status,
          message: `Hafıza servisi hata döndürdü: ${errorText || response.statusText}`,
        },
        { status: response.status }
      );
    }

    if (!response.body) {
      return NextResponse.json(
        { error: "EMPTY_RESPONSE", message: "Hafıza servisinden boş bir yanıt gövdesi alındı." },
        { status: 502 }
      );
    }

    // 4. Tarayıcıya iletilecek OpenAI-uyumlu ReadableStream'i inşa et
    const upstreamReader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    const encoder = new TextEncoder();

    let sseBuffer = "";
    let currentEventName = "";
    let lastDraftText = "";

    const readableStream = new ReadableStream({
      async start(streamController) {
        // OpenAI formatında SSE çerçevesi gönderen yardımcı fonksiyon
        const sendDelta = (content: string) => {
          const payload = {
            choices: [
              {
                delta: { content },
              },
            ],
          };
          streamController.enqueue(
            encoder.encode(`data: ${JSON.stringify(payload)}\n\n`)
          );
        };

        // Metni simüle edilmiş yazma efektiyle (küçük dilimler halinde) akıtan yardımcı fonksiyon
        const streamTextWithTypingEffect = async (text: string) => {
          let offset = 0;
          while (offset < text.length) {
            const chunk = text.slice(offset, offset + TYPING_CHUNK_SIZE);
            sendDelta(chunk);
            offset += TYPING_CHUNK_SIZE;
            // Pürüzsüz bir akış hissi için çok kısa bir bekleme (15ms) ekliyoruz
            await new Promise((resolve) => setTimeout(resolve, 15));
          }
        };

        try {
          while (true) {
            const { value, done } = await upstreamReader.read();
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split("\n");
            sseBuffer = lines.pop() || ""; // Tamamlanmamış son satırı sonraki chunk için sakla

            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed) continue;

              if (trimmed.startsWith("event: ")) {
                currentEventName = trimmed.slice(7).trim();
              } else if (trimmed.startsWith("data: ")) {
                const dataStr = trimmed.slice(6).trim();
                let parsedData;
                try {
                  parsedData = JSON.parse(dataStr);
                } catch (e) {
                  // Bozuk JSON satırlarını yoksay
                  continue;
                }

                // Gelen event tipine göre aksiyon alıyoruz
                if (currentEventName === "shared_context_ready") {
                  console.log(
                    `[Proxy] Shared Context Hazır. RAG: ${parsedData.rag_used}, Web: ${parsedData.web_used}`
                  );
                } else if (currentEventName === "draft") {
                  // Çırağın ürettiği taslak metni
                  const draftText = parsedData.text || "";
                  lastDraftText = draftText;
                  
                  if (draftText) {
                    console.log(`[Proxy] Çırak taslağı akıtılıyor (${draftText.length} karakter)...`);
                    await streamTextWithTypingEffect(draftText);
                  }
                } else if (currentEventName === "critique") {
                  console.log(
                    `[Proxy] Usta Değerlendirmesi (Tur: ${parsedData.turn_index}) | Onay: ${parsedData.approved} | Süre: ${parsedData.timing?.total_seconds?.toFixed(2)}sn`
                  );
                } else if (currentEventName === "final") {
                  // Nihai onaylı cevap metni
                  const finalAnswer = parsedData.text || "";
                  
                  if (finalAnswer) {
                    // Eğer nihai cevap çırağın taslağıyla aynıysa (Usta doğrudan onay verdiyse)
                    if (finalAnswer.trim() === lastDraftText.trim()) {
                      console.log("[Proxy] Taslak usta tarafından doğrudan onaylandı.");
                      await streamTextWithTypingEffect(
                        "\n\n✅ *(Usta modeli taslağı onayladı — değişiklik gerekmedi)*"
                      );
                    } else {
                      // Eğer usta taslağı revize ettiyse, araya şık bir ayraç koyup nihai cevabı akıtıyoruz
                      console.log("[Proxy] Usta taslağı revize etti, nihai cevap akıtılıyor...");
                      const separator =
                        "\n\n---\n*(Usta model tarafından gözden geçirilmiş nihai cevap:)*\n\n";
                      await streamTextWithTypingEffect(separator);
                      await streamTextWithTypingEffect(finalAnswer);
                    }
                  }
                } else if (currentEventName === "error") {
                  console.error(`[Proxy] Sunucu tarafında hata: ${parsedData.message}`);
                  await streamTextWithTypingEffect(
                    `\n\n⚠️ **Hata (Boru Hattı):** ${parsedData.message}`
                  );
                }

                // Event adını sıfırla (SSE protokolü gereği sonraki satır yeni bir event olabilir)
                currentEventName = "";
              }
            }
          }

          // Akış başarıyla tamamlandı
          streamController.enqueue(encoder.encode("data: [DONE]\n\n"));
          streamController.close();
        } catch (streamError: any) {
          console.error("[Proxy] Stream okuma sırasında hata:", streamError);
          sendDelta(`\n\n⚠️ **Hata (Stream):** ${streamError.message || "Akış kesildi."}`);
          streamController.enqueue(encoder.encode("data: [DONE]\n\n"));
          streamController.close();
        }
      },
    });

    return new Response(readableStream, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
      },
    });

  } catch (globalError: any) {
    console.error("[Proxy] Global hata:", globalError);
    return NextResponse.json(
      {
        error: "INTERNAL_SERVER_ERROR",
        message: "Sunucu tarafında beklenmeyen bir hata oluştu.",
        details: globalError.message,
      },
      { status: 500 }
    );
  }
}