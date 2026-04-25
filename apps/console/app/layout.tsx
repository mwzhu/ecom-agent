import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Order Exception Console",
  description: "Internal case console for ecommerce operations agents.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
