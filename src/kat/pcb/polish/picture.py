"""Pictures of a polishing run without a window (Pillow only): a case state (before, after,
both overlaid), the merit over the run, and, when the private map file is at hand, the
selection on a KiCad render of the board. ``report`` puts them into ``report.md``.

Case coordinates are mm with y up; pictures have y down. The colours are the inspection
window's (``gui`` uses ``COLORS`` too).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = {"board": "#f4f1e8", "edge": "#6b6b6b", "region": "#3a7bd5", "keepout": "#d9534f",
          "fixed": "#9a8f80", "hole": "#222222", "orig": "#c9a46a", "cur": "#c46a00",
          "snapped": "#2e8b57", "cand": "#d0021b", "env": "#f0ad4e", "anchor": "#1f4e79",
          "added": "#e91e63"}
LONGEST_PX = 1600          # longest side of the drawing area
MARGIN_MM = 3.0            # around the movement region (the context reaches that far)
PAD = 24                   # px around the drawing area
LEGEND_H = 56              # px below it


def font(size: int = 14):
    """Pillow's built-in font at ``size`` px (the bitmap font on Pillow < 10.1)."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """``#rrggbb`` -> an RGBA tuple."""
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha


def bbox(points) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of ``points``."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


class Canvas:
    """A picture of the case area ``box`` (x0, y0, x1, y1 case mm, y up) at ``ppm`` px/mm,
    a legend strip below it."""

    def __init__(self, box, ppm: float | None = None, legend: bool = True):
        self.box = box
        w, h = box[2] - box[0], box[3] - box[1]
        self.ppm = ppm or LONGEST_PX / max(w, h, 1e-6)
        self.w, self.h = int(w * self.ppm) + 2 * PAD, int(h * self.ppm) + 2 * PAD
        self.img = Image.new("RGBA", (self.w, self.h + (LEGEND_H if legend else 0)), (255, 255, 255, 255))
        self.draw = ImageDraw.Draw(self.img, "RGBA")

    def px(self, p) -> tuple[float, float]:
        """Case point (mm, y up) -> pixel."""
        return PAD + (p[0] - self.box[0]) * self.ppm, PAD + (self.box[3] - p[1]) * self.ppm

    def width(self, mm: float) -> int:
        return max(1, round(mm * self.ppm))

    def polygon(self, pts, fill=None, outline=None, line_px: int = 1):
        self.draw.polygon([self.px(p) for p in pts], fill=fill, outline=outline, width=line_px)

    def polyline(self, pts, color, mm: float | None = None, px: int | None = None):
        """A track: ``mm`` wide (or ``px``), round joins and caps."""
        w = px if px is not None else self.width(mm)
        q = [self.px(p) for p in pts]
        if len(q) > 1:
            self.draw.line(q, fill=color, width=w, joint="curve")
        r = w / 2
        for x, y in (q[0], q[-1]) if w > 2 else ():
            self.draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    def disc(self, c, r_mm: float | None = None, color=(0, 0, 0, 255), r_px: float | None = None, outline=None):
        x, y = self.px(c)
        r = r_px if r_px is not None else max(1.0, r_mm * self.ppm)
        self.draw.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=outline,
                          width=2 if outline else 0)

    def dashed(self, pts, color, px: int = 2, dash: float = 10.0):
        """A closed dashed outline."""
        q = [self.px(p) for p in pts] + [self.px(pts[0])]
        for (x0, y0), (x1, y1) in zip(q, q[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / dash))
            for i in range(0, n, 2):
                a, b = i / n, min(1.0, (i + 1) / n)
                self.draw.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                               fill=color, width=px)

    def label(self, at_px, text: str, color="#000000", size: int = 14):
        """``text`` centred at ``at_px`` on a white, half-transparent box."""
        f = font(size)
        l, t, r, b = self.draw.textbbox((0, 0), text, font=f)
        x, y = at_px[0] - (r - l) / 2, at_px[1] - (b - t) / 2
        self.draw.rectangle((x - 3, y - 2, x + r - l + 3, y + b - t + 2), fill=(255, 255, 255, 200))
        self.draw.text((x - l, y - t), text, fill=color, font=f)

    def frame(self):
        """Blank the margin around the drawing area (shapes reaching beyond the box) and
        outline the area."""
        white = (255, 255, 255, 255)
        for r in ((0, 0, self.w, PAD), (0, self.h - PAD, self.w, self.img.height), (0, 0, PAD, self.h),
                  (self.w - PAD, 0, self.w, self.h)):
            self.draw.rectangle(r, fill=white)
        self.draw.rectangle((PAD, PAD, self.w - PAD, self.h - PAD), outline=(170, 170, 170, 255))

    def title(self, text: str):
        self.draw.text((PAD, 4), text, fill="#000000", font=font(15))

    def scale_bar(self):
        """A bar of a round length at the bottom left of the drawing area."""
        target = (self.box[2] - self.box[0]) / 5
        mm = next(v for v in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1e9) if v >= target / 2)
        x, y = PAD + 8, self.h - 14
        self.draw.rectangle((x, y, x + mm * self.ppm, y + 5), fill="#000000")
        self.draw.text((x + mm * self.ppm + 8, y - 6), f"{mm:g} mm", fill="#000000", font=font(13))

    def legend(self, items: list[tuple[str, str]]):
        """Colour swatches with their meaning in the strip below the drawing."""
        x, y = PAD, self.h + 12
        f = font(13)
        for color, text in items:
            self.draw.rectangle((x, y + 3, x + 22, y + 13), fill=color, outline=(150, 150, 150, 255))
            x += 28
            self.draw.text((x, y), text, fill="#000000", font=f)
            x += self.draw.textlength(text, font=f) + 18
            if x > self.w - 160:
                x, y = PAD, y + 20

    def save(self, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        self.img.convert("RGB").save(out)
        return out


def case_box(case, group_ids) -> tuple[float, float, float, float]:
    """The area a state picture shows: movement region and the group's traces, plus
    ``MARGIN_MM``."""
    pts = list(case.region) + [p for t in group_ids for p in case.traces[t].points]
    x0, y0, x1, y1 = bbox(pts)
    return x0 - MARGIN_MM, y0 - MARGIN_MM, x1 + MARGIN_MM, y1 + MARGIN_MM


def _context(cv: Canvas, case, group_ids):
    col = COLORS
    cv.polygon(case.outline, fill=rgba(col["board"]), outline=rgba(col["edge"]), line_px=2)
    for cut in case.cutouts:
        cv.polygon(cut, fill=(255, 255, 255, 255), outline=rgba(col["edge"]), line_px=2)
    for k in case.keepouts.values():
        cv.polygon(k.points, fill=rgba(col["keepout"], 60), outline=rgba(col["keepout"]), line_px=2)
    fixed = rgba(col["fixed"])
    for o in case.copper.values():
        if o.kind == "polyline":
            cv.polyline(o.points, fixed, o.width)
        elif o.kind == "circle":
            cv.disc(o.center, o.radius, fixed)
        else:
            cv.polygon(o.points, fill=fixed)
    for t in case.traces.values():
        if t.id not in group_ids:
            cv.polyline(t.points, fixed, t.width)
    for h in case.holes.values():
        cv.disc(h.center, h.radius, rgba(col["hole"]))
    cv.dashed(case.region, rgba(col["region"]))


def _label_points(traces: dict, ids: list[str]) -> dict:
    """Where each trace's label goes: on its longest segment, staggered along it by the
    trace's position in the group so that neighbours' labels do not cover each other."""
    out = {}
    for k, t in enumerate(ids):
        pts = traces[t]
        i = max(range(len(pts) - 1), key=lambda j: math.dist(pts[j], pts[j + 1]))
        f = 0.25 + 0.5 * (k / (len(ids) - 1) if len(ids) > 1 else 0.5)
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        out[t] = (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
    return out


def state(case, group_ids: list[str], traces: dict, out: Path, title: str, labels: dict | None = None,
          snapped: dict | None = None, added: dict | None = None, original: bool = False,
          color: str = "cur") -> Path:
    """One PNG of the group's ``traces`` ({id: points}) in their context: ``snapped``
    ({id: segment indices}) drawn green, ``added`` ({id: vertex indices}) as pink dots,
    ``original`` also draws the input geometry faintly underneath, ``labels`` ({id: text},
    default the ids) name the traces. Returns ``out``."""
    col = COLORS
    cv = Canvas(case_box(case, group_ids))
    _context(cv, case, group_ids)
    if original:
        for t in group_ids:
            cv.polyline(case.traces[t].points, rgba(col["orig"], 150), case.traces[t].width)
    for t in group_ids:
        w = case.traces[t].width
        cv.polyline(traces[t], rgba(col[color]), w)
        for i in (snapped or {}).get(t, []):
            cv.polyline(traces[t][i:i + 2], rgba(col["snapped"]), px=max(2, cv.width(w * 0.45)))
    for t in group_ids:
        for p in (case.traces[t].points[0], case.traces[t].points[-1]):
            cv.disc(p, r_px=max(3.0, 0.08 * cv.ppm), color=rgba(col["anchor"]))
        for i in (added or {}).get(t, []):
            cv.disc(traces[t][i], r_px=6, color=rgba(col["added"]), outline=(255, 255, 255, 255))
    for t, p in _label_points(traces, group_ids).items():
        cv.label(cv.px(p), (labels or {}).get(t, t))
    cv.frame()
    cv.title(title)
    cv.scale_bar()
    items = [(col["board"], "board"), (col["fixed"], "fixed copper"), (col["hole"], "hole"),
             (col["keepout"], "keep-out"), (col["region"], "movement region (dashed)"),
             (col["anchor"], "fixed end")]
    if original:
        items.append((col["orig"], "input"))
    items.append((col[color], "polished" if color == "cur" else "selected traces"))
    if any((snapped or {}).values()):
        items.append((col["snapped"], "snapped to 45-degree multiple"))
    if any((added or {}).values()):
        items.append((col["added"], "added vertex"))
    cv.legend(items)
    return cv.save(out)


def merit_chart(events: list[dict], out: Path, best_seq: int | None = None) -> Path:
    """Line chart of the weighted merit (``total``, left axis) over solver iterations, the
    clearance shortfall (mm, right axis) when the run was recovering, ticks for accepted
    snaps / bends / rescues / simplifications and a dot at the exported event. Returns ``out``."""
    W, H, L, R, T, B = 1200, 520, 80, 90, 40, 90
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f = font(13)
    d.text((L, 10), "merit (weighted total, lower is better) over iterations", fill="black", font=font(15))
    xs = [e["iter"] for e in events]
    x0, x1 = min(xs), max(max(xs), min(xs) + 1)
    tot = [e["total"] for e in events]
    lo, hi = min(tot), max(tot)
    if hi - lo < 1e-12:
        lo, hi = lo - 0.5, hi + 0.5
    short = [e.get("shortfall", 0.0) or 0.0 for e in events]
    smax = max(short)
    pw, ph = W - L - R, H - T - B

    def X(i):
        return L + (i - x0) / (x1 - x0) * pw

    def Y(v, a=lo, b=hi):
        return T + ph - (v - a) / (b - a) * ph

    d.rectangle((L, T, L + pw, T + ph), outline="#999999")
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        d.line((L - 5, Y(v), L, Y(v)), fill="#666666")
        d.line((L, Y(v), L + pw, Y(v)), fill="#eeeeee")
        d.text((8, Y(v) - 8), f"{v:.4g}", fill="#333333", font=f)
    for k in range(5):
        i = x0 + (x1 - x0) * k / 4
        d.text((X(i) - 12, T + ph + 6), f"{i:.0f}", fill="#333333", font=f)
    d.text((L + pw / 2 - 30, T + ph + 24), "iteration", fill="#333333", font=f)
    d.line([(X(i), Y(v)) for i, v in zip(xs, tot)], fill=COLORS["region"], width=2)
    if smax > 0:
        d.line([(X(i), Y(v, 0, smax)) for i, v in zip(xs, short)], fill=COLORS["cand"], width=2)
        for k in range(5):
            v = smax * k / 4
            d.text((L + pw + 8, Y(v, 0, smax) - 8), f"{v:.3g}", fill=COLORS["cand"], font=f)
    marks = {"snap": COLORS["snapped"], "bend": COLORS["added"], "rescue": "#7b2cbf", "simplify": COLORS["cur"]}
    for e in events:
        if e["accepted"] and e["type"] in marks:
            d.line((X(e["iter"]), T + ph - 10, X(e["iter"]), T + ph), fill=marks[e["type"]], width=2)
    if best_seq is not None and 0 <= best_seq < len(events):
        e = events[best_seq]
        d.ellipse((X(e["iter"]) - 5, Y(e["total"]) - 5, X(e["iter"]) + 5, Y(e["total"]) + 5), outline="black", width=2)
    items = [(COLORS["region"], "merit"), ("#000000", "exported state (circle)")]
    if smax > 0:
        items.append((COLORS["cand"], "clearance shortfall, mm (right axis)"))
    items += [(c, f"accepted {k}") for k, c in marks.items()]
    x, y = L, H - 34
    for c, text in items:
        d.rectangle((x, y + 3, x + 22, y + 13), fill=c)
        d.text((x + 28, y), text, fill="black", font=f)
        x += 28 + d.textlength(text, font=f) + 18
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def board_views(mapping: dict, case, group_ids: list[str], out_dir: Path, labels: dict | None = None) -> dict:
    """KiCad renders (kicad-cli) of the board named in the private ``mapping``: the whole
    board with the movement region boxed (``board.png``) and the region with the selected
    traces highlighted (``board_region.png``), both on the case's copper layer plus the
    outline. Raises ``KatError`` when the board is gone or cannot be rendered. Returns
    {name: {png, px_per_mm, region_mm}}."""
    from ...env import KatError
    from .. import views
    pcb = Path(mapping["board"])
    if not pcb.exists():
        raise KatError(f"the board {pcb.name} of the extraction is not there any more")
    layers = [case.layer, "Edge.Cuts"]
    sel = rgba(COLORS["region"], 170)
    res = {}
    with tempfile.TemporaryDirectory() as tmp:
        svg = views.export_svg(pcb, layers, Path(tmp) / "board.svg")
        from ..board import Board
        area = views.board_area(Board(pcb))
        cx0, cy0, cx1, cy1 = case_box(case, group_ids)
        region = (cx0, -cy1, cx1, -cy0)                         # case (x, y) = board (x, -y)
        for name, box, ppm in (("board", area, min(40.0, LONGEST_PX / max(area[2] - area[0], area[3] - area[1]))),
                               ("board_region", region, LONGEST_PX / max(region[2] - region[0], region[3] - region[1]))):
            png = out_dir / f"{name}.png"
            info = views._render(svg, png, box, ppm)
            with Image.open(png) as im:
                img = im.convert("RGBA")
            sx, sy = img.width / (box[2] - box[0]), img.height / (box[3] - box[1])
            d = ImageDraw.Draw(img, "RGBA")

            def px(p, box=box, sx=sx, sy=sy):
                return (p[0] - box[0]) * sx, (-p[1] - box[1]) * sy

            if name == "board":
                q = [px(p) for p in case.region]
                d.polygon(q, outline=rgba(COLORS["cand"]), width=3)
            for t in group_ids:
                tr = case.traces[t]
                d.line([px(p) for p in tr.points], fill=sel, width=max(3, round(tr.width * sx)), joint="curve")
            if name == "board_region":
                for t, p in _label_points({t: case.traces[t].points for t in group_ids}, group_ids).items():
                    text = (labels or {}).get(t, t)
                    x, y = px(p)
                    f = font(14)
                    l, tp, r, b = d.textbbox((0, 0), text, font=f)
                    d.rectangle((x - (r - l) / 2 - 3, y - 9, x + (r - l) / 2 + 3, y + 9), fill=(255, 255, 255, 210))
                    d.text((x - (r - l) / 2 - l, y - (b - tp) / 2 - tp), text, fill="black", font=f)
            img.convert("RGB").save(png)
            res[name] = {"png": str(png), "px_per_mm": info["px_per_mm"], "region_mm": info["region_mm"]}
    return res


__all__ = ["COLORS", "Canvas", "board_views", "case_box", "merit_chart", "state"]
