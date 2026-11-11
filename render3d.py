"""Realistic 3D renders of the board from several camera angles (kicad-cli pcb render).

The board is rendered in place (3D model paths are often relative to the project,
${KIPRJMOD}/...); kicad-cli pcb render only reads it. kicad-cli runs with a temp
copy of the KiCad config where every footprint type (THT, SMD, virtual) is shown.
"""
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass

from .common import JobError, font, grid_cols, run

# name: (description, kicad-cli camera arguments)
VIEWS = {
    "top":             ("Top, straight down",               ["--side", "top", "--zoom", "0.85"]),
    "bottom":          ("Bottom, straight up",              ["--side", "bottom", "--zoom", "0.85"]),
    "iso_rear":        ("Rear edge, from above",            ["--perspective", "--rotate", "-55,0,180", "--zoom", "1.0"]),
    "iso_rear_left":   ("Rear-left corner",                 ["--perspective", "--rotate", "-50,0,135", "--zoom", "0.78"]),
    "iso_rear_right":  ("Rear-right corner",                ["--perspective", "--rotate", "-50,0,-135", "--zoom", "0.78"]),
    "iso_front_left":  ("Front-left corner",                ["--perspective", "--rotate", "-45,0,45", "--zoom", "0.78"]),
    "iso_front_right": ("Front-right corner",               ["--perspective", "--rotate", "-45,0,-45", "--zoom", "0.78"]),
    "rear_panel":      ("Rear edge, low-angle close-up",    ["--perspective", "--rotate", "-75,0,180", "--zoom", "1.0"]),
}
DEFAULT_VIEWS = ["top", "bottom", "iso_front_left", "iso_rear_right"]
STOCK_MODEL_DIRS = ["/usr/share/kicad/3dmodels", "/usr/local/share/kicad/3dmodels"]
SHEET_BG = "#15171c"
CARD_BG = "#2a2d34"      # behind transparent renders
CARD_W = 720             # px per view on the overview sheet
GUTTER = 28


@dataclass
class RenderOptions:
    quality: str = "high"        # basic | high
    floor: bool = True           # floor, shadows, post-processing
    width: int = 1920
    height: int = 1280
    transparent: bool = False    # PNG only
    format: str = "png"          # png | jpg
    overview: bool = True


def user_config_dir():
    """Folder holding KiCad's per-version config folders (10.0, 9.0, ...)."""
    if os.environ.get("KICAD_CONFIG_HOME"):
        return os.environ["KICAD_CONFIG_HOME"]
    try:
        import pcbnew
        return os.path.dirname(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath())
    except Exception:
        pass
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/kicad")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kicad")


