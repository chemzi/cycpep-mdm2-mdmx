import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://cycpep-studio-mdmx.chemz.chatgpt.site"),
  title: "CycPep Studio — 双靶环肽设计台",
  description: "从任意靶点输入到七层指标交付的可迁移环肽 Agent 工作台。",
  openGraph: {
    title: "CycPep Studio",
    description: "可迁移环肽 Agent 工作台",
    images: [{ url: "/og.png", width: 1672, height: 941 }],
  },
  twitter: {
    card: "summary_large_image",
    title: "CycPep Studio",
    description: "可迁移环肽 Agent 工作台",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
