import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cyber",
  description: "Cyber — AI sohbet arayüzü",
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