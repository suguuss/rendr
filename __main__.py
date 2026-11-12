"""rendr from the command line (no GUI). Run from the folder that contains rendr/:

  python3 -m rendr copper BOARD.kicad_pcb [options]
  python3 -m rendr 3d     BOARD.kicad_pcb [options]
  python3 -m rendr 3d --list
  python3 -m rendr stackup BOARD.kicad_pcb [options]
"""
import argparse
import os
import sys

from . import boardinfo, copper, render3d, stackup
from .common import Cancelled, Job, JobError, find_kicad_cli


def _names(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m rendr", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kicad-cli", default=find_kicad_cli())
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("copper", help="one image per copper layer (+ overview, GIF)")
    c.add_argument("pcb")
    c.add_argument("-o", "--output", help="output folder (default: <board dir>/renders/copper_layers)")
    c.add_argument("--layers", help="comma-separated layers to write (default: all copper layers)")
    c.add_argument("--from-below", help="comma-separated layers drawn mirrored, as seen from below")
    c.add_argument("--caption", action="append", default=[], metavar="LAYER=TEXT",
                   help="replace a layer's caption, e.g. --caption 'In1.Cu=GND plane' (repeatable)")
    d = copper.CopperOptions()
    c.add_argument("--width", type=int, default=d.width, help="image width in px (default %(default)s)")
    c.add_argument("--dim", type=float, default=d.dim, help="opacity of the other layers, 0..1 (default %(default)s)")
    c.add_argument("--zones", choices=["refill", "saved", "hidden"], default=d.zones,
                   help="refill in a temp copy (default), plot as saved, or hide pours")
    c.add_argument("--hide-biggest", action="store_true",
                   help="hide each layer's biggest pour (ground/power planes), keep the small ones")
    c.add_argument("--no-overview", action="store_true")
    c.add_argument("--gif", action="store_true", help="also write an animated GIF cycling through the layers")
    c.add_argument("--gif-delay", type=int, default=d.gif_delay, help="ms per frame (default %(default)s)")
    c.add_argument("--gif-width", type=int, default=d.gif_width, help="GIF width in px (default %(default)s)")

    r = sub.add_parser("3d", help="3D renders from several camera angles (+ overview)")
    r.add_argument("pcb", nargs="?")
    r.add_argument("-o", "--output", help="output folder (default: <board dir>/renders/3d)")
    r.add_argument("--views", default="all", help="comma-separated view names, or 'all' (default)")
    r.add_argument("--list", action="store_true", help="list views and exit")
    o = render3d.RenderOptions()
    r.add_argument("--quality", choices=["basic", "high"], default=o.quality)
    r.add_argument("--no-floor", action="store_true", help="no floor, shadows or post-processing")
    r.add_argument("--width", type=int, default=o.width)
    r.add_argument("--height", type=int, default=o.height)
    r.add_argument("--transparent", action="store_true", help="transparent background (PNG only)")
    r.add_argument("--format", choices=["png", "jpg"], default=o.format)
    r.add_argument("--no-overview", action="store_true")

    s = sub.add_parser("stackup", help="stackup cross-section with layer table")
    s.add_argument("pcb")
    s.add_argument("-o", "--output", help="output folder (default: <board dir>/renders/stackup)")
    so = stackup.StackupOptions()
    s.add_argument("--width", type=int, default=so.width, help="image width in px (default %(default)s)")
    s.add_argument("--light", action="store_true", help="white background (for documents)")
    s.add_argument("--no-via", action="store_true", help="no plated via in the section")
    s.add_argument("--no-captions", action="store_true", help="no copper layer captions")
    s.add_argument("--caption", action="append", default=[], metavar="LAYER=TEXT",
                   help="replace a copper layer's caption (repeatable)")
    a = ap.parse_args(argv)

    if a.cmd == "3d" and a.list:
        for k, (desc, args) in render3d.VIEWS.items():
            print(f"{k:16} {desc:32} {' '.join(args)}")
        return 0
    if not a.pcb or not os.path.isfile(a.pcb):
        ap.error(f"board not found: {a.pcb}")
    pcb = os.path.abspath(a.pcb)
    job = Job()
    try:
        if a.cmd == "copper":
            layers = boardinfo.copper_layers(boardinfo.load(pcb))
            known = {l.name for l in layers}
            sel = _names(a.layers) or [l.name for l in layers]
            below = _names(a.from_below)
            caps = dict(x.split("=", 1) for x in a.caption if "=" in x)
            bad = [n for n in sel + below + list(caps) if n not in known]
            if bad:
                ap.error(f"not a copper layer of this board: {', '.join(bad)} (has {', '.join(known)})")
            for l in layers:
                l.from_below = l.name in below
                l.caption = caps.get(l.name, l.caption)
            opt = copper.CopperOptions(width=a.width, dim=a.dim, zones=a.zones, hide_biggest=a.hide_biggest,
                                       overview=not a.no_overview,
                                       gif=a.gif, gif_delay=a.gif_delay, gif_width=a.gif_width)
            out = a.output or os.path.join(os.path.dirname(pcb), "renders", "copper_layers")
            copper.export_copper(a.kicad_cli, pcb, layers, sel, out, opt, job)
        elif a.cmd == "stackup":
            try:  # default captions (user layer names, pours) need pcbnew
                caps = {l.name: l.caption for l in boardinfo.copper_layers(boardinfo.load(pcb))}
            except ImportError:
                caps = {}
            caps.update(x.split("=", 1) for x in a.caption if "=" in x)
            opt = stackup.StackupOptions(width=a.width, light=a.light, via=not a.no_via, captions=not a.no_captions)
            out = a.output or os.path.join(os.path.dirname(pcb), "renders", "stackup")
            stackup.export_stackup(pcb, out, opt, job, caps)
        else:
            names = list(render3d.VIEWS) if a.views == "all" else _names(a.views)
            opt = render3d.RenderOptions(quality=a.quality, floor=not a.no_floor, width=a.width, height=a.height,
                                         transparent=a.transparent, format=a.format, overview=not a.no_overview)
            out = a.output or os.path.join(os.path.dirname(pcb), "renders", "3d")
            render3d.render_views(a.kicad_cli, pcb, names, out, opt, job)
    except JobError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (Cancelled, KeyboardInterrupt):
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
