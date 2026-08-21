"""
Build scan_report_v2.html from scan_v2.json with embedded base64 screenshots.
UI: screenshots visible by default, position heatmap summary, card layout, minimal text.
Includes change comparison vs previous scan (slot changes, additions, removals).
"""

import json
import base64
from pathlib import Path

BASE = Path(__file__).parent
PROJECT_ROOT = BASE.parent
SCAN = PROJECT_ROOT / "data" / "scan_v2.json"
PREV_SCAN = PROJECT_ROOT / "data" / "scan_v2.prev.json"
SHOT_DIR = PROJECT_ROOT / "public" / "screenshots_v2"
OUT = PROJECT_ROOT / "scan_report_v2.html"

SURFACE_ORDER = [
    "homepage",
    "explore",
    "search_all",
    "search_video",
    "search_video_generation",
    "search_text_to_video",
    "search_image_to_video",
    "learn",
    "blog",
]

SURFACE_META = {
    "homepage":               {"title": "Homepage", "short": "Home"},
    "explore":                {"title": "Explore Page", "short": "Explore"},
    "search_all":             {"title": "Search: All Models", "short": "All"},
    "search_video":           {"title": 'Search: "video"', "short": "video"},
    "search_video_generation":{"title": 'Search: "video generation"', "short": "vid gen"},
    "search_text_to_video":   {"title": 'Search: "text to video"', "short": "T2V"},
    "search_image_to_video":  {"title": 'Search: "image to video"', "short": "I2V"},
    "learn":                  {"title": "fal.ai/learn (Editorial)", "short": "Learn"},
    "blog":                   {"title": "blog.fal.ai (Blog)", "short": "Blog"},
}

MODEL_FAMILIES = ["HH 1.1", "HH 1.0", "Wan 2.7", "Wan 2.6"]
MODEL_COLORS = {
    "happy-horse-1.1": "#7c3aed",
    "happy-horse-1.0": "#a78bfa",
    "wan-2.7": "#0891b2",
    "wan-2.6": "#0ea5e9",
}


# ---------- Comparison Logic ----------

def build_prev_index(prev_data):
    """
    Build a lookup from previous scan: {(surface, model, section, href): placement_dict}
    This lets us quickly find the previous state for any current placement.
    Uses href to disambiguate same-model variants in the same section.
    """
    index = {}
    if not prev_data:
        return index
    for s in prev_data.get("surfaces", []):
        surface_name = s.get("surface", "")
        for p in s.get("placements") or []:
            href = p.get("href", "").rstrip("/").lower()
            key = (surface_name, p.get("model", ""), p.get("section", ""), href)
            index[key] = p
    return index


def build_current_index(data):
    """Build a lookup from current scan for detecting removals."""
    index = set()
    for s in data.get("surfaces", []):
        surface_name = s.get("surface", "")
        for p in s.get("placements") or []:
            href = p.get("href", "").rstrip("/").lower()
            key = (surface_name, p.get("model", ""), p.get("section", ""), href)
            index.add(key)
    return index


def compute_change(current_placement, surface_name, prev_index):
    """
    Compute the change indicator for a placement.
    Returns a dict with change info or None if no previous data.
    """
    href = current_placement.get("href", "").rstrip("/").lower()
    key = (surface_name, current_placement.get("model", ""), current_placement.get("section", ""), href)
    prev = prev_index.get(key)
    if prev is None:
        # Model was not in this surface/section before — it's new
        return {"type": "new"}
    prev_slot = prev.get("slot", 0)
    curr_slot = current_placement.get("slot", 0)
    if prev_slot == curr_slot:
        return {"type": "unchanged", "slot": curr_slot}
    delta = prev_slot - curr_slot  # positive = moved UP (improved)
    return {"type": "moved", "delta": delta, "prev_slot": prev_slot}


def get_removals(surface_name, prev_data, current_index):
    """Find placements that existed in previous scan but are gone from current."""
    removals = []
    if not prev_data:
        return removals
    for s in prev_data.get("surfaces", []):
        if s.get("surface") != surface_name:
            continue
        for p in s.get("placements") or []:
            href = p.get("href", "").rstrip("/").lower()
            key = (surface_name, p.get("model", ""), p.get("section", ""), href)
            if key not in current_index:
                removals.append(p)
    return removals


