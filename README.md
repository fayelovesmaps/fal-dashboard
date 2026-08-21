# fal.ai Promotion Tracker

Automated scanner that monitors how Alibaba AI models (Happy Horse 1.0/1.1, Wan 2.6/2.7) are positioned and promoted across fal.ai surfaces. Produces a visual HTML report showing each model's placement, slot position, and changes since the last scan.

## What it tracks

The scanner visits 9 fal.ai surfaces and records where our models appear:

- **Homepage** — hero section, featured models
- **Explore page** — category grids, hero carousel
- **Search results** — queries for "video", "video generation", "text to video", "image to video", and all models
- **Learn (editorial)** — articles mentioning our models
- **Blog** — blog.fal.ai posts

For each placement found, it captures the model name, section, slot position (e.g. 3rd out of 28), and a screenshot.

## What the report shows

- **Position heatmap** — quick overview of all models across all surfaces
- **Placement cards** — screenshot + slot position for each detection
- **Change indicators** — compared to previous scan:
  - Green ▲N = moved up N slots
  - Red ▼N = moved down N slots
  - Blue NEW = newly appeared
  - Red REMOVED = no longer present

## Requirements

- Python 3.10+
- Node.js 18+ (only needed if using the Next.js dashboard)
- Playwright (`pip install playwright && playwright install chromium`)

## Setup

```bash
cd "/Users/yifeilin/summer-internship-projects/projects/Fal Dashboard/fal-promotion-tracker/fal-dashboard"
npm install
pip install playwright
playwright install chromium
```

## Usage

### Run a scan and generate the report

```bash
npm run scan
```

This does two things:
1. Runs the Playwright scanner against all 9 fal.ai surfaces
2. Generates `scan_report_v2.html` — open this file in your browser to view results

The scan takes about 2-3 minutes (headless Chromium visits each page, waits for hydration, captures screenshots).

### Regenerate the report without re-scanning

```bash
npm run report
```

Useful if you want to rebuild the HTML from existing scan data.

### View the report

```bash
open scan_report_v2.html
```

The HTML file is fully self-contained (screenshots are base64-embedded) — you can share it with anyone via DingTalk, email, etc.

## How change tracking works

Each scan automatically archives the previous `scan_v2.json` as `scan_v2.prev.json` before writing new data. The report builder compares current vs previous using a composite key of (surface, model, section, URL) to detect movements, additions, and removals.

On your first scan there's no previous data, so no change indicators appear. From the second scan onward, you'll see the ▲/▼/NEW/REMOVED badges.

## Project structure

```
fal-dashboard/
├── scripts/
│   ├── fal_scanner_v2.py      # Playwright scanner
│   └── build_report_v2.py     # HTML report generator
├── data/
│   ├── scan_v2.json           # Latest scan results
│   └── scan_v2.prev.json      # Previous scan (auto-archived)
├── public/screenshots_v2/     # Captured screenshots
├── scan_report_v2.html        # Generated report (open in browser)
└── src/app/                   # Next.js dashboard (optional)
    ├── page.tsx               # Dashboard page component
    ├── layout.tsx
    └── globals.css
```

## Optional: Live dashboard

The project also includes a Next.js static dashboard (`src/app/page.tsx`) that reads the same scan data. If you want to host it on Vercel or any static hosting:

```bash
npm run build     # generates static site in out/
```

But for daily use, `npm run scan` + opening the HTML file is the simplest workflow.
