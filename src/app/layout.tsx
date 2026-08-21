import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'fal.ai — Alibaba Model Placements',
  description: 'Live dashboard tracking Alibaba AI model positions on fal.ai',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-slate-50 text-slate-900 antialiased min-h-screen">
        {children}
      </body>
    </html>
  )
}