# ---------- Rendering Helpers ----------

def b64(path: Path) -> str:
    if not path.exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

def slot_badge_class(slot: int, total: int) -> str:
    if total <= 0: return "slot-bad"
    ratio = slot / total
    if ratio <= 0.33: return "slot-good"
    if ratio <= 0.66: return "slot-ok"
    return "slot-bad"

def model_color(family: str) -> str:
    return MODEL_COLORS.get(family, "#64748b")

def get_family_display(label: str) -> str:
    if label.startswith("HH 1.1"): return "HH 1.1"
    if label.startswith("HH 1.0"): return "HH 1.0"
    if label.startswith("Wan 2.7"): return "Wan 2.7"
    if label.startswith("Wan 2.6"): return "Wan 2.6"
    return label


def render_change_indicator(change):
    """Render the HTML for a change indicator next to a placement."""
    if change is None:
        return ""
    t = change["type"]
    if t == "new":
        return '<span class="change-badge change-new">NEW</span>'
    if t == "unchanged":
        return ""
    if t == "moved":
        delta = change["delta"]
        if delta > 0:
            return f'<span class="change-badge change-up">&#9650; {delta}</span>'
        else:
            return f'<span class="change-badge change-down">&#9660; {abs(delta)}</span>'
    return ""


def render_removal_card(p):
    """Render a removed placement as a dimmed card."""
    color = model_color(p.get("family", ""))
    section = p.get("section", "")
    slot = p.get("slot", 0)
    total = p.get("total_in_section", 0)
    return f"""
    <div class="placement-card removed-card">
      <div class="card-img"><div class="no-ss removed-label">REMOVED</div></div>
      <div class="card-meta">
        <span class="model-chip" style="background:{color}18;color:{color};border-color:{color}">{p.get('model','')}</span>
        <span class="slot-badge slot-bad">was slot {slot}/{total}</span>
        {f'<span class="section-name">{section}</span>' if section else ''}
        <span class="change-badge change-removed">REMOVED</span>
      </div>
    </div>
    """


def build_heatmap(surfaces, prev_index):
    """Build a model x surface heatmap for the executive summary, with change indicators."""
    grid = {fam: {} for fam in MODEL_FAMILIES}
    surface_names = [s["surface"] for s in surfaces if s.get("type") != "model_page"]

    for s in surfaces:
        if s.get("type") == "model_page":
            continue
        for p in s.get("placements") or []:
            fam = get_family_display(p["model"])
            if fam in grid:
                name = s["surface"]
                if name not in grid[fam]:
                    grid[fam][name] = []
                grid[fam][name].append((p, compute_change(p, name, prev_index)))

    # Build HTML table
    header_cells = "".join(
        f'<th><span>{SURFACE_META.get(n, {}).get("short", n)}</span></th>'
        for n in surface_names
    )

    rows = []
    for fam in MODEL_FAMILIES:
        cells = []
        for sname in surface_names:
            entries = grid[fam].get(sname, [])
            if not entries:
                cells.append('<td class="cell-empty">\u2014</td>')
            else:
                badges = []
                for p, change in entries:
                    cls = slot_badge_class(p["slot"], p["total_in_section"])
                    change_html = render_change_indicator(change)
                    badges.append(f'<span class="mini-badge {cls}">{p["slot"]}/{p["total_in_section"]}</span>{change_html}')
                cells.append(f'<td class="cell-found">{"".join(badges)}</td>')

        fam_color_map = {"HH 1.1": "happy-horse-1.1", "HH 1.0": "happy-horse-1.0", "Wan 2.7": "wan-2.7", "Wan 2.6": "wan-2.6"}
        color = MODEL_COLORS.get(fam_color_map.get(fam, ""), "#64748b")
        rows.append(f'<tr><td class="fam-label" style="border-left: 3px solid {color}">{fam}</td>{"".join(cells)}</tr>')

    return f"""
    <div class="heatmap-wrap">
      <table class="heatmap">
        <thead><tr><th></th>{header_cells}</tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    {build_family_stack(grid, surface_names)}
    """

