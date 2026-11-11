"""The rendr window: pick copper layers or 3D views, options, output folder, run.

Modeless (KiCad stays usable while it renders); the work runs in a thread, kicad-cli
does the plotting and rendering on the board file saved on disk.
"""
import json
import os
import threading

import wx
import wx.dataview as dv

from . import boardinfo, copper, render3d
from .common import Cancelled, Job, JobError, find_kicad_cli

SETTINGS = os.path.join(render3d.user_config_dir(), "rendr.json")
COL_ON, COL_LAYER, COL_BELOW, COL_CAPTION = range(4)


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


class DialogJob(Job):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg

    def log(self, msg):
        wx.CallAfter(self.dlg.append_log, msg)

    def progress(self, done, total):
        wx.CallAfter(self.dlg.set_progress, done, total)


def _swatch(colour, size=12):
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(colour)))
    dc.Clear()
    dc.SetPen(wx.Pen(wx.Colour(80, 80, 80)))
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    dc.DrawRectangle(0, 0, size, size)
    dc.SelectObject(wx.NullBitmap)
    icon = wx.Icon()
    icon.CopyFromBitmap(bmp)
    return icon


class RendrDialog(wx.Dialog):
    def __init__(self, parent, board):
        super().__init__(parent, title="rendr: board images",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.parent = parent
        self.pcb = board.GetFileName()
        self.layers = boardinfo.copper_layers(board)
        self.default_captions = {l.name: l.caption for l in self.layers}
        self.settings = load_settings()
        self.bset = self.settings.setdefault("boards", {}).setdefault(self.pcb, {})
        self.job = None
        self.close_when_done = False
        self.last_out = None
        self.clean_fp = None  # board fingerprint when it last matched the file

        p = wx.Panel(self)
        top = wx.BoxSizer(wx.VERTICAL)

        head = wx.FlexGridSizer(2, 8, 8)
        head.AddGrowableCol(1)
        head.Add(wx.StaticText(p, label="Board"), 0, wx.ALIGN_CENTER_VERTICAL)
        head.Add(wx.StaticText(p, label=self.pcb), 0, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND)
        head.Add(wx.StaticText(p, label="Output folder"), 0, wx.ALIGN_CENTER_VERTICAL)
        default_out = os.path.join(os.path.dirname(self.pcb), "renders")
        self.out = wx.DirPickerCtrl(p, path=self.bset.get("output", default_out),
                                    style=wx.DIRP_USE_TEXTCTRL | wx.DIRP_SMALL)
        head.Add(self.out, 1, wx.EXPAND)
        top.Add(head, 0, wx.EXPAND | wx.ALL, 10)
        self.unsaved = wx.StaticText(p, label="Unsaved changes in the editor: rendr reads the board file "
                                              "on disk. Save (Ctrl+S) to include them.")
        self.unsaved.SetForegroundColour(wx.Colour(220, 120, 0))
        top.Add(self.unsaved, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.book = wx.Notebook(p)
        self.book.AddPage(self._copper_page(self.book), "Copper layers")
        self.book.AddPage(self._render_page(self.book), "3D renders")
        self.book.SetSelection(self.settings.get("tab", 0))
        top.Add(self.book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.gauge = wx.Gauge(p, range=100)
        top.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.logbox = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 110))
        top.Add(self.logbox, 0, wx.EXPAND | wx.ALL, 10)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = wx.Button(p, label="Open output folder")
        btns.Add(self.open_btn)
        btns.AddStretchSpacer()
        self.run_btn = wx.Button(p, label="Run")
        self.run_btn.SetDefault()
        self.close_btn = wx.Button(p, wx.ID_CLOSE, "Close")
        btns.Add(self.run_btn, 0, wx.RIGHT, 8)
        btns.Add(self.close_btn)
        top.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        p.SetSizer(top)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(p, 1, wx.EXPAND)
        self.SetSizerAndFit(s)
        self.SetSize(self.FromDIP(wx.Size(720, 760)))
        self.SetMinSize(self.FromDIP(wx.Size(560, 600)))

        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, lambda e: (e.Skip(), wx.CallAfter(self.update_run_label)))
        self.layer_list.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, lambda e: self.update_run_label())
        self.view_list.Bind(wx.EVT_CHECKLISTBOX, lambda e: self.update_run_label())
        self.gif.Bind(wx.EVT_CHECKBOX, lambda e: self._gif_enable())
        self.zones.Bind(wx.EVT_CHOICE, lambda e: self._zones_enable())
        self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)
        self.open_btn.Bind(wx.EVT_BUTTON, self.on_open)
        self.close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, lambda e: (self.check_unsaved(), e.Skip()))
        self._gif_enable()
        self._zones_enable()
        self.update_run_label()
        self.check_unsaved()
        self.CentreOnParent()

    # ---------------------------------------------------------------- pages
    def _copper_page(self, book):
        pg = wx.Panel(book)
        s = wx.BoxSizer(wx.VERTICAL)
        cs = self.settings.get("copper", {})
        d = copper.CopperOptions()

        self.layer_list = dv.DataViewListCtrl(pg, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self.layer_list.AppendToggleColumn("", mode=dv.DATAVIEW_CELL_ACTIVATABLE, width=self.FromDIP(30))
        self.layer_list.AppendIconTextColumn("Layer", width=self.FromDIP(110))
        self.layer_list.AppendToggleColumn("From below", mode=dv.DATAVIEW_CELL_ACTIVATABLE, width=self.FromDIP(90))
        self.layer_list.AppendTextColumn("Caption (click to edit)", mode=dv.DATAVIEW_CELL_EDITABLE, width=-1)
        sel = self.bset.get("layers")
        below = self.bset.get("from_below", [])
        caps = self.bset.get("captions", {})
        for l in self.layers:
            self.layer_list.AppendItem([sel is None or l.name in sel, dv.DataViewIconText(l.name, _swatch(l.colour)),
                                        l.name in below, caps.get(l.name, l.caption)])
        s.Add(self.layer_list, 1, wx.EXPAND | wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("All", lambda e: self._check_layers(True)), ("None", lambda e: self._check_layers(False)),
                          ("Reset captions", self._reset_captions)):
            b = wx.Button(pg, label=label, style=wx.BU_EXACTFIT)
            b.Bind(wx.EVT_BUTTON, fn)
            row.Add(b, 0, wx.RIGHT, 6)
        s.Add(row, 0, wx.LEFT | wx.RIGHT, 8)

        box = wx.StaticBoxSizer(wx.VERTICAL, pg, "Options")
        sb = box.GetStaticBox()
        g = wx.FlexGridSizer(2, 6, 10)
        g.AddGrowableCol(1)
        self.cu_width = wx.SpinCtrl(sb, min=400, max=12000, initial=cs.get("width", d.width))
        g.Add(wx.StaticText(sb, label="Image width (px)"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.cu_width)
        self.dim = wx.Slider(sb, value=round(100 * cs.get("dim", d.dim)), minValue=0, maxValue=100,
                             style=wx.SL_HORIZONTAL | wx.SL_VALUE_LABEL)
        g.Add(wx.StaticText(sb, label="Other layers opacity (%)"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.dim, 1, wx.EXPAND)
        self.zones = wx.Choice(sb, choices=["Refill (temp copy, board untouched)", "As saved in the file",
                                            "Hidden (tracks, pads, vias only)"])
        self.zones.SetSelection(["refill", "saved", "hidden"].index(cs.get("zones", d.zones)))
        g.Add(wx.StaticText(sb, label="Zones"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.zones)
        box.Add(g, 0, wx.EXPAND | wx.ALL, 4)
        self.hide_biggest = wx.CheckBox(sb, label="Hide the biggest pour of each layer (planes), keep the small ones")
        self.hide_biggest.SetValue(cs.get("hide_biggest", d.hide_biggest))
        self.hide_biggest.SetToolTip("Per layer, the zone with the largest fill is left out if it covers "
                                     "at least a fifth of the board: a GND plane goes, small pours stay.")
        box.Add(self.hide_biggest, 0, wx.ALL, 4)

        self.cu_overview = wx.CheckBox(sb, label="Overview sheet (all selected layers in a grid)")
        self.cu_overview.SetValue(cs.get("overview", d.overview))
        box.Add(self.cu_overview, 0, wx.ALL, 4)
        gr = wx.BoxSizer(wx.HORIZONTAL)
        self.gif = wx.CheckBox(sb, label="Animated GIF")
        self.gif.SetValue(cs.get("gif", d.gif))
        self.gif_delay = wx.SpinCtrl(sb, min=100, max=10000, initial=cs.get("gif_delay", d.gif_delay))
        self.gif_width = wx.SpinCtrl(sb, min=200, max=4000, initial=cs.get("gif_width", d.gif_width))
        gr.Add(self.gif, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        gr.Add(wx.StaticText(sb, label="ms per layer"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        gr.Add(self.gif_delay, 0, wx.RIGHT, 12)
        gr.Add(wx.StaticText(sb, label="width (px)"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        gr.Add(self.gif_width)
        box.Add(gr, 0, wx.ALL, 4)
        s.Add(box, 0, wx.EXPAND | wx.ALL, 8)
        pg.SetSizer(s)
        return pg

    def _render_page(self, book):
        pg = wx.Panel(book)
        s = wx.BoxSizer(wx.VERTICAL)
        rs = self.settings.get("render", {})
        d = render3d.RenderOptions()

        self.view_names = list(render3d.VIEWS)
        self.view_list = wx.CheckListBox(pg, choices=[f"{n}  -  {render3d.VIEWS[n][0]}" for n in self.view_names])
        for i, n in enumerate(self.view_names):
            self.view_list.Check(i, n in rs.get("views", render3d.DEFAULT_VIEWS))
        s.Add(self.view_list, 1, wx.EXPAND | wx.ALL, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, on in (("All", True), ("None", False)):
            b = wx.Button(pg, label=label, style=wx.BU_EXACTFIT)
            b.Bind(wx.EVT_BUTTON, lambda e, on=on: self._check_views(on))
            row.Add(b, 0, wx.RIGHT, 6)
        s.Add(row, 0, wx.LEFT | wx.RIGHT, 8)

        box = wx.StaticBoxSizer(wx.VERTICAL, pg, "Options")
        sb = box.GetStaticBox()
        g = wx.FlexGridSizer(2, 6, 10)
        self.quality = wx.Choice(sb, choices=["High (raytraced, slower)", "Basic (fast preview)"])
        self.quality.SetSelection(0 if rs.get("quality", d.quality) == "high" else 1)
        g.Add(wx.StaticText(sb, label="Quality"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.quality)
        size = wx.BoxSizer(wx.HORIZONTAL)
        self.r_width = wx.SpinCtrl(sb, min=200, max=8000, initial=rs.get("width", d.width))
        self.r_height = wx.SpinCtrl(sb, min=200, max=8000, initial=rs.get("height", d.height))
        size.Add(self.r_width)
        size.Add(wx.StaticText(sb, label=" x "), 0, wx.ALIGN_CENTER_VERTICAL)
        size.Add(self.r_height)
        g.Add(wx.StaticText(sb, label="Size (px)"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(size)
        self.fmt = wx.Choice(sb, choices=["png", "jpg"])
        self.fmt.SetStringSelection(rs.get("format", d.format))
        g.Add(wx.StaticText(sb, label="Format"), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.fmt)
        box.Add(g, 0, wx.ALL, 4)
        self.floor = wx.CheckBox(sb, label="Floor, shadows and post-processing")
        self.floor.SetValue(rs.get("floor", d.floor))
        self.transparent = wx.CheckBox(sb, label="Transparent background (PNG only)")
        self.transparent.SetValue(rs.get("transparent", d.transparent))
        self.r_overview = wx.CheckBox(sb, label="Overview sheet (all views in a grid, captioned)")
        self.r_overview.SetValue(rs.get("overview", d.overview))
        for c in (self.floor, self.transparent, self.r_overview):
            box.Add(c, 0, wx.ALL, 4)
        s.Add(box, 0, wx.EXPAND | wx.ALL, 8)
        pg.SetSizer(s)
        return pg

    # --------------------------------------------------------------- state
    def _check_layers(self, on):
        for r in range(self.layer_list.GetItemCount()):
            self.layer_list.SetToggleValue(on, r, COL_ON)
        self.update_run_label()

    def _check_views(self, on):
        for i in range(self.view_list.GetCount()):
            self.view_list.Check(i, on)
        self.update_run_label()

    def _reset_captions(self, _evt):
        for r, l in enumerate(self.layers):
            self.layer_list.SetTextValue(self.default_captions[l.name], r, COL_CAPTION)

    def _gif_enable(self):
        self.gif_delay.Enable(self.gif.GetValue())
        self.gif_width.Enable(self.gif.GetValue())

    def _zones_enable(self):
        self.hide_biggest.Enable(self.zones.GetSelection() != 2)  # pointless when all pours are hidden

    def selected_layers(self):
        return [l.name for r, l in enumerate(self.layers) if self.layer_list.GetToggleValue(r, COL_ON)]

    def selected_views(self):
        return [self.view_names[i] for i in self.view_list.GetCheckedItems()]

    def update_run_label(self):
        if self.job:
            return
        if self.book.GetSelection() == 0:
            n = len(self.selected_layers())
            self.run_btn.SetLabel(f"Export {n} layer{'s' if n != 1 else ''}")
        else:
            n = len(self.selected_views())
            self.run_btn.SetLabel(f"Render {n} view{'s' if n != 1 else ''}")
        self.run_btn.Enable(n > 0)
        self.run_btn.GetParent().Layout()

    def check_unsaved(self):
        """Warn when the editor holds edits the file on disk doesn't have.
        KiCad marks the board modified after every action plugin run (even one
        that changes nothing), so the "*" in the title alone can't be trusted:
        remember the board's fingerprint while the title is clean, and only warn
        once the board really differs from it."""
        title = self.parent.GetTitle() if self.parent else ""
        board = _current_board()
        if not title.startswith("*"):
            show = False
            if board is not None:
                self.clean_fp = boardinfo.fingerprint(board)
        else:
            show = (self.clean_fp is None or board is None
                    or boardinfo.fingerprint(board) != self.clean_fp)
        if show != self.unsaved.IsShown():
            self.unsaved.Show(show)
            self.unsaved.GetParent().Layout()

    def collect(self):
        """Read the widgets into option objects and remember them."""
        layers = self.layers
        for r, l in enumerate(layers):
            l.from_below = self.layer_list.GetToggleValue(r, COL_BELOW)
            l.caption = self.layer_list.GetTextValue(r, COL_CAPTION)
        cu = copper.CopperOptions(width=self.cu_width.GetValue(), dim=self.dim.GetValue() / 100,
                                  zones=["refill", "saved", "hidden"][self.zones.GetSelection()],
                                  hide_biggest=self.hide_biggest.GetValue(),
                                  overview=self.cu_overview.GetValue(), gif=self.gif.GetValue(),
                                  gif_delay=self.gif_delay.GetValue(), gif_width=self.gif_width.GetValue())
        r3 = render3d.RenderOptions(quality="high" if self.quality.GetSelection() == 0 else "basic",
                                    floor=self.floor.GetValue(), width=self.r_width.GetValue(),
                                    height=self.r_height.GetValue(), transparent=self.transparent.GetValue(),
                                    format=self.fmt.GetStringSelection(), overview=self.r_overview.GetValue())
        self.settings["tab"] = self.book.GetSelection()
        self.settings["copper"] = vars(cu)
        self.settings["render"] = dict(vars(r3), views=self.selected_views())
        self.bset.update(output=self.out.GetPath(), layers=self.selected_layers(),
                         from_below=[l.name for l in layers if l.from_below],
                         captions={l.name: l.caption for l in layers if l.caption != self.default_captions[l.name]})
        save_settings(self.settings)
        return cu, r3

    # ----------------------------------------------------------------- run
    def append_log(self, msg):
        if self:
            self.logbox.AppendText(msg + "\n")

    def set_progress(self, done, total):
        if self:
            self.gauge.SetRange(max(total, 1))
            self.gauge.SetValue(min(done, total))

    def on_run(self, _evt):
        if self.job:  # the button is "Cancel" while running
            self.job.cancelled = True
            self.run_btn.Disable()
            self.append_log("Cancelling...")
            return
        if not os.path.isfile(self.pcb):
            wx.MessageBox("Save the board first: rendr works on the board file.", "rendr", wx.ICON_WARNING, self)
            return
        self.check_unsaved()
        cu, r3 = self.collect()
        out_root = self.out.GetPath() or os.path.join(os.path.dirname(self.pcb), "renders")
        cli = find_kicad_cli()
        if self.book.GetSelection() == 0:
            out = os.path.join(out_root, "copper_layers")
            sel = self.selected_layers()
            work = lambda job: copper.export_copper(cli, self.pcb, self.layers, sel, out, cu, job)
        else:
            out = os.path.join(out_root, "3d")
            views = self.selected_views()
            work = lambda job: render3d.render_views(cli, self.pcb, views, out, r3, job)
        self.last_out = out
        self.logbox.Clear()
        self.gauge.SetValue(0)
        self.job = DialogJob(self)
        self.book.Disable()
        self.out.Disable()
        self.run_btn.SetLabel("Cancel")
        job = self.job

        def worker():
            try:
                written = work(job)
                wx.CallAfter(self.finished, f"Done: {len(written)} file(s) in {out}")
            except Cancelled:
                wx.CallAfter(self.finished, "Cancelled.")
            except JobError as e:
                wx.CallAfter(self.finished, f"Error: {e}")
            except Exception as e:  # keep the window usable whatever happens
                wx.CallAfter(self.finished, f"Error: {type(e).__name__}: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def finished(self, msg):
        if not self:
            return
        self.job = None
        self.append_log(msg)
        if msg.startswith("Done"):
            self.gauge.SetValue(self.gauge.GetRange())
        self.book.Enable()
        self.out.Enable()
        self.run_btn.Enable()
        self.update_run_label()
        if self.close_when_done:
            self.Destroy()

    def on_open(self, _evt):
        path = self.last_out if self.last_out and os.path.isdir(self.last_out) else self.out.GetPath()
        if not os.path.isdir(path):
            wx.MessageBox(f"Nothing rendered yet:\n{path}", "rendr", wx.ICON_INFORMATION, self)
            return
        wx.LaunchDefaultApplication(path)

    def on_close(self, _evt):
        if self.job:
            self.job.cancelled = True
            self.close_when_done = True
            self.Hide()
            return
        self.collect()
        self.Destroy()


def _current_board():
    try:
        import pcbnew
        return pcbnew.GetBoard()
    except Exception:
        return None
