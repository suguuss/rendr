"""Shared helpers: finding kicad-cli, running it (cancellable), fonts, overview sheets."""
import glob
import math
import os
import shutil
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont


class Cancelled(Exception):
    pass


class JobError(Exception):
    pass


class Job:
    """What a running export reports to: log lines, progress, cancel flag.
    The CLI uses it as is (prints); the dialog overrides log/progress."""

    def __init__(self):
        self.cancelled = False

    def log(self, msg):
        print(msg, flush=True)

    def progress(self, done, total):
        pass

    def check(self):
        if self.cancelled:
            raise Cancelled()


def block_end(s, j):
    """Index of the ")" closing the s-expression that opens at s[j]."""
    depth, k, n = 0, j, len(s)
    while k < n:
        c = s[k]
        if c == '"':
            k += 1
            while s[k] != '"':
                k += 2 if s[k] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return n - 1


def find_kicad_cli():
    """kicad-cli on PATH, else next to KiCad's own executable / Python, else usual install dirs."""
    exe = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    found = shutil.which("kicad-cli")
    if found:
        return found
    cands = [os.path.join(os.path.dirname(sys.executable), exe)]
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            cands += sorted(glob.glob(os.path.join(root, "KiCad", "*", "bin", exe)), reverse=True)
    elif sys.platform == "darwin":
        cands.append("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return "kicad-cli"


def run(cmd, job, env=None):
    """Run a command; kill it if the job is cancelled. Raise JobError on failure."""
    job.check()
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, creationflags=flags)
    except FileNotFoundError:
        raise JobError(f"program not found: {cmd[0]}")
    while p.poll() is None:
        if job.cancelled:
            p.kill()
            p.wait()
            raise Cancelled()
        time.sleep(0.05)
    out, err = p.communicate()
    if p.returncode != 0:
        raise JobError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{out}\n{err}".rstrip())
    return out


_FONTS = {False: ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"],
          True: ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"]}


def font(size, bold=False):
    """A system TrueType font (Pillow looks in the OS font folders), else Pillow's own."""
    for name in _FONTS[bold]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def grid_cols(n):
    return n if n <= 3 else (2 if n == 4 else 3)


def overview(tiles, tile_width, background, caption_px=0, captions=None):
    """Grid of images (all the same aspect ratio), optional caption strip under each tile."""
    cols = grid_cols(len(tiles))
    rows = math.ceil(len(tiles) / cols)
    th = round(tiles[0].height * tile_width / tiles[0].width)
    sheet = Image.new("RGB", (tile_width * cols, (th + caption_px) * rows), background)
    d = ImageDraw.Draw(sheet)
    for k, t in enumerate(tiles):
        x, y = (k % cols) * tile_width, (k // cols) * (th + caption_px)
        if t.mode == "RGBA":  # flatten transparent renders on the background
            bg = Image.new("RGBA", t.size, background)
            t = Image.alpha_composite(bg, t)
        sheet.paste(t.convert("RGB").resize((tile_width, th), Image.LANCZOS), (x, y))
        if captions and caption_px:
            d.text((x + 10, y + th + caption_px // 5), captions[k], fill="white", font=font(int(caption_px * 0.55)))
    return sheet