def build_family_stack(grid, surface_names):
    """Mobile-only alternative to the heatmap: card stack grouped by model family."""
    fam_color_map = {"HH 1.1": "happy-horse-1.1", "HH 1.0": "happy-horse-1.0", "Wan 2.7": "wan-2.7", "Wan 2.6": "wan-2.6"}
    cards = []
    for fam in MODEL_FAMILIES:
        color = MODEL_COLORS.get(fam_color_map.get(fam, ""), "#64748b")
        total_count = sum(len(grid[fam].get(sname, [])) for sname in surface_names)
        rows = []
        for sname in surface_names:
            entries = grid[fam].get(sname, [])
            if not entries:
                continue
            surface_short = SURFACE_META.get(sname, {}).get("short", sname)
            badges = "".join(
                f'<span class="mini-badge {slot_badge_class(p["slot"], p["total_in_section"])}">{p["slot"]}/{p["total_in_section"]}</span>{render_change_indicator(change)}'
                for p, change in entries
            )
            rows.append(f'<div class="stack-row"><span class="stack-surface">{surface_short}</span><span class="stack-badges">{badges}</span></div>')
        if not rows:
            body = '<div class="stack-empty">No placements on tracked surfaces</div>'
        else:
            body = "".join(rows)
        count_pill = f'<span class="stack-count">{total_count}</span>' if total_count else '<span class="stack-count zero">0</span>'
        cards.append(f"""
        <div class="stack-card" style="border-left: 4px solid {color}">
          <div class="stack-head">
            <span class="stack-fam" style="color:{color}">{fam}</span>
            {count_pill}
          </div>
          {body}
        </div>
        """)
    return f'<div class="family-stack">{"".join(cards)}</div>'

def render_placement_card(p, surface_name, idx, change):
    """Render a single placement as a card with screenshot visible and change indicator."""
    ss = p.get("screenshot")
    b64_img = b64(SHOT_DIR / ss) if ss else ""
    total = p.get("total_in_section", 0)
    slot = p.get("slot", 0)
    badge_cls = slot_badge_class(slot, total)
    color = model_color(p.get("family", ""))
    title = p.get("title", "")
    change_html = render_change_indicator(change)

    img_html = f'<img src="{b64_img}" alt="{p["model"]}" onclick="openModal(this.src)"/>' if b64_img else '<div class="no-ss">No screenshot</div>'

    # Hide section name when it's redundant (search pages show "Search Results", explore uses sub-headers)
    section_name = p.get('section', '')
    show_section = section_name and section_name != "Search Results" and surface_name not in ("explore", "learn")
    section_html = f'<span class="section-name">{section_name}</span>' if show_section else ""

    # Article title (learn/blog only) — link to the source article
    href = p.get("href", "")
    if title:
        # Build absolute URL for relative fal.ai/learn paths
        if href.startswith("/learn/"):
            full_url = f"https://fal.ai{href}"
        elif href.startswith("http"):
            full_url = href
        else:
            # blog.fal.ai relative slug
            full_url = f"https://blog.fal.ai{href}" if href.startswith("/") else f"https://blog.fal.ai/{href}"
        title_html = f'<a class="article-title" href="{full_url}" target="_blank">{title}</a>'
    else:
        title_html = ""

    return f"""
    <div class="placement-card">
      <div class="card-img">{img_html}</div>
      {title_html}
      <div class="card-meta">
        <span class="model-chip" style="background:{color}18;color:{color};border-color:{color}">{p['model']}</span>
        <span class="slot-badge {badge_cls}">slot {slot}/{total}</span>
        {change_html}
        {section_html}
      </div>
    </div>
    """

