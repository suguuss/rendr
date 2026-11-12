"""One image per copper layer in KiCad's "highlighted layer" look: the layer in full
colour on top, the other copper layers dimmed underneath, the board outline.

1. Copy the .kicad_pcb (+ .kicad_pro / .kicad_dru) to a temp folder; refill zones
   there (kicad-cli pcb drc --refill-zones --save-board) or strip the fills.
   The board file itself is never written.
2. kicad-cli pcb export svg --black-and-white --page-size-mode 2 per layer
   (board area only, so every layer has the same size and alignment).
3. Rasterise (wx.svg, else rsvg-convert), turn into a copper mask, colourise and
   stack with Pillow.
"""
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageOps

from .common import Job, JobError, block_end, font, overview, run

EDGE_COLOUR = "#D0D2CD"
BACKGROUND = "#001023"
MARGIN = 0.04  # empty border around the board, fraction of the image width
BIGGEST_MIN_SHARE = 0.2  # a "biggest pour" must cover this much of the board to be hidden


@dataclass
class CopperOptions:
    width: int = 2400           # px
    dim: float = 0.18           # opacity of the other layers, 0..1
    zones: str = "refill"       # refill | saved | hidden
    hide_biggest: bool = False  # hide each layer's biggest pour (planes), keep the small ones
    overview: bool = True
    gif: bool = False
    gif_delay: int = 1000       # ms per frame
    gif_width: int = 1200


def _cut(s, spans):
    """s without the given (start, end) spans."""
    out, i = [], 0
    for a, b in sorted(spans):
        out.append(s[i:a])
        i = b
    out.append(s[i:])
    return "".join(out)


def strip_zone_fills(path):
    """Remove every (filled_polygon ...) block so zones plot as nothing (tracks, pads, vias stay)."""
    with open(path, encoding="utf-8") as f:
        s = f.read()
    spans, i = [], 0
    while (j := s.find("(filled_polygon", i)) >= 0:
        i = block_end(s, j) + 1
        spans.append((j, i))
    with open(path, "w", encoding="utf-8") as f:
        f.write(_cut(s, spans))
    return len(spans)


