import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cyber",
  description: "Cyber — AI sohbet arayüzü",
};

// KALİTE DÜZELTMESİ (canlı site mobil testinde bulundu): Bu dosyada viewport meta tag'i
// HİÇ TANIMLI DEĞİLDİ. Next.js App Router'da <meta name="viewport"> otomatik eklenmez —
// bu export ile açıkça belirtilmesi gerekir. Eksik olduğunda mobil tarayıcılar (özellikle
// Android Chrome) sayfayı varsayılan ~980px'lik bir "masaüstü genişliğinde" render edip
// SONRA küçültür — bu da kullanıcının bildirdiği "masaüstü layout'un küçük ekrana
// sıkıştırılmış hali" görünümünün tam olarak kök nedenidir (mesaj baloncuklarının sağ
// kenara tam oturmaması, genel "mobile-first" hissi vermemesi). Bu export eklenince
// tarayıcı sayfayı gerçek cihaz genişliğinde (device-width) ve 1x ölçekte render eder.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="tr" className="dark">
      <body className="bg-cyber-bg text-cyber-text antialiased font-mono">
        {children}
      </body>
    </html>
  );
}