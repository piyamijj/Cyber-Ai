import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Çırak-Usta (Draft-Critique) işbirlikçi boru hattı CPU üzerinde 2 tam tur (taslak + eleştiri)
// çalışabileceğinden, Vercel Serverless fonksiyonunun maksimum çalışma süresini cömert tutuyoruz.
export const maxDuration = 290;

/**
 * Cyber AI - Server-Side Proxy API Route (Streaming Çırak-Usta Entegrasyonlu)
 *
 * RESMİ/VARSAYILAN MİMARİ (finalize edildi): CÜMLE-BAZLI SEQUENTIAL RELAY
 * ------------------------------------------------------------------------
 * Bu proxy, Oracle sunucusunda port 8083 üzerinde çalışan hafıza servisinin
 * `/collab_stream_sentence` endpoint'ini çağırır. Bu, kullanıcının onayladığı NİHAİ
 * mimaridir: gerçek ölçümlerde token-stream mimarisine göre ~4.3x daha hızlı, CPU
 * çekişmesi daha düşük ve (repeat_penalty + tekrar-kontrolü guardrail'leri eklendikten
 * sonra) kalite açısından da sağlıklı sonuç veriyor.
 *
 * Eski `/collab_stream` (tam token-stream canlı besleme) endpoint'i backend'de hâlâ
 * mevcut ve çalışır durumda bırakıldı — SADECE referans/gelecekte GPU'lu bir sunucuya
 * geçilirse yeniden değerlendirilmek üzere saklanıyor. Bu proxy ARTIK ONU ÇAĞIRMIYOR.
 *
 * `/collab_stream_sentence` arka planda şu adımları yönetir:
 *   1. Shared Context (Tek Seferlik RAG): Kullanıcı sorusu geldiği ilk anda RAG (Qdrant) ve
 *      gerekirse web araması (Tavily) BİR SEFERDE çekilir ve ortak bir bağlam alanına konur.
 *   2. Cümle-Bazlı Sequential Relay: Çırak model (0.5B) bir CÜMLEYİ tamamladığı an, o cümle
 *      usta modele (14B) değerlendirilmek üzere gönderilir; çırak ise bir sonraki cümleyi
 *      üretmeye ARA VERMEDEN devam eder (usta'nın değerlendirmesi kısa ömürlü/bounded bir
 *      görevdir). Usta reddettiği cümleyi düzeltip "araya sıkıştırır" (splice-in).
 *   3. Kod Seviyesinde Kesin Sınır: Revizyon döngüsü en fazla 2 tur ile sınırlıdır; ayrıca
 *      bir turdaki cümle-red oranı yüksekse (>%30) tam bir revizyon turu tetiklenir.
 *   4. Early-Exit (Erken Onay): Usta tüm cümleleri onayladığında döngü erken kesilir.
 *
 * BU PROXY'NİN GÖREVİ:
 * -------------------
 * `/collab_stream_sentence`, `/collab_stream` ile AYNI temel SSE sözleşmesini kullanır
 * (event: draft, event: critique, event: final, event: error — ek olarak sadece bu
 * mimariye özel bir "sentence_detail" event'i de gönderir, bu proxy onu görmezden gelip
 * atlıyor). Bu proxy bu akışı dinleyip tarayıcıya OpenAI-uyumlu standart bir
 * chat-completion SSE akışı olarak (`data: {"choices":[{"delta":{"content": "..."}}]}\n\n`)
 * yeniden iletir. Bu sayede MEVCUT FRONTEND (`app/page.tsx`) ÜZERİNDE HİÇBİR DEĞİŞİKLİK
 * YAPILMASI GEREKMEZ.
 *
 * KULLANICI DENEYİMİ (UX) STRATEJİSİ (canlı site testinde bulunan ÇİFT CEVAP hatası
 * düzeltildikten sonra güncellendi):
 * -------------------------------------------------------------------------------
 * ÖNCEKİ TASARIM (artık geçerli değil, referans için not düşülüyor): Bu proxy önceden hem
 * `event: draft` (çırağın taslağı) hem `event: final` (usta'nın nihai cevabı) metinlerini
 * ayrı ayrı, ikisini de tarayıcıya akıtıyordu — token-stream mimarisinde bu iki metin
 * genelde birbirinden belirgin şekilde FARKLIYDI (usta tam bir revizyon yapabiliyordu), bu
 * yüzden ikisini ayrı ayrı göstermek anlamlıydı. AMA cümle-bazlı relay mimarisinde (şu anki
 * resmi mimari) draft ve final neredeyse her zaman aynı/çok benzer metindir (düzeltmeler
 * zaten cümle seviyesinde üretim sırasında yapılıyor) — bu yüzden ikisini ayrı ayrı akıtmak
 * kullanıcıya AYNI CEVABI İKİ KEZ göstermeye yol açıyordu.
 *
 * GÜNCEL TASARIM: `event: draft` artık tarayıcıya HİÇ AKITILMIYOR (sadece log/ilerleme takibi
 * için saklanıyor). Kullanıcı SADECE `event: final` içindeki nihai, tek ve temiz metni,
 * simüle edilmiş yazma efektiyle (küçük parçalar halinde) görür. Böylece kullanıcı tek bir
 * cevap görür, cevabın aniden değişmesi veya tekrarlanması söz konusu olmaz.
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

// RESMİ/VARSAYILAN MİMARİ: Cümle-Bazlı Sequential Relay (kullanıcı onaylı nihai karar).
// Token-stream (/collab_stream) backend'de referans olarak saklanıyor ama artık çağrılmıyor.
const COLLAB_ENDPOINT_PATH = "/collab_stream_sentence";

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

    // 3. Oracle sunucusundaki /collab_stream_sentence (resmi/varsayılan mimari) endpoint'ine istek at
    const controller = new AbortController();
    // 280 saniyelik zaman aşımı (Vercel'in 290s sınırının hemen altında kalacak şekilde)
    const timeoutId = setTimeout(() => controller.abort(), 280000);

    let response;
    try {
      response = await fetch(`${memoryServiceUrl}${COLLAB_ENDPOINT_PATH}`, {
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
                //
                // KALİTE DÜZELTMESİ (canlı site testinde bulundu — ÇİFT CEVAP HATASI):
                // Önceki mantık, token-stream mimarisi (/collab_stream) için tasarlanmıştı: orada
                // "draft" (çırağın TEK BÜTÜN taslağı) ile "final" (usta'nın TEK BÜTÜN revizyonu)
                // gerçekten birbirinden farklı, ayrı ayrı gösterilmeye değer iki şeydi. AMA
                // cümle-bazlı relay (/collab_stream_sentence) mimarisinde bu ayrım artık geçerli
                // değil: orada HER TUR için bir "draft" event'i geliyor (o turun çırak taslağı) VE
                // ayrıca bir "final" event'i geliyor (tüm pipeline'ın nihai metni) — ikisi çoğu
                // zaman neredeyse AYNI metindir (splice-in düzeltmeleri zaten üretim sırasında
                // cümle seviyesinde yapılıyor, draft zaten neredeyse nihai halidir). Eski mantık
                // draft'ı TAM olarak akıttıktan SONRA final'i de (birebir aynı olmadığı için,
                // ufak cümle farkları yüzünden) tekrar TAM olarak akıtıyordu — kullanıcı aynı
                // haber listesini/cevabı İKİ KEZ ard arda görüyordu.
                //
                // DÜZELTME: Bu mimaride draft event'lerini artık EKRANA YAZDIRMIYORUZ (sadece
                // konsola log atıyoruz, kullanıcı akışını beklerken "typing" hissi vermek için
                // ilerleme bilgisini saklıyoruz) — EKRANA SADECE "final" event'indeki nihai,
                // tek ve temiz metin akıtılıyor. Böylece kullanıcı TEK BİR CEVAP görür.
                if (currentEventName === "shared_context_ready") {
                  console.log(
                    `[Proxy] Shared Context Hazır. RAG: ${parsedData.rag_used}, Web: ${parsedData.web_used}`
                  );
                } else if (currentEventName === "draft") {
                  // Çırağın ürettiği taslak metni — ARTIK EKRANA AKITILMIYOR (bkz. yukarıdaki not),
                  // sadece log ve "kullanıcı bir şey üretiliyor" sinyali için saklanıyor.
                  const draftText = parsedData.text || "";
                  lastDraftText = draftText;
                  if (draftText) {
                    console.log(`[Proxy] Çırak taslağı alındı (${draftText.length} karakter, ekrana yazılmıyor)...`);
                  }
                } else if (currentEventName === "critique") {
                  console.log(
                    `[Proxy] Usta Değerlendirmesi (Tur: ${parsedData.turn_index}) | Onay: ${parsedData.approved} | Süre: ${parsedData.timing?.total_seconds?.toFixed(2)}sn`
                  );
                } else if (currentEventName === "final") {
                  // Nihai onaylı cevap metni — kullanıcının GÖRECEĞİ TEK metin budur.
                  const finalAnswer = parsedData.text || "";
                  if (finalAnswer) {
                    console.log(`[Proxy] Nihai cevap akıtılıyor (${finalAnswer.length} karakter)...`);
                    await streamTextWithTypingEffect(finalAnswer);
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