_ZONE = re.compile(r"\(zone\s")
_NET = re.compile(r'\((?:net_name|net)\s+(?:\d+\s+)?"((?:[^"\\]|\\.)*)"\)')
_LAYER = re.compile(r'\(layer\s+"([^"]+)"')
_XY = re.compile(r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def _area(pts):
    """Shoelace area (KiCad stores fills as fractured polygons: no holes)."""
    a = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        a += x0 * y1 - x1 * y0
    return abs(a) / 2


def strip_biggest_pours(path, min_share=BIGGEST_MIN_SHARE):
    """On each copper layer, remove the fill of the zone with the largest filled
    area, if it covers at least min_share of the board (the extent of all fills).
    Smaller pours stay. Returns {layer: net name of the hidden pour}."""
    with open(path, encoding="utf-8") as f:
        s = f.read()
    per_layer = {}  # layer -> {zone start: [area, net, [spans]]}
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    for m in _ZONE.finditer(s):
        z0 = m.start()
        z1 = block_end(s, z0)
        body = s[z0:z1]
        net = _NET.search(body)
        net = net.group(1).rsplit("/", 1)[-1] if net else "?"
        i = 0
        while (j := body.find("(filled_polygon", i)) >= 0:
            i = block_end(body, j) + 1
            poly = body[j:i]
            layer = _LAYER.search(poly)
            pts = [(float(x), float(y)) for x, y in _XY.findall(poly)]
            if not layer or len(pts) < 3:
                continue
            for x, y in pts:
                xmin, xmax, ymin, ymax = min(xmin, x), max(xmax, x), min(ymin, y), max(ymax, y)
            z = per_layer.setdefault(layer.group(1), {}).setdefault(z0, [0.0, net, []])
            z[0] += _area(pts)
            z[2].append((z0 + j, z0 + i))
    if not per_layer:
        return {}
    board = (xmax - xmin) * (ymax - ymin)
    spans, hidden = [], {}
    for layer, zones in per_layer.items():
        area, net, zspans = max(zones.values(), key=lambda z: z[0])
        if area >= min_share * board:
            spans += zspans
            hidden[layer] = net
    with open(path, "w", encoding="utf-8") as f:
        f.write(_cut(s, spans))
    return hidden


def rasterise(svg, width):
    """SVG -> RGBA image `width` px wide."""
    try:
        import wx.svg
    except ImportError:
        wx = None
    if wx is not None:
        img = wx.svg.SVGimage.CreateFromFile(svg)
        if img.width > 0:
            scale = width / img.width
            h = round(img.height * scale)
            buf = bytearray(width * h * 4)
            img.RasterizeToBuffer(buf, 0, 0, scale, width, h, width * 4)
            return Image.frombuffer("RGBA", (width, h), bytes(buf), "raw", "RGBA", 0, 1)
    rsvg = shutil.which("rsvg-convert")
    if not rsvg:
        raise JobError("cannot rasterise SVG: neither wxPython (wx.svg) nor rsvg-convert is available")
    png = svg[:-4] + ".png"
    run([rsvg, "-w", str(width), "-o", png, svg], Job())
    return Image.open(png).convert("RGBA")


def export_mask(cli, pcb, layer, workdir, width, mirror, job):
    """'L' mask (255 = copper) of one layer."""
    svg = os.path.join(workdir, layer.replace(".", "_") + ("_m" if mirror else "") + ".svg")
    cmd = [cli, "pcb", "export", "svg", "--mode-single", "--layers", layer,
           "--black-and-white", "--page-size-mode", "2", "--exclude-drawing-sheet",
           "--drill-shape-opt", "2", "-o", svg]
    if mirror:
        cmd.append("--mirror")
    run(cmd + [pcb], job)
    im = rasterise(svg, width)
    # black-and-white plot: copper is dark and opaque, holes/background white or transparent
    return ImageChops.multiply(im.getchannel("A"), ImageChops.invert(im.convert("L")))


def compose(layers, masks, highlight, dim):
    size = masks["Edge.Cuts"].size
    out = Image.new("RGB", size, BACKGROUND)
    for l in reversed(layers):  # bottom-up, so the stack order looks natural
        if l.name != highlight.name:
            out.paste(Image.new("RGB", size, l.colour), (0, 0), masks[l.name].point(lambda v: int(v * dim)))
    out.paste(Image.new("RGB", size, highlight.colour), (0, 0), masks[highlight.name])
    out.paste(Image.new("RGB", size, EDGE_COLOUR), (0, 0), masks["Edge.Cuts"])
    return out


def caption(img, title, sub, indent):
    """Title (bold) and subtitle above the image, left-aligned with the board (indent px)."""
    ts, ss = max(20, img.width // 30), max(14, img.width // 48)
    gap = ts // 3
    h = indent // 2 + ts + gap + ss
    out = Image.new("RGB", (img.width, img.height + h), BACKGROUND)
    out.paste(img, (0, h))
    d = ImageDraw.Draw(out)
    d.text((indent, indent // 2), title, fill="white", font=font(ts, bold=True))
    d.text((indent, indent // 2 + ts + gap), sub, fill="#9aa3ad", font=font(ss))
    return out


def export_copper(cli, pcb, all_layers, selected, out_dir, opt, job):
    """all_layers: the whole stack (dimmed underneath); selected: names to write. Returns written paths."""
    pcb = os.path.abspath(pcb)
    sel = [l for l in all_layers if l.name in selected]
    if not sel:
        raise JobError("no copper layer selected")
    os.makedirs(out_dir, exist_ok=True)
    mirrored = any(l.from_below for l in sel)
    steps = (len(all_layers) + 1) * (2 if mirrored else 1) + len(sel) + (opt.zones != "saved") + opt.overview + opt.gif
    done = 0

    def step():
        nonlocal done
        done += 1
        job.progress(done, steps)

    written = []
    with tempfile.TemporaryDirectory(prefix="rendr_cu_") as tmp:
        base = os.path.splitext(os.path.basename(pcb))[0]
        for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            src = os.path.join(os.path.dirname(pcb), base + ext)
            if os.path.isfile(src):
                shutil.copy2(src, tmp)
        tpcb = os.path.join(tmp, base + ".kicad_pcb")
        if opt.zones == "hidden":
            job.log(f"Removed {strip_zone_fills(tpcb)} zone fills in a temp copy (tracks, pads, vias only)")
            step()
        elif opt.zones == "refill":
            job.log("Refilling zones in a temp copy...")
            run([cli, "pcb", "drc", "--refill-zones", "--save-board", "-o", os.path.join(tmp, "drc.rpt"), tpcb], job)
            step()
        hidden_pours = {}
        if opt.hide_biggest and opt.zones != "hidden":
            hidden_pours = strip_biggest_pours(tpcb)
            for layer, net in hidden_pours.items():
                job.log(f"Hiding the {net} pour on {layer}")

        def masks_for(mirror):
            ms = {}
            for name in [l.name for l in all_layers] + ["Edge.Cuts"]:
                job.log(f"Plotting {name}{' (mirrored)' if mirror else ''}")
                ms[name] = export_mask(cli, tpcb, name, tmp, opt.width, mirror, job)
                step()
            sizes = {m.size for m in ms.values()}
            if len(sizes) != 1:
                raise JobError(f"layer images differ in size: {sizes}")
            return ms

        masks = masks_for(False)
        masks_m = masks_for(True) if mirrored else None

    tiles = []
    for l in sel:
        job.check()
        ms = masks_m if l.from_below else masks
        img = compose(all_layers, ms, l, opt.dim)
        border = round(img.width * MARGIN)
        img = ImageOps.expand(img, border=border, fill=BACKGROUND)
        sub = l.caption + (" (zones hidden)" if opt.zones == "hidden" else "")
        if l.name in hidden_pours:
            sub += f" ({hidden_pours[l.name]} pour hidden)"
        img = caption(img, f"{l.position}. {l.name}" + (" (seen from below)" if l.from_below else ""), sub, border)
        path = os.path.join(out_dir, f"{l.position}_{l.name.replace('.', '_')}.png")
        img.save(path, optimize=True)
        tiles.append(img)
        written.append(path)
        job.log(f"Wrote {path}")
        step()

    if opt.overview and len(tiles) > 1:
        path = os.path.join(out_dir, "0_overview.png")
        overview(tiles, opt.width // 3, BACKGROUND).save(path, optimize=True)
        written.append(path)
        job.log(f"Wrote {path}")
        step()

    if opt.gif:
        gw = min(opt.gif_width, tiles[0].width)
        gh = round(tiles[0].height * gw / tiles[0].width)
        frames = [t.resize((gw, gh), Image.LANCZOS).quantize(colors=128, method=Image.Quantize.MEDIANCUT,
                                                              dither=Image.Dither.NONE) for t in tiles]
        path = os.path.join(out_dir, "copper_layers.gif")
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=opt.gif_delay, loop=0, optimize=True)
        written.append(path)
        job.log(f"Wrote {path} ({len(frames)} frames, {opt.gif_delay} ms each)")
        step()
    return written