def render_surface(surface, prev_index, current_index, prev_data):
    name = surface.get("surface", "")
    meta = SURFACE_META.get(name, {"title": name, "short": name})
    url = surface.get("url", "")

    if surface.get("error"):
        return f'<section class="surface"><h2>{meta["title"]}</h2><div class="error">Error: {surface["error"]}</div></section>'

    # Skip model pages
    if surface.get("type") == "model_page":
        return ""

    placements = surface.get("placements", []) or []
    removals = get_removals(name, prev_data, current_index)

    # Viewport screenshot
    vp = surface.get("viewport_screenshot")
    vp_b64 = b64(SHOT_DIR / vp) if vp else ""

    if placements:
        # For explore and learn pages: group by section header
        if name in ("explore", "learn"):
            from collections import OrderedDict
            grouped = OrderedDict()
            for p in placements:
                section = p.get("section", "Other")
                if section not in grouped:
                    grouped[section] = []
                grouped[section].append(p)

            content_html = ""
            for section_name, section_placements in grouped.items():
                cards = "".join(
                    render_placement_card(p, name, i, compute_change(p, name, prev_index))
                    for i, p in enumerate(section_placements)
                )
                content_html += f"""
                <div class="section-group">
                  <h3 class="section-subheader">{section_name}</h3>
                  <div class="cards-grid">{cards}</div>
                </div>
                """
        else:
            cards = "".join(
                render_placement_card(p, name, i, compute_change(p, name, prev_index))
                for i, p in enumerate(placements)
            )
            content_html = f'<div class="cards-grid">{cards}</div>'
    else:
        content_html = f"""
        <div class="no-placements">
          No tracked models on this page.
          <img src="{vp_b64}" alt="Viewport" onclick="openModal(this.src)" class="viewport-evidence"/>
        </div>
        """

    # Render removal cards if any
    removal_html = ""
    if removals:
        removal_cards = "".join(render_removal_card(p) for p in removals)
        removal_html = f"""
        <div class="removals-section">
          <h3 class="section-subheader removals-header">Removed since last scan</h3>
          <div class="cards-grid">{removal_cards}</div>
        </div>
        """

    count_label = f'<span class="count-badge">{len(placements)}</span>' if placements else '<span class="count-badge zero">0</span>'

    return f"""
    <section class="surface">
      <div class="surface-header">
        <h2>{meta['title']} {count_label}</h2>
        <a class="url" href="{url}" target="_blank">{url}</a>
      </div>
      {content_html}
      {removal_html}
    </section>
    """

def main():
    data = json.loads(SCAN.read_text())
    surfaces = {s["surface"]: s for s in data["surfaces"]}
    ordered = [surfaces[n] for n in SURFACE_ORDER if n in surfaces]

    # Load previous scan for comparison
    prev_data = None
    prev_timestamp = ""
    if PREV_SCAN.exists():
        try:
            prev_data = json.loads(PREV_SCAN.read_text())
            prev_timestamp = prev_data.get("timestamp", "")[:16].replace("T", " ")
        except (json.JSONDecodeError, KeyError):
            prev_data = None

    prev_index = build_prev_index(prev_data)
    current_index = build_current_index(data)

    # Stats
    non_model_surfaces = [s for s in ordered if s.get("type") != "model_page"]
    total_placements = sum(len(s.get("placements") or []) for s in non_model_surfaces)
    total_removals = sum(len(get_removals(s.get("surface", ""), prev_data, current_index)) for s in non_model_surfaces)

    heatmap_html = build_heatmap(ordered, prev_index)
    sections_html = "\n".join(render_surface(s, prev_index, current_index, prev_data) for s in ordered)
    scan_time = data.get("timestamp", "")[:16].replace("T", " ")

    # Comparison header info
    compare_html = ""
    if prev_data:
        compare_html = f'<span class="compare-info">Compared to scan from {prev_timestamp} UTC</span>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>fal.ai Placement Scan</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro", "Segoe UI", sans-serif;
  margin: 0; background: #f8fafc; color: #1e293b;
  font-size: 14px; line-height: 1.5;
}}
header {{
  background: #0f172a; color: white;
  padding: 20px 28px; display: flex; align-items: baseline; gap: 16px;
  flex-wrap: wrap;
}}
header h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
header .ts {{ opacity: 0.6; font-size: 12px; }}
header .compare-info {{
  font-size: 11px; opacity: 0.7; flex-basis: 100%;
  margin-top: 2px;
}}
main {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}

