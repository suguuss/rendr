import os

import pcbnew
import wx

from . import dialog

_window = None  # the open rendr window, if any


class Rendr(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "rendr: Board Images"
        self.category = "Export"
        self.description = "Copper layer images, 3D renders and stackup of the board, picked from a window"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

    def Run(self):
        global _window
        board = pcbnew.GetBoard()
        if _window:  # a destroyed window is falsy in wxPython
            if _window.pcb == board.GetFileName() or _window.job:
                _window.check_unsaved()  # now, before KiCad flags the board modified
                _window.Show()
                _window.Raise()
                return
            _window.Close()  # another board was opened since: start over
        frame = wx.FindWindowByName("PcbFrame")
        if not board.GetFileName():
            wx.MessageBox("Save the board first: rendr works on the board file.", "rendr", wx.ICON_WARNING, frame)
            return
        _window = dialog.RendrDialog(frame, board)
        _window.Show()


def register():
    Rendr().register()