def _temp_config():
    """Copy of the user's KiCad config with every footprint type visible in 3D."""
    tmp = tempfile.mkdtemp(prefix="rendr_cfg_")
    src = user_config_dir()
    vers = sorted((d for d in os.listdir(src) if d[:1].isdigit()),
                  key=lambda v: [int(x) for x in v.split(".") if x.isdigit()]) if os.path.isdir(src) else []
    ver = vers[-1] if vers else "10.0"
    if vers:
        shutil.copytree(os.path.join(src, ver), os.path.join(tmp, ver), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lck", "backups"))
    else:
        os.makedirs(os.path.join(tmp, ver))
    vj = os.path.join(tmp, ver, "3d_viewer.json")
    cfg = {}
    if os.path.isfile(vj):
        with open(vj, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.setdefault("render", {}).update({"show_footprints_normal": True, "show_footprints_insert": True,
                                         "show_footprints_virtual": True})
    with open(vj, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return tmp


def render_views(cli, pcb, names, out_dir, opt, job):
    """Render the named views. Returns written paths."""
    pcb = os.path.abspath(pcb)
    bad = [n for n in names if n not in VIEWS]
    if bad:
        raise JobError(f"unknown view(s): {', '.join(bad)}")
    if not names:
        raise JobError("no view selected")
    os.makedirs(out_dir, exist_ok=True)
    fmt = opt.format
    transparent = opt.transparent and fmt == "png"

    cfg = _temp_config()
    env = dict(os.environ, KICAD_CONFIG_HOME=cfg)
    stock = next((d for d in STOCK_MODEL_DIRS if os.path.isdir(d)), None)
    if stock:
        for v in ("KICAD9_3DMODEL_DIR", "KICAD10_3DMODEL_DIR"):
            env.setdefault(v, stock)

    steps = len(names) + (opt.overview and len(names) > 1)
    results = []
    try:
        for i, n in enumerate(names, 1):
            desc, cam = VIEWS[n]
            path = os.path.join(out_dir, f"{i}_{n}.{fmt}")
            cmd = [cli, "pcb", "render", "-o", path, "-w", str(opt.width), "-h", str(opt.height),
                   "--quality", opt.quality, "--background", "transparent" if transparent else "opaque"] + cam
            if opt.floor:
                cmd.append("--floor")
            job.log(f"Rendering {n} ({opt.quality})...")
            run(cmd + [pcb], job, env=env)
            if not os.path.isfile(path):
                raise JobError(f"kicad-cli wrote no image for {n}")
            results.append((n, desc, path))
            job.log(f"Wrote {path}")
            job.progress(i, steps)
    finally:
        shutil.rmtree(cfg, ignore_errors=True)

    written = [p for _, _, p in results]
    if opt.overview and len(results) > 1:
        path = os.path.join(out_dir, f"0_overview.{fmt}")
        sheet = overview_sheet(results, os.path.splitext(os.path.basename(pcb))[0])
        sheet.save(path, quality=92) if fmt == "jpg" else sheet.save(path, optimize=True)
        written.append(path)
        job.log(f"Wrote {path}")
        job.progress(steps, steps)
    return written


def content_box(im):
    """Bounding box of the board in a render: what differs from the background
    (KiCad's background is a vertical gradient: each row is one colour)."""
    from PIL import ImageChops
    w, h = im.size
    if im.getpixel((0, 0))[3] < 255:  # transparent background
        return im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    rgb = im.convert("RGB")
    bg = rgb.crop((0, 0, 1, h)).resize((w, h))
    return ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 18 else 0).getbbox()


def overview_sheet(results, title):
    """All views as rounded cards on a dark sheet, captioned, under a header.
    Every view gets the same crop (the union of the board areas, plus a margin),
    so the cards line up and KiCad's background gradient stays intact."""
    from PIL import Image, ImageDraw
    imgs = [Image.open(p).convert("RGBA") for _, _, p in results]
    w, h = imgs[0].size
    boxes = [b for b in (content_box(i) for i in imgs if i.size == (w, h)) if b]
    if boxes:
        pad = round(0.04 * max(w, h))
        x0 = max(0, min(b[0] for b in boxes) - pad)
        y0 = max(0, min(b[1] for b in boxes) - pad)
        x1 = min(w, max(b[2] for b in boxes) + pad)
        y1 = min(h, max(b[3] for b in boxes) + pad)
        imgs = [i.crop((x0, y0, x1, y1)) if i.size == (w, h) else i for i in imgs]
    cw = CARD_W
    ch = round(imgs[0].height * cw / imgs[0].width)
    cols = grid_cols(len(imgs))
    rows = -(-len(imgs) // cols)
    title_px, cap_px, id_px = 40, 26, 18
    head = GUTTER + title_px + GUTTER
    cap = 14 + cap_px + 18
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * GUTTER, head + rows * (ch + cap + GUTTER)), SHEET_BG)
    d = ImageDraw.Draw(sheet)
    base = GUTTER + title_px
    d.text((GUTTER, base), title, fill="white", font=font(title_px, bold=True), anchor="ls")
    tw = d.textlength(title, font=font(title_px, bold=True))
    d.text((GUTTER + tw + 16, base), "3D views", fill="#8b93a0", font=font(24), anchor="ls")
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=16, fill=255)
    for k, (im, (name, desc, _)) in enumerate(zip(imgs, results)):
        x = GUTTER + (k % cols) * (cw + GUTTER)
        y = head + (k // cols) * (ch + cap + GUTTER)
        card = Image.new("RGBA", (cw, ch), CARD_BG)
        card.alpha_composite(im.resize((cw, ch), Image.LANCZOS))
        sheet.paste(card.convert("RGB"), (x, y), mask)
        base = y + ch + 14 + cap_px
        d.text((x + 4, base), desc, fill="white", font=font(cap_px, bold=True), anchor="ls")
        d.text((x + cw - 4, base), name, fill="#8b93a0", font=font(id_px), anchor="rs")
    return sheet