/* Heatmap */
.summary {{ background: white; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }}
.summary h2 {{ margin: 0 0 12px; font-size: 15px; font-weight: 600; color: #475569; }}
.heatmap-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 -4px; }}
.heatmap {{ width: 100%; min-width: 480px; border-collapse: collapse; font-size: 12px; }}
.heatmap thead th {{ padding: 6px 8px; font-weight: 500; color: #64748b; text-align: center; font-size: 11px; }}
.heatmap tbody td {{ padding: 8px 6px; text-align: center; border-top: 1px solid #f1f5f9; }}
.fam-label {{ font-weight: 600; text-align: left !important; padding-left: 10px !important; white-space: nowrap; }}
.cell-empty {{ color: #cbd5e1; }}
.cell-found {{ }}
.mini-badge {{
  display: inline-block; padding: 2px 6px; border-radius: 4px;
  font-family: "SF Mono", Menlo, monospace; font-size: 10px; font-weight: 600;
  margin: 1px;
}}
.slot-good {{ background: #dcfce7; color: #166534; }}
.slot-ok   {{ background: #fef9c3; color: #854d0e; }}
.slot-bad  {{ background: #fee2e2; color: #991b1b; }}
.total-line {{ margin-top: 10px; font-size: 12px; color: #64748b; }}

/* Change indicators */
.change-badge {{
  display: inline-block; padding: 1px 5px; border-radius: 3px;
  font-size: 9px; font-weight: 700; margin-left: 3px;
  vertical-align: middle; letter-spacing: 0.3px;
}}
.change-up {{ background: #dcfce7; color: #166534; }}
.change-down {{ background: #fee2e2; color: #991b1b; }}
.change-new {{ background: #dbeafe; color: #1e40af; }}
.change-removed {{ background: #fee2e2; color: #991b1b; }}

/* Family stack (mobile-only alternative to the heatmap). Hidden by default. */
.family-stack {{ display: none; }}
.stack-card {{
  background: #f8fafc; border-radius: 8px; padding: 10px 12px;
  margin-bottom: 8px;
}}
.stack-card:last-child {{ margin-bottom: 0; }}
.stack-head {{
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 6px;
}}
.stack-fam {{ font-weight: 700; font-size: 13px; }}
.stack-count {{
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 20px; height: 20px; border-radius: 999px;
  background: #0f172a; color: white; font-size: 11px; font-weight: 700;
  padding: 0 6px;
}}
.stack-count.zero {{ background: #e2e8f0; color: #94a3b8; }}
.stack-row {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 4px 0; font-size: 12px; gap: 8px;
}}
.stack-row + .stack-row {{ border-top: 1px dashed #e2e8f0; }}
.stack-surface {{ color: #475569; font-weight: 500; }}
.stack-badges {{ display: flex; flex-wrap: wrap; gap: 3px; justify-content: flex-end; }}
.stack-empty {{ font-size: 11px; color: #94a3b8; font-style: italic; }}

/* Surface sections */
section.surface {{
  background: white; border-radius: 10px; padding: 20px;
  margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.04);
}}
.surface-header {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }}
.surface-header h2 {{ margin: 0; font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }}
.url {{ font-size: 11px; color: #94a3b8; text-decoration: none; }}
.url:hover {{ text-decoration: underline; }}
.count-badge {{
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 22px; height: 22px; border-radius: 999px;
  background: #0f172a; color: white; font-size: 11px; font-weight: 700;
}}
.count-badge.zero {{ background: #e2e8f0; color: #94a3b8; }}

/* Placement cards grid */
.cards-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}}
.placement-card {{
  border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;
  transition: box-shadow .15s;
}}
.placement-card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.08); }}
.placement-card.removed-card {{
  opacity: 0.6; border-color: #fca5a5; background: #fef2f2;
}}
.card-img img {{
  width: 100%; display: block; cursor: zoom-in;
  border-bottom: 1px solid #f1f5f9;
}}
.card-img .no-ss {{
  height: 120px; display: flex; align-items: center; justify-content: center;
  color: #94a3b8; font-size: 12px; background: #f8fafc;
}}
.card-img .removed-label {{
  color: #dc2626; font-weight: 700; font-size: 14px;
  background: #fef2f2;
}}
.card-meta {{
  padding: 8px 10px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
}}
.model-chip {{
  padding: 3px 8px; border-radius: 999px; font-weight: 600; font-size: 11px;
  border: 1px solid;
}}
.slot-badge {{
  padding: 2px 6px; border-radius: 4px;
  font-family: "SF Mono", Menlo, monospace; font-size: 10px; font-weight: 600;
}}
.section-name {{ font-size: 11px; color: #64748b; }}
.article-title {{
  display: block; padding: 10px 12px 4px; font-size: 13px; font-weight: 600;
  color: #0f172a; text-decoration: none; line-height: 1.35;
}}
.article-title:hover {{ color: #2563eb; text-decoration: underline; }}

/* Section grouping within explore */
.section-group {{ margin-bottom: 16px; }}
.section-group:last-child {{ margin-bottom: 0; }}
.section-subheader {{ font-size: 13px; font-weight: 600; color: #475569; margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid #f1f5f9; }}
.removals-section {{ margin-top: 16px; padding-top: 12px; border-top: 2px dashed #fca5a5; }}
.removals-header {{ color: #dc2626 !important; }}

/* No placements */
.no-placements {{
  color: #64748b; font-size: 13px; text-align: center; padding: 16px;
}}
.viewport-evidence {{
  display: block; max-width: 100%; max-height: 300px; margin: 12px auto 0;
  border-radius: 8px; border: 1px solid #e2e8f0; cursor: zoom-in;
}}

/* Error state */
.error {{
  color: #dc2626; font-size: 13px; padding: 12px;
  background: #fef2f2; border-radius: 6px;
}}

/* Modal */
#modal {{
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,.88); z-index: 1000;
  justify-content: center; align-items: center; padding: 32px; cursor: pointer;
}}
#modal.open {{ display: flex; }}
#modal img {{ max-width: 100%; max-height: 100%; border-radius: 6px; cursor: default; }}

/* Mobile — <=640px */
@media (max-width: 640px) {{
  body {{ font-size: 13px; }}
  header {{ padding: 14px 16px; gap: 8px; }}
  header h1 {{ font-size: 17px; }}
  header .ts {{ font-size: 11px; flex-basis: 100%; }}
  main {{ padding: 12px; }}
  .summary {{ padding: 14px; }}
  section.surface {{ padding: 14px; }}
  .surface-header h2 {{ font-size: 15px; }}
  .cards-grid {{
    grid-template-columns: 1fr;
    gap: 10px;
  }}
  .section-subheader {{ font-size: 12px; }}
  .placement-card {{ font-size: 12px; }}
  .card-meta {{ padding: 8px; }}
  .heatmap-wrap {{ display: none; }}
  .family-stack {{ display: block; }}
  #modal {{ padding: 8px; overflow: auto; align-items: flex-start; }}
  #modal img {{ max-width: none; max-height: none; width: auto; height: auto; }}
}}
</style>
</head>
<body>
  <header>
    <h1>fal.ai — Alibaba Model Placements</h1>
    <span class="ts">Scanned {scan_time} UTC · {total_placements} placements found{f' · {total_removals} removed' if total_removals else ''}</span>
    {compare_html}
  </header>
  <main>
    <div class="summary">
      <h2>Position Overview</h2>
      {heatmap_html}
      <div class="total-line">Tracking: Happy Horse 1.0/1.1, Wan 2.6, Wan 2.7 across {len(non_model_surfaces)} surfaces. Slot color: <span class="mini-badge slot-good">top 1/3</span> <span class="mini-badge slot-ok">middle</span> <span class="mini-badge slot-bad">bottom 1/3</span> · Change: <span class="change-badge change-up">&#9650; N</span> moved up <span class="change-badge change-down">&#9660; N</span> moved down <span class="change-badge change-new">NEW</span> added <span class="change-badge change-removed">REMOVED</span> gone</div>
    </div>
    {sections_html}
  </main>
  <div id="modal" onclick="closeModal()">
    <img id="modal-img" src="" alt="" onclick="event.stopPropagation()"/>
  </div>
  <script>
    function openModal(src) {{ event.stopPropagation(); document.getElementById('modal-img').src = src; document.getElementById('modal').classList.add('open'); }}
    function closeModal() {{ document.getElementById('modal').classList.remove('open'); }}
    document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
  </script>
</body>
</html>"""

    OUT.write_text(html)
    sz = OUT.stat().st_size / 1024 / 1024
    print(f"Wrote {OUT.name} ({sz:.1f} MB, {total_placements} placements, {len(ordered)} surfaces)")
    if prev_data:
        print(f"  Compared to previous scan from {prev_timestamp} UTC")
        if total_removals:
            print(f"  {total_removals} placement(s) removed since last scan")

if __name__ == "__main__":
    main()
