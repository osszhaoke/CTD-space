import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CTDS · 空间与微环境看板",
  description: "面向空间空气与床微环境的授权上下文数据底座看板。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-theme="dark">
      <body>{children}</body>
    </html>
  );
}
