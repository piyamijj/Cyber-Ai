import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Cyber AI - Server-Side Proxy API Route
 * 
 * Neden bu proxy'yi kullanıyoruz?
 * 1. GÜVENLİK: Tarayıcı konsolunda veya ağ isteklerinde Oracle sunucunuzun IP adresi (79.76.63.191) görünmez.
 * 2. MIXED CONTENT ENGELİ: Vercel siteniz HTTPS (güvenli) üzerinden çalışırken, tarayıcılar doğrudan HTTP (güvensiz)
 *    bir IP adresine istek atılmasını engeller (Mixed Content Block). Bu proxy sunucu tarafında (Vercel Serverless)
 *    çalıştığı için bu engeli tamamen aşar.
 * 3. ESNEKLİK: Sunucu adresi veya portu değişirse, kodu değiştirmeden Vercel panelinden LLAMA_SERVER_URL
 *    çevre değişkenini (Environment Variable) güncellemeniz yeterlidir.
 */

const DEFAULT_UPSTREAM_URL = "http://79.76.63.191:8082";

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

    // 2. Upstream sunucu adresini belirle (Vercel env var veya varsayılan IP)
    const upstreamBaseUrl = process.env.LLAMA_SERVER_URL || DEFAULT_UPSTREAM_URL;
    const upstreamEndpoint = `${upstreamBaseUrl}/v1/chat/completions`;

    // 3. 60 saniyelik bir bağlantı zaman aşımı (timeout) tanımla
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60000);

    try {
      // 4. Sistem Mesajı (Cyber AI Kimliği) ve İstek Yönlendirme
      const systemMessage = {
        role: "system",
        content:
          "Sen Cyber AI'sın (veya kısaca Cyber). Oracle Cloud üzerinde çalışan, yüksek performanslı ve özel bir yapay zeka asistanısın. Kim olduğun sorulduğunda asla 'Qwen' veya 'Alibaba' olduğunu söyleme; kendini her zaman 'Cyber AI' olarak tanıt. Türkçe konuş.",
      };

      // Kullanıcı mesajlarının en başına sistem mesajını enjekte ediyoruz
      const formattedMessages = [systemMessage, ...messages];

      // Oracle sunucusundaki llama.cpp API'sine isteği yönlendir
      const upstreamResponse = await fetch(upstreamEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Gerekirse buraya API anahtarı veya ek başlıklar eklenebilir
        },
        body: JSON.stringify({
          model: "models/qwen2.5-14b.gguf", // Sunucumuzdaki gerçek model id'si ile eşleştiriyoruz (GET /v1/models çıktısına göre)
          messages: formattedMessages,
          stream: true, // Akış (streaming) modunu etkinleştiriyoruz
          temperature: 0.7,
          max_tokens: 2048,
        }),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      // 5. Upstream hata durumlarını yönet
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

      // 6. Başarılı akış (stream) yanıtını doğrudan istemciye (tarayıcıya) yönlendir
      if (upstreamResponse.body) {
        return new Response(upstreamResponse.body, {
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
      
      // Zaman aşımı veya ağ hatası durumunu yakala
      const isTimeout = fetchError.name === "AbortError";
      return NextResponse.json(
        {
          error: "UPSTREAM_UNREACHABLE",
          message: isTimeout 
            ? "Oracle sunucusuna bağlanırken zaman aşımı oluştu (60s)." 
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