"""Stackup cross-section: the board's layer stack as one picture, a section through
the board on the left (a plated through via included), a table on the right
(type, material, thickness, dielectric constant, loss tangent), leader lines between.

Read from the (stackup ...) block of the .kicad_pcb (Board Setup > Board Stackup).
A board without one (stackup never edited, or made before KiCad 5.1) gets KiCad's
default: 35 um copper, the board thickness split evenly between the dielectrics.
Drawn with Pillow, no kicad-cli: fast enough for a live preview in the window.
"""
import os
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from .boardinfo import colour_for
from .common import JobError, block_end, font


@dataclass
class StackupOptions:
    width: int = 2400        # px
    light: bool = False      # white background (documents) instead of the dark one
    via: bool = True         # draw a plated through via in the section
    captions: bool = True    # copper layer captions (from the Copper layers tab)


@dataclass
class Item:
    name: str                # F.Cu, dielectric 1, F.Mask
    kind: str                # copper | core | prepreg | mask
    thickness: float         # mm
    material: str = ""
    epsilon_r: str = ""
    loss_tangent: str = ""
    colour: str = ""         # #RRGGBB from the stackup, "" = default for the kind


@dataclass
class Stackup:
    items: list = field(default_factory=list)  # top to bottom
    thickness: float = 1.6   # board thickness setting, mm
    finish: str = ""
    impedance: bool = False  # "impedance controlled" (dielectric constraints) ticked
    default: bool = False    # no stackup in the file: KiCad's default

    @property
    def total(self):
        return sum(i.thickness for i in self.items)

    @property
    def copper_count(self):
        return sum(i.kind == "copper" for i in self.items)


# KiCad's named stackup colours, approximately
MASK_COLOURS = {"green": "#1F6B3A", "light green": "#4FA35E", "saturated green": "#138A3C",
                "red": "#9E1E22", "light red": "#D04545", "red/orange": "#C8501E", "yellow": "#D2B21C",
                "light yellow": "#E6D45C", "blue": "#1E46A0", "light blue": "#4A80D0", "black": "#1C1C1C",
                "white": "#E6E6E6", "purple": "#58287E", "light purple": "#8C5CB4"}
DIELECTRIC_COLOURS = {"fr4 natural": "#8E8A55", "fr4 natural, dark": "#5E5A35", "polyimide": "#B07A2A",
                      "phenolic natural": "#9A6A3A", "ptfe natural": "#D2D2C8", "pi": "#B07A2A",
                      "aluminum": "#B8BCC0"}
DEFAULT_COLOUR = {"mask": "#1F6B3A", "core": "#8E8A55", "prepreg": "#8E8A55"}
BARREL = "#C8803F"
FINISH_COLOURS = (("enepig", "#D8B44A"), ("enig", "#D8B44A"), ("gold", "#D8B44A"),
                  ("hal", "#C4C8CC"), ("hasl", "#C4C8CC"), ("tin", "#C4C8CC"), ("silver", "#D6DADE"))
THEMES = {False: dict(bg="#001023", fg="#FFFFFF", muted="#9AA3AD", rule="#22344D", warn="#F0A040"),
          True: dict(bg="#FFFFFF", fg="#1B1F24", muted="#5F6B78", rule="#D5DAE0", warn="#B35C00")}
TYPE_LABEL = {"copper": "Copper", "core": "Core", "prepreg": "Prepreg", "mask": "Solder mask"}

# ------------------------------------------------------------------ reading
_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def _parse(s):
    """One s-expression -> nested lists of strings (quotes removed)."""
    stack = [[]]
    for t in _TOK.findall(s):
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t[1:-1].replace('\\"', '"') if t[0] == '"' else t)
    return stack[0][0] if stack[0] else []


def _block(s, head, start=0, end=None):
    """Text of the first (head ...) block in s[start:end], or ""."""
    i = s.find("(" + head, start, end)
    while i >= 0 and s[i + 1 + len(head)] not in " \t\r\n)":
        i = s.find("(" + head, i + 1, end)
    return s[i:block_end(s, i) + 1] if i >= 0 else ""


def _colour(name, table, kind):
    if name.startswith("#") and len(name) >= 7:
        return name[:7]
    n = name.lower().strip()
    return table.get(n) or table.get(n.replace("matte ", "")) or DEFAULT_COLOUR[kind]


