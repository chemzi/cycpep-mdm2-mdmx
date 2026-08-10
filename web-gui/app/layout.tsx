import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://cycpep-studio-mdmx.chemz.chatgpt.site"),
  applicationName: "CycPep Workbench",
  title: "CycPep Workbench",
  description: "环肽候选、证据、执行与实验溯源科学工作台。",
  icons: { icon: "/favicon.svg" },
  openGraph: {
    title: "CycPep Workbench",
    description: "环肽候选、证据、执行与实验溯源科学工作台。",
  },
  twitter: {
    card: "summary",
    title: "CycPep Workbench",
    description: "环肽候选、证据、执行与实验溯源科学工作台。",
  },
};

export const viewport: Viewport = {
  themeColor: "#eef2f4",
};

const localFontFaces = `
  @font-face {
    font-family: "STIX Two Text";
    src: url("/fonts/stix-two-text/STIXTwoText-Regular.woff2") format("woff2");
    font-style: normal;
    font-weight: 400;
    font-display: swap;
  }
  @font-face {
    font-family: "STIX Two Text";
    src: url("/fonts/stix-two-text/STIXTwoText-SemiBold.woff2") format("woff2");
    font-style: normal;
    font-weight: 600;
    font-display: swap;
  }
  @font-face {
    font-family: "IBM Plex Sans";
    src: url("/fonts/ibm-plex-sans/IBMPlexSans-Regular.woff2") format("woff2");
    font-style: normal;
    font-weight: 400;
    font-display: swap;
  }
  @font-face {
    font-family: "IBM Plex Sans";
    src: url("/fonts/ibm-plex-sans/IBMPlexSans-SemiBold.woff2") format("woff2");
    font-style: normal;
    font-weight: 600;
    font-display: swap;
  }
  @font-face {
    font-family: "IBM Plex Mono";
    src: url("/fonts/ibm-plex-mono/IBMPlexMono-Regular.woff2") format("woff2");
    font-style: normal;
    font-weight: 400;
    font-display: swap;
  }
`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <head>
        <link
          rel="preload"
          href="/fonts/ibm-plex-sans/IBMPlexSans-Regular.woff2"
          as="font"
          type="font/woff2"
          crossOrigin="anonymous"
        />
        <link
          rel="preload"
          href="/fonts/stix-two-text/STIXTwoText-Regular.woff2"
          as="font"
          type="font/woff2"
          crossOrigin="anonymous"
        />
        <style>{localFontFaces}</style>
      </head>
      <body>{children}</body>
    </html>
  );
}
