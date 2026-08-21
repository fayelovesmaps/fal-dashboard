"""
fal.ai Promotion Scanner v2
============================
Targeted scan of HH 1.0, HH 1.1, Wan 2.6, Wan 2.7 across public fal.ai surfaces.
Produces scan_v2.json + per-placement screenshots for the report builder.

Design principles (from post-mortem):
  1. All fragile things (selectors, thresholds, timings) live in CONFIG at top of file.
  2. Explicit phase ordering: PROBE -> EXTRACT_DYNAMIC -> FREEZE -> EXTRACT_STATIC -> SCREENSHOT.
     The carousel MUST be extracted before CSS animations are frozen, because slide
     switching depends on transitions.
  3. Self-checks only assert on scraper-broken conditions (RSC hook fired, >=1 model
     link exists, screenshot >5KB, HTTP 200). Never assert on business facts like
     "explore must have N sections" or "carousel must have N slides" — those can
     legitimately change on fal.ai's side.
  4. Screenshots are the source of truth. Nothing goes into JSON as a "placement"
     unless a screenshot >MIN_SCREENSHOT_BYTES was captured for it.
  5. Retries with backoff on transient wait_for_function timeouts.
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeoutError

# ============================================================================
# CONFIG — every fragile thing lives here. Change once, applies everywhere.
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data"
SCREENSHOT_DIR = PROJECT_ROOT / "public" / "screenshots_v2"

def reset_screenshot_dir():
    """
    Clear screenshots_v2/ at the start of each scan so orphans from prior runs
    don't accumulate. These files are scanner-owned outputs (regenerated every
    run) — safe to delete outright, no trash routing needed.
    """
    if SCREENSHOT_DIR.exists():
        for f in SCREENSHOT_DIR.iterdir():
            if f.is_file():
                f.unlink()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CONFIG = {
    # Viewport
    "viewport_width":         1440,
    "viewport_height":         900,
    "device_scale_factor":       2,

    # Timeouts (ms)
    "nav_timeout_ms":        60000,
    "flight_wait_ms":        15000,
    "grid_wait_ms":          15000,
    "click_timeout_ms":       3000,
    "scroll_into_view_ms":    5000,

    # Sleeps (seconds) — post-navigation settling
    "post_nav_settle_s":       4.0,
    "post_grid_settle_s":      1.5,
    "post_scroll_settle_s":    0.4,
    "post_carousel_click_s":   1.5,
    "post_freeze_settle_s":    0.3,
    "cookie_click_settle_s":   0.5,

    # Retries
    "wait_retry_count":          2,
    "wait_retry_backoff_s":    2.0,

    # Card filtering (heuristics that separate real cards from nav links / CTAs)
    "search_min_card_w":       150,
    "search_min_card_h":       100,
    "sections_min_card_w":     100,
    "sections_min_card_h":      50,
    "homepage_min_card_w":      50,
    "homepage_min_card_h":      20,
    "min_card_area":          5000,      # for scrollIntoView target election

    # Grid readiness — MINIMUM number of /models/ links visible before extraction.
    # Set low enough that a legitimately quiet page (e.g. narrow search result)
    # still passes; only meant to catch pre-hydration extraction.
    "min_model_links_ready":     4,

    # Screenshot QA
    "min_screenshot_bytes":   5000,      # <5KB = almost certainly blank/broken

    # Screenshot framing
    "heading_search_range_px": 600,      # how far above card to look for a heading
    "heading_include_gap_px":  400,      # if heading is within this distance, frame both
    "heading_top_margin_px":    30,      # pixels of padding above heading in frame
    "card_only_top_margin_px": 120,      # pixels of padding above card when no heading
    "card_viewport_slack_px":   50,      # tolerance for "card is in viewport"
}

# CSS/XPath selectors — every DOM query used in the scanner. When fal.ai
# restructures their page, this is the ONE place that needs updates.
SELECTORS = {
    "model_link":            'a[href*="/models/"]',
    "learn_article_link":    'a[href^="/learn/"]',
    "blog_article_link":     'a[href]',   # blog.fal.ai post links are relative slugs; filtered by extractor
    "cookie_buttons":        'button:has-text("Opt out"), button:has-text("Accept"), button[aria-label="Close"]',
    "carousel_section":      'section.relative',       # /explore hero section
    "carousel_indicator_min_text_len": 3,               # spans w/ this length are model labels
    "carousel_min_children": 6,                         # must have >=5 slides + 1 indicator div
    "explore_headings":      'h2, h3',
    "homepage_headings":     'h1, h2, h3',
    "model_page_h1":         'h1',
}

# Which surface types show grid/list of models vs. articles. Used for filtering,
# wait conditions, and screenshot logic.
MODEL_SURFACE_TYPES   = {"homepage", "sections", "search"}
ARTICLE_SURFACE_TYPES = {"learn_index", "blog_index"}

# Injected on every page — captures Next.js RSC flight data as it loads.
# Verified fal.ai uses this pattern; if they migrate away we'll detect via self-check.
INIT_SCRIPT = """
window.__intercepted_flight = [];
Object.defineProperty(window, "__next_f", {
    configurable: true,
    set: function(val) {
        this.__next_f_real = val;
        val.forEach(item => window.__intercepted_flight.push(item));
        const origPush = val.push.bind(val);
        val.push = function(...args) {
            window.__intercepted_flight.push(...args);
            return origPush(...args);
        };
    },
    get: function() { return this.__next_f_real; }
});
"""

# ============================================================================
# MODEL DEFINITIONS — what we're tracking. Edit here to add/remove targets.
# ============================================================================

TARGET_MODEL_PATTERNS = {
    # (family_key, url_substring, human_label_prefix)
    "happy-horse-1.1": {"match": lambda h: "happy-horse" in h and "v1.1" in h,        "prefix": "HH 1.1"},
    "happy-horse-1.0": {"match": lambda h: "happy-horse" in h and "v1.1" not in h,    "prefix": "HH 1.0"},
    "wan-2.7":         {"match": lambda h: "wan/v2.7" in h,                            "prefix": "Wan 2.7"},
    "wan-2.6":         {"match": lambda h: "wan/v2.6" in h,                            "prefix": "Wan 2.6"},
}

# Substrings that disqualify a URL even if it matches (LoRA trainers, other versions, etc.)
EXCLUDE_SUBSTRINGS = ["wan-22", "v2.2", "wan-25", "v2.5", "wan-21", "v2.1", "trainer", "lora"]

# Sub-modality detection for human labels
SUBMODAL_MAP = [
    ("reference",       "R2V"),
    ("image-to",        "I2V"),
    ("text-to-video",   "T2V"),
    ("text-to-image",   "T2I"),
    ("video-edit",      "V2V"),
    ("edit",            "Edit"),
]

def is_target_model(href: str) -> bool:
    h = href.lower()
    if h.endswith("/api"):
        return False
    if any(x in h for x in EXCLUDE_SUBSTRINGS):
        return False
    return any(spec["match"](h) for spec in TARGET_MODEL_PATTERNS.values())

def get_model_family(href: str) -> str:
    h = href.lower()
    for family_key, spec in TARGET_MODEL_PATTERNS.items():
        if spec["match"](h):
            return family_key
    return "unknown"

def get_model_label(href: str) -> str:
    h = href.lower()
    family_key = get_model_family(href)
    if family_key == "unknown":
        return href.split("/")[-1]
    prefix = TARGET_MODEL_PATTERNS[family_key]["prefix"]
    for substr, suffix in SUBMODAL_MAP:
        if substr in h:
            return f"{prefix} {suffix}"
    return prefix

# Article-matching (editorial content). Broader than URL matching because titles
# use varied wording ("Happy Horse 1.0", "HappyHorse 1", "Wan 2.7 prompting guide"...).
ARTICLE_PATTERNS = {
    "happy-horse-1.1": {
        "prefix": "HH 1.1",
        "match": lambda text: bool(re.search(r"happy[\s\-]?horse[\s\-]?1[\s\-]?[.\-]?1|happyhorse[\s\-]?1[\s\-]?[.\-]?1|hh[\s\-]?1[.\-]?1", text, re.I)),
    },
    "happy-horse-1.0": {
        "prefix": "HH 1.0",
        # Match "happy horse 1.0" or bare "happy horse" (without 1.1)
        "match": lambda text: bool(re.search(r"happy[\s\-]?horse|happyhorse|hh[\s\-]?1(?![.\-]?1)", text, re.I)) and not bool(re.search(r"1[\s\-]?[.\-]?1|v1[.\-]?1", text, re.I)),
    },
    "wan-2.7": {
        "prefix": "Wan 2.7",
        "match": lambda text: bool(re.search(r"wan[\s\-]?2[\s\-]?[.\-]?7|wan27", text, re.I)),
    },
    "wan-2.6": {
        "prefix": "Wan 2.6",
        "match": lambda text: bool(re.search(r"wan[\s\-]?2[\s\-]?[.\-]?6|wan26", text, re.I)),
    },
}

# Article-context exclusions — don't match if the ONLY mention is these tokens
ARTICLE_EXCLUDE = re.compile(r"wan[\s\-]?2[\s\-]?[.\-]?[125]", re.I)

def is_target_article(href: str, title: str) -> bool:
    combined = f"{href} {title}"
    return any(spec["match"](combined) for spec in ARTICLE_PATTERNS.values())

def get_article_family(href: str, title: str) -> str:
    combined = f"{href} {title}"
    # Check in specificity order (1.1 before 1.0 to avoid catching HH1.1 as HH1.0)
    for family_key in ["happy-horse-1.1", "wan-2.7", "wan-2.6", "happy-horse-1.0"]:
        if ARTICLE_PATTERNS[family_key]["match"](combined):
            return family_key
    return "unknown"

def get_article_label(href: str, title: str) -> str:
    family = get_article_family(href, title)
    if family == "unknown":
        return "Unknown"
    return ARTICLE_PATTERNS[family]["prefix"]

# ============================================================================
# SURFACES — the pages to scan.
# ============================================================================

SURFACES = [
    {"name": "homepage",                 "url": "https://fal.ai/",                                      "type": "homepage"},
    {"name": "explore",                  "url": "https://fal.ai/explore",                               "type": "sections"},
    {"name": "search_all",               "url": "https://fal.ai/explore/search",                        "type": "search"},
    {"name": "search_video",             "url": "https://fal.ai/explore/search?q=video",                "type": "search"},
    {"name": "search_video_generation",  "url": "https://fal.ai/explore/search?q=video+generation",     "type": "search"},
    {"name": "search_text_to_video",     "url": "https://fal.ai/explore/search?q=text+to+video",        "type": "search"},
    {"name": "search_image_to_video",    "url": "https://fal.ai/explore/search?q=image+to+video",       "type": "search"},
    {"name": "learn",                    "url": "https://fal.ai/learn",                                 "type": "learn_index"},
    {"name": "blog",                     "url": "https://blog.fal.ai/",                                 "type": "blog_index"},
]

# ============================================================================
# UTILITIES
# ============================================================================

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")

async def _wait_with_retry(page: Page, js_expr: str, timeout_ms: int, label: str) -> bool:
    """wait_for_function with retries — tolerates transient hydration hiccups."""
    for attempt in range(1, CONFIG["wait_retry_count"] + 1):
        try:
            await page.wait_for_function(js_expr, timeout=timeout_ms)
            return True
        except PWTimeoutError:
            if attempt < CONFIG["wait_retry_count"]:
                print(f"    \u23f1  wait ({label}) timed out, retry {attempt}/{CONFIG['wait_retry_count']-1}")
                await asyncio.sleep(CONFIG["wait_retry_backoff_s"] * attempt)
            else:
                print(f"    \u26a0  wait ({label}) exhausted retries \u2014 proceeding anyway")
    return False

def _screenshot_is_valid(path: Path) -> bool:
    """Screenshot QA: reject blank/broken captures under the size threshold."""
    if not path.exists():
        return False
    return path.stat().st_size >= CONFIG["min_screenshot_bytes"]

# ============================================================================
# SELF-CHECKS — assertions about the SCRAPER, never about fal.ai's content.
# ============================================================================

class SelfCheckResult:
    """Aggregates per-surface scraper health signals. NOT for business facts."""
    def __init__(self, surface_name: str):
        self.surface_name = surface_name
        self.checks: List[Dict] = []

    def add(self, name: str, passed: bool, detail: str = ""):
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        icon = "\u2714" if passed else "\u2718"
        print(f"    [check] {icon} {name}" + (f" \u2014 {detail}" if detail else ""))

    def to_dict(self) -> Dict:
        return {
            "surface": self.surface_name,
            "all_passed": all(c["passed"] for c in self.checks),
            "checks": self.checks,
        }

async def run_scraper_health_checks(page: Page, http_status: Optional[int], surface: Dict) -> SelfCheckResult:
    """
    Verify the SCRAPER is working. Does not assert anything about fal.ai's
    business decisions (# of sections, # of slides, whether results are empty).
    """
    r = SelfCheckResult(surface["name"])

    # 1. HTTP 200 — page actually loaded
    r.add("http_ok", http_status is not None and 200 <= http_status < 400,
          f"status={http_status}")

    # 2. Body has content — not a blank page
    body_len = await page.evaluate("() => (document.body && document.body.textContent || '').length")
    r.add("body_has_content", body_len > 500, f"body chars={body_len}")

    # 3. RSC flight hook fired — only meaningful on Next.js pages (fal.ai/*).
    #    blog.fal.ai is a different stack (Ghost/static) — skip this check there.
    if surface["type"] != "blog_index":
        flight_len = await page.evaluate("() => (window.__intercepted_flight || []).length")
        r.add("rsc_hook_fired", flight_len > 0, f"flight entries={flight_len}")

    # 4. Card/link selector still returns something — proves our extractor won't
    #    find zero elements due to a DOM restructure. Selector depends on surface:
    #    model surfaces need /models/ links, learn/blog surfaces need article links.
    if surface["type"] in MODEL_SURFACE_TYPES:
        link_count = await page.evaluate(
            f"() => document.querySelectorAll('{SELECTORS['model_link']}').length"
        )
        r.add("model_link_selector_valid", link_count >= 1, f"model links found={link_count}")
    elif surface["type"] == "learn_index":
        link_count = await page.evaluate(
            f"() => document.querySelectorAll('{SELECTORS['learn_article_link']}').length"
        )
        r.add("learn_link_selector_valid", link_count >= 1, f"learn links found={link_count}")
    elif surface["type"] == "blog_index":
        # blog.fal.ai posts show up as h2s inside anchors; count h2 to verify feed rendered
        h2_count = await page.evaluate("() => document.querySelectorAll('h2').length")
        r.add("blog_h2_selector_valid", h2_count >= 1, f"h2 headings found={h2_count}")

    return r

# ============================================================================
# EXTRACTORS — per-surface DOM scraping
# ============================================================================

async def extract_sections_surface(page: Page) -> List[Dict]:
    """Extract from /explore — has h2/h3 section headers with card rows."""
    return await page.evaluate(
        """(cfg) => {
            const headings = Array.from(document.querySelectorAll(cfg.explore_headings))
                .filter(h => h.textContent && h.textContent.trim().length < 60)
                .map(h => ({
                    text: h.textContent.trim(),
                    top: Math.round(h.getBoundingClientRect().top + window.scrollY),
                }));

            const allLinks = Array.from(document.querySelectorAll(cfg.model_link))
                .map(l => {
                    const rect = l.getBoundingClientRect();
                    return { href: l.getAttribute('href') || '', top: Math.round(rect.top + window.scrollY), w: Math.round(rect.width), h: Math.round(rect.height) };
                })
                .filter(l => l.w > cfg.min_w && l.h > cfg.min_h)
                .filter(l => !l.href.endsWith('/api'));

            const results = [];
            headings.forEach((section, i) => {
                const nextTop = i + 1 < headings.length ? headings[i + 1].top : Infinity;
                const cards = allLinks.filter(l => l.top > section.top && l.top < nextTop);
                const seen = new Set();
                const unique = cards.filter(c => { if (seen.has(c.href)) return false; seen.add(c.href); return true; });
                unique.forEach((card, idx) => {
                    results.push({ section: section.text, href: card.href, slot: idx + 1, totalSlots: unique.length });
                });
            });
            return results;
        }""",
        {
            "explore_headings": SELECTORS["explore_headings"],
            "model_link":       SELECTORS["model_link"],
            "min_w":            CONFIG["sections_min_card_w"],
            "min_h":            CONFIG["sections_min_card_h"],
        },
    )

async def extract_search_surface(page: Page) -> List[Dict]:
    """Extract from search results page — flat list, section label is 'Search Results'."""
    return await page.evaluate(
        """(cfg) => {
            const allLinks = Array.from(document.querySelectorAll(cfg.model_link))
                .map(l => {
                    const rect = l.getBoundingClientRect();
                    return { href: l.getAttribute('href') || '', top: Math.round(rect.top + window.scrollY), w: Math.round(rect.width), h: Math.round(rect.height) };
                })
                .filter(l => l.w > cfg.min_w && l.h > cfg.min_h)
                .filter(l => !l.href.endsWith('/api'));

            const seen = new Set();
            const unique = allLinks.filter(c => { if (seen.has(c.href)) return false; seen.add(c.href); return true; });

            return unique.map((card, idx) => ({
                section: "Search Results",
                href: card.href,
                slot: idx + 1,
                totalSlots: unique.length,
            }));
        }""",
        {
            "model_link": SELECTORS["model_link"],
            "min_w":      CONFIG["search_min_card_w"],
            "min_h":      CONFIG["search_min_card_h"],
        },
    )

async def extract_homepage_surface(page: Page) -> List[Dict]:
    """Homepage — group by nearest heading, or by above/below the fold."""
    return await page.evaluate(
        """(cfg) => {
            const headings = Array.from(document.querySelectorAll(cfg.homepage_headings))
                .filter(h => h.textContent && h.textContent.trim().length < 80)
                .map(h => ({
                    text: h.textContent.trim(),
                    top: Math.round(h.getBoundingClientRect().top + window.scrollY),
                }));

            const allLinks = Array.from(document.querySelectorAll(cfg.model_link))
                .map(l => {
                    const rect = l.getBoundingClientRect();
                    return { href: l.getAttribute('href') || '', top: Math.round(rect.top + window.scrollY), w: Math.round(rect.width), h: Math.round(rect.height) };
                })
                .filter(l => l.w > cfg.min_w && l.h > cfg.min_h)
                .filter(l => !l.href.endsWith('/api'));

            const seen = new Set();
            const unique = allLinks.filter(c => { if (seen.has(c.href)) return false; seen.add(c.href); return true; });
            unique.sort((a, b) => a.top - b.top);

            const zones = {};
            unique.forEach(card => {
                let section = card.top < 900 ? "Hero / Above the fold" : "Below the fold";
                for (let i = headings.length - 1; i >= 0; i--) {
                    if (headings[i].top <= card.top) { section = headings[i].text; break; }
                }
                if (!zones[section]) zones[section] = [];
                zones[section].push(card);
            });

            const results = [];
            Object.keys(zones).forEach(sectionName => {
                zones[sectionName].forEach((card, idx) => {
                    results.push({ section: sectionName, href: card.href, slot: idx + 1, totalSlots: zones[sectionName].length });
                });
            });
            return results;
        }""",
        {
            "homepage_headings": SELECTORS["homepage_headings"],
            "model_link":        SELECTORS["model_link"],
            "min_w":             CONFIG["homepage_min_card_w"],
            "min_h":             CONFIG["homepage_min_card_h"],
        },
    )

async def extract_learn_surface(page: Page) -> List[Dict]:
    """
    Extract articles from fal.ai/learn — grouped under h2 section headers
    like "Generative Media for Enterprise", "for Developers", "AI Tools & Model Comparisons".
    Each article is an <a href="/learn/..."> with a title inside.
    """
    return await page.evaluate(
        """() => {
            const sections = Array.from(document.querySelectorAll('h2'))
                .filter(h => h.textContent && h.textContent.trim().length < 80)
                .map(h => ({
                    text: h.textContent.trim(),
                    top: Math.round(h.getBoundingClientRect().top + window.scrollY),
                }));

            const allLinks = Array.from(document.querySelectorAll('a[href^="/learn/"]'))
                .map(l => {
                    const rect = l.getBoundingClientRect();
                    // Use the largest visible text inside as the "title"
                    const title = (l.textContent || '').replace(/\\s+/g, ' ').trim();
                    return {
                        href: l.getAttribute('href') || '',
                        title: title.substring(0, 120),
                        top: Math.round(rect.top + window.scrollY),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                    };
                })
                // Filter out nav links (very small) and require some text
                .filter(l => l.w > 100 && l.h > 30 && l.title.length > 10)
                // Filter out top-level category links (/learn/biz, /learn/devs, /learn/tools with no slug after)
                .filter(l => l.href.split('/').filter(Boolean).length >= 3);

            // Dedupe by href, keep first (highest position)
            const seen = new Set();
            const unique = allLinks.filter(l => { if (seen.has(l.href)) return false; seen.add(l.href); return true; });
            unique.sort((a, b) => a.top - b.top);

            const results = [];
            sections.forEach((section, i) => {
                const nextTop = i + 1 < sections.length ? sections[i + 1].top : Infinity;
                const items = unique.filter(l => l.top > section.top && l.top < nextTop);
                items.forEach((item, idx) => {
                    results.push({
                        section: section.text,
                        href: item.href,
                        title: item.title,
                        slot: idx + 1,
                        totalSlots: items.length,
                    });
                });
            });
            return results;
        }"""
    )

async def extract_blog_surface(page: Page) -> List[Dict]:
    """
    Extract posts from blog.fal.ai/ — flat chronological feed where each post
    is announced with an h2 whose parent (or nearest ancestor) contains a link.
    """
    return await page.evaluate(
        """() => {
            const posts = [];
            const seen = new Set();
            // Strategy: for each h2, find the enclosing <a> or the nearest <a> descendant/ancestor
            const h2s = Array.from(document.querySelectorAll('h2'));
            h2s.forEach((h2, idx) => {
                const title = (h2.textContent || '').replace(/\\s+/g, ' ').trim();
                if (title.length < 5) return;
                // Look for the article's link — try ancestor first, then descendant
                let anchor = h2.closest('a[href]');
                if (!anchor) {
                    // Try sibling/parent link
                    const parent = h2.parentElement;
                    if (parent) {
                        anchor = parent.querySelector('a[href]') || parent.closest('a[href]');
                    }
                }
                if (!anchor) return;
                const href = anchor.getAttribute('href') || '';
                // Only accept links that look like blog posts (relative slug or blog.fal.ai path)
                if (!href || href === '/' || href === '#') return;
                if (seen.has(href)) return;
                seen.add(href);
                const rect = h2.getBoundingClientRect();
                posts.push({
                    href: href,
                    title: title.substring(0, 160),
                    top: Math.round(rect.top + window.scrollY),
                });
            });
            posts.sort((a, b) => a.top - b.top);
            return posts.map((p, idx) => ({
                section: "Recent Posts",
                href: p.href,
                title: p.title,
                slot: idx + 1,
                totalSlots: posts.length,
            }));
        }"""
    )

EXTRACTORS = {
    "homepage":    extract_homepage_surface,
    "sections":    extract_sections_surface,
    "search":      extract_search_surface,
    "learn_index": extract_learn_surface,
    "blog_index":  extract_blog_surface,
}

# ============================================================================
# HERO CAROUSEL — must run BEFORE animation freeze (slide switching needs transitions)
# ============================================================================

async def extract_hero_carousel(page: Page) -> List[Dict]:
    """
    Navigate the hero carousel on /explore, clicking each target indicator,
    screenshotting the slide, and recording position.

    CRITICAL: this MUST be called before freeze_animations(). Slide switching
    depends on CSS transitions.
    """
    placements: List[Dict] = []

    try:
        slides_info = await page.evaluate(
            """(cfg) => {
                const section = document.querySelector(cfg.carousel_section);
                if (!section || section.children.length < cfg.carousel_min_children) return null;

                const indicators = section.children[section.children.length - 1];
                const spans = Array.from(indicators.querySelectorAll('span'))
                    .filter(s => s.textContent.trim().length > cfg.min_text_len);

                return spans.map((s, i) => ({
                    idx: i,
                    text: s.textContent.trim(),
                    isTarget: /happy.?horse|wan.?2\\.7|wan.?2\\.6/i.test(s.textContent),
                }));
            }""",
            {
                "carousel_section":        SELECTORS["carousel_section"],
                "carousel_min_children":   SELECTORS["carousel_min_children"],
                "min_text_len":            SELECTORS["carousel_indicator_min_text_len"],
            },
        )

        if not slides_info:
            return placements

        total_slides = len(slides_info)
        target_slides = [s for s in slides_info if s["isTarget"]]

        for slide in target_slides:
            # Click the indicator to activate this slide
            await page.evaluate(
                """(args) => {
                    const section = document.querySelector(args.sel);
                    const indicators = section.children[section.children.length - 1];
                    const spans = Array.from(indicators.querySelectorAll('span'))
                        .filter(s => s.textContent.trim().length > args.minLen);
                    const target = spans[args.idx];
                    if (target) (target.closest('div') || target).click();
                }""",
                {"sel": SELECTORS["carousel_section"], "minLen": SELECTORS["carousel_indicator_min_text_len"], "idx": slide["idx"]},
            )
            await asyncio.sleep(CONFIG["post_carousel_click_s"])

            # Freeze animations for a clean screenshot (this specific slide only)
            await page.add_style_tag(content="""
                *, *::before, *::after {
                    animation-duration: 0s !important;
                    animation-play-state: paused !important;
                    transition-duration: 0s !important;
                }
            """)
            await asyncio.sleep(CONFIG["post_freeze_settle_s"])

            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(CONFIG["post_scroll_settle_s"])

            slide_href = await page.evaluate(
                """(args) => {
                    const section = document.querySelector(args.sel);
                    const slideDiv = section.children[args.idx];
                    if (!slideDiv) return null;
                    const links = Array.from(slideDiv.querySelectorAll(args.link))
                        .filter(a => !a.getAttribute('href').endsWith('/api'));
                    return links.length > 0 ? links[0].getAttribute('href') : null;
                }""",
                {"sel": SELECTORS["carousel_section"], "link": SELECTORS["model_link"], "idx": slide["idx"]},
            )

            # Derive label from href when possible, fallback to indicator text
            label = get_model_label(slide_href) if slide_href else slide["text"]
            filename = f"explore_hero_{_slug(label)}.png"
            filepath = SCREENSHOT_DIR / filename
            await page.screenshot(path=str(filepath))

            if not _screenshot_is_valid(filepath):
                print(f"    \u26a0  hero screenshot too small ({filepath.stat().st_size}B), skipping {label}")
                continue

            placements.append({
                "model":              label,
                "family":             get_model_family(slide_href) if slide_href else "unknown",
                "href":               slide_href or "",
                "section":            "Hero Carousel",
                "slot":               slide["idx"] + 1,
                "total_in_section":   total_slides,
                "screenshot":         filename,
            })
            print(f"    [Hero Carousel] {label} \u2014 slot {slide['idx']+1}/{total_slides} \U0001f4f8")

    except Exception as e:
        print(f"    Hero carousel extraction error: {e}")

    return placements

# ============================================================================
# SCREENSHOT-PER-PLACEMENT — contextual capture with heading + card
# ============================================================================

async def screenshot_placement(page: Page, href: str, surface_name: str, idx: int) -> Optional[str]:
    """
    Contextual per-card screenshot: brings the card into viewport horizontally,
    frames the nearest heading above it, captures the full viewport.
    Returns filename on success, None if any step fails or screenshot is too small.
    """
    try:
        scroll_result = await page.evaluate(
            """(args) => {
                const links = Array.from(document.querySelectorAll('a[href="' + args.href + '"]'));
                if (!links.length) return null;
                let best = null, bestArea = 0;
                for (const link of links) {
                    const rect = link.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    if (area > bestArea) { bestArea = area; best = link; }
                }
                if (!best || bestArea < args.minArea) return null;
                best.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'instant' });
                return true;
            }""",
            {"href": href, "minArea": CONFIG["min_card_area"]},
        )

        if not scroll_result:
            return None

        await asyncio.sleep(CONFIG["post_scroll_settle_s"] + 0.1)

        measurements = await page.evaluate(
            """(args) => {
                const links = Array.from(document.querySelectorAll('a[href="' + args.href + '"]'));
                let best = null, bestArea = 0;
                for (const link of links) {
                    const rect = link.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    if (area > bestArea) { bestArea = area; best = link; }
                }
                if (!best) return null;

                const cardRect = best.getBoundingClientRect();
                const absTop = cardRect.top + window.scrollY;
                const inViewport = cardRect.left >= -args.slack && cardRect.right <= (args.vw + args.slack);

                const headings = Array.from(document.querySelectorAll('h1, h2, h3'));
                let nearestHeadingAbsTop = null, nearestHeading = null;
                for (const h of headings) {
                    const ht = h.getBoundingClientRect().top + window.scrollY;
                    if (ht < absTop && ht > absTop - args.searchRange) {
                        if (!nearestHeadingAbsTop || ht > nearestHeadingAbsTop) {
                            nearestHeadingAbsTop = ht;
                            nearestHeading = h.textContent.trim().substring(0, 50);
                        }
                    }
                }

                return {
                    cardAbsTop: absTop,
                    cardAbsBottom: cardRect.bottom + window.scrollY,
                    inViewport: inViewport,
                    headingAbsTop: nearestHeadingAbsTop,
                    headingText: nearestHeading,
                };
            }""",
            {
                "href":        href,
                "vw":          CONFIG["viewport_width"],
                "slack":       CONFIG["card_viewport_slack_px"],
                "searchRange": CONFIG["heading_search_range_px"],
            },
        )

        if not measurements:
            return None

        # Vertical framing
        heading_top = measurements["headingAbsTop"]
        card_top = measurements["cardAbsTop"]
        if heading_top is not None and (card_top - heading_top) < CONFIG["heading_include_gap_px"]:
            scroll_to = max(0, heading_top - CONFIG["heading_top_margin_px"])
        else:
            scroll_to = max(0, card_top - CONFIG["card_only_top_margin_px"])

        await page.evaluate(f"window.scrollTo(0, {int(scroll_to)})")
        await asyncio.sleep(CONFIG["post_scroll_settle_s"])

        filename = f"{surface_name}_{idx}_{_slug(get_model_label(href))}.png"
        filepath = SCREENSHOT_DIR / filename
        await page.screenshot(path=str(filepath))

        if not _screenshot_is_valid(filepath):
            print(f"    \u26a0  screenshot too small ({filepath.stat().st_size}B) for {href}")
            return None

        return filename

    except Exception as e:
        print(f"    screenshot_placement error for {href}: {e}")
        return None

async def screenshot_article(page: Page, href: str, title: str, surface_name: str, idx: int) -> Optional[str]:
    """
    Screenshot an article/post link in context. Same viewport-framing logic as
    screenshot_placement, but the filename uses a slug derived from the article
    title (there is no model-label mapping for editorial content).
    """
    try:
        scroll_result = await page.evaluate(
            """(args) => {
                const links = Array.from(document.querySelectorAll('a[href="' + args.href + '"]'));
                if (!links.length) return null;
                let best = null, bestArea = 0;
                for (const link of links) {
                    const rect = link.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    if (area > bestArea) { bestArea = area; best = link; }
                }
                if (!best) return null;
                // Articles can be text-only links; loosen the min-area gate
                if (bestArea < 500) return null;
                best.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                return true;
            }""",
            {"href": href},
        )

        if not scroll_result:
            return None

        await asyncio.sleep(CONFIG["post_scroll_settle_s"] + 0.1)

        measurements = await page.evaluate(
            """(args) => {
                const links = Array.from(document.querySelectorAll('a[href="' + args.href + '"]'));
                let best = null, bestArea = 0;
                for (const link of links) {
                    const rect = link.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    if (area > bestArea) { bestArea = area; best = link; }
                }
                if (!best) return null;
                const cardRect = best.getBoundingClientRect();
                const absTop = cardRect.top + window.scrollY;

                const headings = Array.from(document.querySelectorAll('h1, h2, h3'));
                let nearestHeadingAbsTop = null;
                for (const h of headings) {
                    const ht = h.getBoundingClientRect().top + window.scrollY;
                    if (ht < absTop && ht > absTop - args.searchRange) {
                        if (!nearestHeadingAbsTop || ht > nearestHeadingAbsTop) {
                            nearestHeadingAbsTop = ht;
                        }
                    }
                }
                return {
                    cardAbsTop: absTop,
                    headingAbsTop: nearestHeadingAbsTop,
                };
            }""",
            {"href": href, "searchRange": CONFIG["heading_search_range_px"]},
        )

        if not measurements:
            return None

        heading_top = measurements["headingAbsTop"]
        card_top = measurements["cardAbsTop"]
        if heading_top is not None and (card_top - heading_top) < CONFIG["heading_include_gap_px"]:
            scroll_to = max(0, heading_top - CONFIG["heading_top_margin_px"])
        else:
            scroll_to = max(0, card_top - CONFIG["card_only_top_margin_px"])

        await page.evaluate(f"window.scrollTo(0, {int(scroll_to)})")
        await asyncio.sleep(CONFIG["post_scroll_settle_s"])

        slug = _slug(title)[:50] or _slug(href)[-40:] or f"item_{idx}"
        filename = f"{surface_name}_{idx}_{slug}.png"
        filepath = SCREENSHOT_DIR / filename
        await page.screenshot(path=str(filepath))

        if not _screenshot_is_valid(filepath):
            print(f"    \u26a0  article screenshot too small ({filepath.stat().st_size}B) for {href}")
            return None

        return filename

    except Exception as e:
        print(f"    screenshot_article error for {href}: {e}")
        return None

# ============================================================================
# PHASES — explicit ordering. NEVER change without understanding phase 2/3 dep.
# ============================================================================

async def phase_1_navigate_and_wait(page: Page, surface: Dict) -> Optional[int]:
    """Navigate, wait for hydration, dismiss cookies. Returns HTTP status or None."""
    response = await page.goto(surface["url"], wait_until="domcontentloaded",
                                timeout=CONFIG["nav_timeout_ms"])
    status = response.status if response else None

    # blog.fal.ai is Ghost/static — no RSC flight data, skip the wait
    if surface["type"] != "blog_index":
        await _wait_with_retry(
            page,
            "() => window.__intercepted_flight && window.__intercepted_flight.length > 5",
            CONFIG["flight_wait_ms"],
            "rsc_flight",
        )
    await asyncio.sleep(CONFIG["post_nav_settle_s"])

    # Dismiss cookie banner if present — required before viewport screenshots
    try:
        cookie_btn = page.locator(SELECTORS["cookie_buttons"]).first
        if await cookie_btn.count() > 0:
            await cookie_btn.click(timeout=CONFIG["click_timeout_ms"])
            await asyncio.sleep(CONFIG["cookie_click_settle_s"])
    except Exception:
        pass

    # For grid pages, wait for model cards to actually render (with retry)
    if surface["type"] in MODEL_SURFACE_TYPES:
        await _wait_with_retry(
            page,
            f"() => document.querySelectorAll('{SELECTORS['model_link']}').length >= {CONFIG['min_model_links_ready']}",
            CONFIG["grid_wait_ms"],
            "grid_hydration",
        )
        await asyncio.sleep(CONFIG["post_grid_settle_s"])

    # For learn index, wait for article links to render
    elif surface["type"] == "learn_index":
        await _wait_with_retry(
            page,
            f"() => document.querySelectorAll('{SELECTORS['learn_article_link']}').length >= 3",
            CONFIG["grid_wait_ms"],
            "learn_hydration",
        )
        await asyncio.sleep(CONFIG["post_grid_settle_s"])

    # For blog index (Ghost-based static site), just wait for h2 posts to be visible
    elif surface["type"] == "blog_index":
        await _wait_with_retry(
            page,
            "() => document.querySelectorAll('h2').length >= 2",
            CONFIG["grid_wait_ms"],
            "blog_hydration",
        )
        await asyncio.sleep(CONFIG["post_grid_settle_s"])

    return status

async def phase_2_extract_dynamic(page: Page, surface: Dict) -> List[Dict]:
    """
    Extract elements that require live animations (hero carousel).
    MUST run BEFORE phase_3_freeze_animations().
    """
    if surface["type"] == "sections":
        return await extract_hero_carousel(page)
    return []

async def phase_3_freeze_animations(page: Page) -> None:
    """Kill CSS animations/transitions so screenshots are deterministic."""
    await page.add_style_tag(content="""
        *, *::before, *::after {
            animation-duration: 0s !important;
            transition-duration: 0s !important;
        }
    """)

async def phase_4_extract_static(page: Page, surface: Dict) -> List[Dict]:
    """Extract static grid/list content post-freeze."""
    extractor = EXTRACTORS.get(surface["type"])
    if not extractor:
        return []
    return await extractor(page)

async def phase_5_screenshot_placements(
    page: Page, surface: Dict, target_cards: List[Dict]
) -> List[Dict]:
    """Screenshot each target placement. Only include those with valid screenshots."""
    placements = []
    for idx, card in enumerate(target_cards):
        screenshot = await screenshot_placement(page, card["href"], surface["name"], idx)
        placements.append({
            "model":              get_model_label(card["href"]),
            "family":             get_model_family(card["href"]),
            "href":               card["href"],
            "section":            card["section"],
            "slot":               card["slot"],
            "total_in_section":   card["totalSlots"],
            "screenshot":         screenshot,   # None if capture failed
        })
        icon = "\U0001f4f8" if screenshot else "\u26a0\ufe0f"
        print(f"    [{card['section']}] {placements[-1]['model']} \u2014 slot {card['slot']}/{card['totalSlots']} {icon}")
    return placements

async def phase_5_screenshot_articles(
    page: Page, surface: Dict, target_articles: List[Dict]
) -> List[Dict]:
    """Screenshot each editorial article/post that references a tracked model."""
    placements = []
    for idx, art in enumerate(target_articles):
        href = art["href"]
        title = art.get("title", "")
        screenshot = await screenshot_article(page, href, title, surface["name"], idx)
        placements.append({
            "model":              get_article_label(href, title),
            "family":             get_article_family(href, title),
            "href":               href,
            "title":              title,
            "section":            art["section"],
            "slot":               art["slot"],
            "total_in_section":   art["totalSlots"],
            "screenshot":         screenshot,
        })
        icon = "\U0001f4f8" if screenshot else "\u26a0\ufe0f"
        print(f"    [{art['section']}] {placements[-1]['model']} \u00b7 {title[:50]} \u2014 slot {art['slot']}/{art['totalSlots']} {icon}")
    return placements

# ============================================================================
# ORCHESTRATOR
# ============================================================================

async def scan_surface(context: BrowserContext, surface: Dict) -> Dict:
    print(f"\n  Scanning: {surface['name']} \u2014 {surface['url']}")
    page = await context.new_page()
    await page.add_init_script(INIT_SCRIPT)

    try:
        # PHASE 1: navigate + hydrate
        status = await phase_1_navigate_and_wait(page, surface)

        # Self-checks (scraper health only, NOT business facts)
        health = await run_scraper_health_checks(page, status, surface)

        # PHASE 2: dynamic content BEFORE animation freeze
        hero_placements = await phase_2_extract_dynamic(page, surface)

        # PHASE 3: freeze animations
        await phase_3_freeze_animations(page)

        # Viewport screenshot (evidence for surfaces with no placements)
        viewport_file = f"{surface['name']}_viewport.png"
        viewport_path = SCREENSHOT_DIR / viewport_file
        await page.screenshot(path=str(viewport_path))
        if not _screenshot_is_valid(viewport_path):
            print(f"    \u26a0  viewport screenshot too small ({viewport_path.stat().st_size}B)")

        # PHASE 4: static extraction (grid cards or article links)
        all_items = await phase_4_extract_static(page, surface)

        # PHASE 5: per-placement screenshots — filter + capture rules depend on surface type
        if surface["type"] in ARTICLE_SURFACE_TYPES:
            target_items = [c for c in all_items if is_target_article(c["href"], c.get("title", ""))]
            static_placements = await phase_5_screenshot_articles(page, surface, target_items)
        else:
            target_items = [c for c in all_items if is_target_model(c["href"])]
            static_placements = await phase_5_screenshot_placements(page, surface, target_items)

        # Merge: hero first (most prominent), then static
        placements = hero_placements + static_placements

        await page.close()
        return {
            "surface":            surface["name"],
            "url":                surface["url"],
            "type":               surface["type"],
            "viewport_screenshot": viewport_file,
            "placements":         placements,
            "self_check":         health.to_dict(),
        }

    except Exception as e:
        print(f"    ERROR: {e}")
        await page.close()
        return {
            "surface": surface["name"],
            "url":     surface["url"],
            "error":   str(e),
        }

async def main():
    print("=" * 60)
    print("  fal.ai Promotion Scanner v2")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    reset_screenshot_dir()
    print(f"  Cleared {SCREENSHOT_DIR.name}/ \u2014 fresh capture directory")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": CONFIG["viewport_width"], "height": CONFIG["viewport_height"]},
            device_scale_factor=CONFIG["device_scale_factor"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        results = []
        for surface in SURFACES:
            results.append(await scan_surface(context, surface))
        await browser.close()

    # Aggregate scraper health across surfaces
    surface_health = [r.get("self_check") for r in results if r.get("self_check")]
    failed_checks = [
        (h["surface"], c["name"], c["detail"])
        for h in surface_health for c in h["checks"] if not c["passed"]
    ]

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "surfaces":  results,
        "scraper_health": {
            "all_passed":    len(failed_checks) == 0,
            "failed_checks": [{"surface": s, "check": c, "detail": d} for s, c, d in failed_checks],
        },
    }
    output_path = OUTPUT_DIR / "scan_v2.json"
    prev_path = OUTPUT_DIR / "scan_v2.prev.json"
    # Archive previous scan for comparison in report builder
    if output_path.exists():
        import shutil
        shutil.copy2(output_path, prev_path)
        print(f"  Archived previous scan \u2192 {prev_path.name}")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print("  SCAN COMPLETE")
    print(f"{'='*60}")
    total_placements = sum(len(r.get("placements", [])) for r in results)
    print(f"  Total target model placements: {total_placements}")
    for r in results:
        if r.get("placements"):
            print(f"  {r['surface']:25s} | {len(r['placements'])} placements")
        elif r.get("error"):
            print(f"  {r['surface']:25s} | ERROR: {r['error'][:60]}")

    # Scraper health summary — loud alerts only if scraper itself is broken
    if failed_checks:
        print(f"\n  \u26a0  SCRAPER HEALTH: {len(failed_checks)} check(s) failed:")
        for s, c, d in failed_checks:
            print(f"      - {s}: {c} ({d})")
        print("  \u2192 Scanner may need selector updates. Investigate before trusting results.")
    else:
        print(f"\n  \u2714 SCRAPER HEALTH: all checks passed across {len(surface_health)} surfaces")

    return output

if __name__ == "__main__":
    asyncio.run(main())