def _copper_names(s):
    """Copper layers of the (layers ...) block, top to bottom."""
    names = set(re.findall(r'\(\d+\s+"?([^"\s()]+\.Cu)"?\s', _block(s, "layers")))
    inner = sorted((n for n in names if n.startswith("In")), key=lambda n: int(n[2:-3]))
    return ["F.Cu"] + inner + (["B.Cu"] if "B.Cu" in names else [])


def default_stackup(copper_names, thickness):
    """KiCad's default stackup for this many copper layers."""
    n = len(copper_names)
    cu = 0.035
    d = max(thickness - n * cu, 0.1) / max(n - 1, 1)
    items = [Item("F.Mask", "mask", 0.01, epsilon_r="3.3")]
    for k, name in enumerate(copper_names):
        items.append(Item(name, "copper", cu))
        if k < n - 1:
            items.append(Item(f"dielectric {k + 1}", "core" if k % 2 == 0 else "prepreg", d, "FR4", "4.5", "0.02"))
    items.append(Item("B.Mask", "mask", 0.01, epsilon_r="3.3"))
    return Stackup(items, thickness, default=True)


def read_stackup(pcb):
    with open(pcb, encoding="utf-8") as f:
        s = f.read()
    general = _block(s, "general")
    m = re.search(r"\(thickness\s+([\d.]+)", general)
    thickness = float(m.group(1)) if m else 1.6
    setup = _block(s, "setup")
    block = _parse(_block(setup, "stackup")) if setup else []
    st = Stackup(thickness=thickness)
    for e in block[1:]:
        if not isinstance(e, list) or len(e) < 2:
            continue
        if e[0] == "copper_finish":
            st.finish = "" if e[1] == "None" else e[1]
        elif e[0] == "dielectric_constraints":
            st.impedance = e[1] == "yes"
        elif e[0] == "layer":
            subs = [{}]  # a dielectric can hold several sublayers (addsublayer)
            for f in e[2:]:
                if f == "addsublayer" or f == ["addsublayer"]:
                    subs.append({})
                elif isinstance(f, list) and len(f) >= 2:
                    subs[-1][f[0]] = f[1]
            typ = subs[0].get("type", "").lower()
            if typ == "copper":
                kind = "copper"
            elif typ.endswith("solder mask"):
                kind = "mask"
            elif e[1].startswith("dielectric"):
                kind = "prepreg" if typ == "prepreg" else "core"
            else:
                continue  # silkscreen, paste: no thickness
            for k, sub in enumerate(subs):
                name = e[1] + (f" ({k + 1}/{len(subs)})" if len(subs) > 1 else "")
                try:
                    t = float(sub.get("thickness", 0.035 if kind == "copper" else 0))
                except ValueError:
                    t = 0.0
                colour = ""
                if kind != "copper" and sub.get("color", subs[0].get("color")):
                    colour = _colour(sub.get("color", subs[0].get("color")),
                                     MASK_COLOURS if kind == "mask" else DIELECTRIC_COLOURS, kind)
                tan = sub.get("loss_tangent", "")
                st.items.append(Item(name, kind, t, sub.get("material", ""), sub.get("epsilon_r", ""),
                                     "" if tan in ("0", "0.0") else tan, colour))
    if st.copper_count == 0:
        names = _copper_names(s)
        if len(names) < 2:
            raise JobError("no copper layers found in the board file")
        return default_stackup(names, thickness)
    return st


# ------------------------------------------------------------------ drawing
def _mm(t):
    return (f"{t:.4f}".rstrip("0").rstrip(".") or "0") + " mm"


def _fit(d, text, f, w):
    if d.textlength(text, font=f) <= w:
        return text
    while text and d.textlength(text + "…", font=f) > w:
        text = text[:-1]
    return text + "…" if text else ""


def _mix(a, b, t):
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def _fill(it):
    if it.kind == "copper":
        return colour_for(it.name)
    c = it.colour or DEFAULT_COLOUR[it.kind]
    return _mix(c, "#FFFFFF", 0.22) if it.kind == "prepreg" else c


