import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://cycpep-studio-mdmx.chemz.chatgpt.site"),
  title: "CycPep Studio — Frontend V2 Workbench",
  description: "基于正式 V2 observability contract 的只读环肽科学工作台。",
  openGraph: {
    title: "CycPep Studio",
    description: "只读环肽科学观测工作台",
    images: [{ url: "/og.png", width: 1672, height: 941 }],
  },
  twitter: {
    card: "summary_large_image",
    title: "CycPep Studio",
    description: "只读环肽科学观测工作台",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
