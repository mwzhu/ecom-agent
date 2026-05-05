import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import { clerkProviderEnabled } from "../lib/console-auth";
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
  const body = clerkProviderEnabled() ? <ClerkProvider>{children}</ClerkProvider> : children;
  return (
    <html lang="en">
      <body>{body}</body>
    </html>
  );
}