def _band_heights(items, height, pitch):
    """Pixel height of each band: copper and mask at a readable minimum,
    dielectrics share the rest in proportion to their thickness."""
    total = sum(i.thickness for i in items) or 1.0
    k = height / total
    mins = {"copper": max(4.0, pitch * 0.24), "mask": max(3.0, pitch * 0.16)}
    hs = [max(mins[i.kind], i.thickness * k) if i.kind in mins else None for i in items]
    rest = height - sum(h for h in hs if h is not None)
    diel = sum(i.thickness for i, h in zip(items, hs) if h is None) or 1.0
    return [h if h is not None else max(pitch * 0.5, i.thickness * rest / diel) for i, h in zip(items, hs)]


def draw(st, opt, title, captions=None):
    """The stackup picture (PIL RGB image, opt.width px wide)."""
    W = opt.width
    th = THEMES[opt.light]
    captions = captions or {}
    M = round(W * 0.04)
    slab_w = round(W * 0.24)
    gap = round(W * 0.07)
    tx = M + slab_w + gap
    table_w = W - tx - M
    items = st.items
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    rows = []
    for it in items:
        cap = captions.get(it.name, "") if it.kind == "copper" and opt.captions else ""
        rows.append([it.name, cap, TYPE_LABEL[it.kind], it.material, _mm(it.thickness), it.epsilon_r, it.loss_tangent])
    heads = ["Layer", "", "Type", "Material", "Thickness", "εr", "tan δ"]
    keep = [c for c in range(len(heads)) if c in (0, 2, 4) or any(r[c] for r in rows)]
    right = {4, 5, 6}  # numbers, right-aligned

    # biggest font (down to 60 %) whose table fits whole, else the captions are cut
    def layout(fs):
        f, fb = font(fs), font(fs, bold=True)
        widths = {c: max(measure.textlength(r[c], font=fb if c == 0 else f) for r in rows + [heads])
                  + round(fs * 1.3) for c in keep}
        widths[0] += round(fs * 0.75) + fs // 2  # swatch before the layer name
        return widths

    top_fs = max(10, W // 52)
    for fs in range(top_fs, max(8, round(top_fs * 0.6)) - 1, -1):
        widths = layout(fs)
        if sum(widths.values()) <= table_w:
            break
    else:
        fixed = sum(w for c, w in widths.items() if c != 1)
        while 1 in widths and fixed + fs * 8 > table_w and fs > 8:
            fs -= 1
            widths = layout(fs)
            fixed = sum(w for c, w in widths.items() if c != 1)
        if 1 in widths:
            widths[1] = max(fs * 8, table_w - fixed)
    sw = round(fs * 0.75)
    pitch = round(fs * 1.9)

    ts, ss = max(20, W // 30), max(14, W // 48)
    head_h = M // 2 + ts + ts // 3 + ss + M
    body_top = head_h
    rows_top = body_top + pitch  # first data row (header row above it)
    rows_h = pitch * len(rows)
    hs = _band_heights(items, rows_h, pitch)
    slab_h = sum(hs)
    slab_top = rows_top + (rows_h - slab_h) / 2
    foot = []
    if st.default:
        foot.append(("No stackup in the board file (Board Setup > Board Stackup): KiCad's default shown.",
                     th["warn"]))
    elif abs(st.total - st.thickness) > 0.01 * st.thickness:
        foot.append((f"Stackup adds up to {_mm(st.total)}, the board thickness setting is {_mm(st.thickness)}.",
                     th["warn"]))
    body_bottom = max(rows_top + rows_h, slab_top + slab_h)
    H = round(body_bottom + (pitch * 0.6 + len(foot) * pitch if foot else 0) + M)

    img = Image.new("RGB", (W, H), th["bg"])
    d = ImageDraw.Draw(img)

    # header
    d.text((M, M // 2), title, fill=th["fg"], font=font(ts, bold=True))
    sub = [f"{st.copper_count} copper layers", f"{st.total:.2f} mm"]
    if st.finish:
        sub.append(st.finish)
    if st.impedance:
        sub.append("impedance controlled")
    d.text((M, M // 2 + ts + ts // 3), "  ·  ".join(sub), fill=th["muted"], font=font(ss))

    # section through the board
    sx0, sx1 = M, M + slab_w
    edges = [slab_top]
    for h in hs:
        edges.append(edges[-1] + h)
    edges = [round(e) for e in edges]
    lw = max(1, round(W / 1200))
    for it, y0, y1 in zip(items, edges, edges[1:]):
        band = Image.new("RGB", (slab_w, max(1, y1 - y0)), _fill(it))
        if it.kind == "prepreg":  # glass weave hint
            bd = ImageDraw.Draw(band)
            step = max(6, fs // 2)
            for x in range(-band.height, slab_w, step):
                bd.line((x, band.height, x + band.height, 0), fill=_mix(_fill(it), "#000000", 0.18), width=1)
        img.paste(band, (sx0, y0))
    cu = [k for k, it in enumerate(items) if it.kind == "copper"]
    if opt.via and len(cu) >= 2:
        hw = round(slab_w * 0.09)         # drill
        ring = round(slab_w * 0.07)       # annular ring
        wall = max(3, round(slab_w * 0.02))
        hx0 = sx0 + round(slab_w * 0.62)
        hx1 = hx0 + hw
        top, bot = edges[cu[0]], edges[cu[-1] + 1]
        finish = next((c for k, c in FINISH_COLOURS if k in st.finish.lower()), None)
        for k, it in enumerate(items):  # mask opening around the via, finish on the exposed pad
            if it.kind == "mask" and (k < cu[0] or k > cu[-1]):
                d.rectangle((hx0 - ring - wall, edges[k], hx1 + ring + wall - 1, edges[k + 1] - 1), fill=th["bg"])
                if finish:
                    y = edges[k + 1] - max(2, lw * 2) if k < cu[0] else edges[k]
                    d.rectangle((hx0 - ring - wall, y, hx1 + ring + wall - 1, y + max(2, lw * 2) - 1), fill=finish)
        d.rectangle((hx0 - wall, top, hx1 + wall - 1, bot - 1), fill=BARREL)
        d.rectangle((hx0, top, hx1 - 1, bot - 1), fill=th["bg"])
    d.rectangle((sx0, edges[0], sx1 - 1, edges[-1] - 1), outline=th["rule"], width=lw)

    # table header
    f, fb, fh = font(fs), font(fs, bold=True), font(round(fs * 0.85))
    xs, x = {}, tx
    for c in keep:
        xs[c] = x
        x += widths[c]

    def cell(c, y, text, fnt, fill):
        w = widths[c] - round(fs * 1.3)
        text = _fit(d, text, fnt, w)
        if c in right:
            d.text((xs[c] + w, y), text, fill=fill, font=fnt, anchor="rm")
        else:
            d.text((xs[c] + (sw + fs // 2 if c == 0 else 0), y), text, fill=fill, font=fnt, anchor="lm")

    yh = body_top + pitch // 2
    for c in keep:
        cell(c, yh, heads[c], fh, th["muted"])
    d.line((tx, rows_top - lw, W - M, rows_top - lw), fill=th["rule"], width=lw)

    # rows and leaders
    for k, (it, r) in enumerate(zip(items, rows)):
        yr = rows_top + k * pitch + pitch // 2
        yb = (edges[k] + edges[k + 1]) / 2
        lead = colour_for(it.name) if it.kind == "copper" else th["muted"]
        d.line([(sx1 + lw, yb), (sx1 + gap * 0.25, yb), (tx - gap * 0.25, yr), (tx - fs * 0.4, yr)],
               fill=lead, width=lw, joint="curve")
        d.rectangle((tx, yr - sw // 2, tx + sw - 1, yr - sw // 2 + sw - 1), fill=_fill(it), outline=th["rule"])
        for c in keep:
            cell(c, yr, r[c], fb if c == 0 and it.kind == "copper" else f,
                 th["fg"] if c in (0, 1, 4) or it.kind != "copper" else th["muted"])
    d.line((tx, rows_top + rows_h, W - M, rows_top + rows_h), fill=th["rule"], width=lw)

    y = body_bottom + pitch * 0.6
    for text, colour in foot:
        d.text((M, y + pitch // 2), text, fill=colour, font=f, anchor="lm")
        y += pitch
    return img


def export_stackup(pcb, out_dir, opt, job, captions=None):
    """Write <out_dir>/stackup.png. captions: {copper layer name: text}. Returns written paths."""
    pcb = os.path.abspath(pcb)
    job.log("Reading the stackup...")
    st = read_stackup(pcb)
    if st.default:
        job.log("No stackup in the board file: drawing KiCad's default")
    job.check()
    img = draw(st, opt, os.path.splitext(os.path.basename(pcb))[0] + " stackup", captions)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "stackup.png")
    img.save(path, optimize=True)
    job.log(f"Wrote {path}")
    job.progress(1, 1)
    return [path]
