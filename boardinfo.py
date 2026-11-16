"""Copper stack of a board, read through the pcbnew API (the open board in the plugin,
pcbnew.LoadBoard() from the command line)."""
from dataclasses import dataclass

# KiCad default colour theme
CU_COLOURS = {"F.Cu": "#C83434", "B.Cu": "#4D7FC4"}
INNER_COLOURS = ["#7FC87F", "#CE7D2C", "#4FCBCB", "#DB628B", "#A7A5C6", "#28CCD9",
                 "#E8B2A7", "#F2EDA1", "#8DCB81", "#ED7C33", "#5BC3EB", "#F76F8E"]


@dataclass
class CopperLayer:
    name: str        # canonical name, what kicad-cli takes (In1.Cu)
    position: int    # 1 = top
    colour: str
    caption: str     # second caption line, editable in the dialog
    from_below: bool = False


def colour_for(name):
    if name in CU_COLOURS:
        return CU_COLOURS[name]
    try:
        n = int(name[2:-3])  # "In7.Cu" -> 7
    except ValueError:
        return "#C0C0C0"
    return INNER_COLOURS[(n - 1) % len(INNER_COLOURS)]


def _short(net):
    return net.rsplit("/", 1)[-1] or net


def _zone_area(zone, lid):
    """Filled area of the zone on that layer, its outline area while it is unfilled."""
    try:
        if zone.HasFilledPolysForLayer(lid):
            area = zone.GetFilledPolysList(lid).Area()
            if area > 0:
                return area
    except Exception:
        pass
    return zone.Outline().Area()


def copper_layers(board):
    """[CopperLayer] top to bottom. Default caption: user layer name if set, else
    Top/Inner n/Bottom, plus the net of the layer's biggest pour (the smaller zones
    are not the pour, they go unnamed)."""
    ids = list(board.GetEnabledLayers().CuStack())
    pours = {}  # layer id -> (area, net) of its biggest zone
    for z in board.Zones():
        if z.GetIsRuleArea() or not z.GetNetname():
            continue
        for lid in z.GetLayerSet().CuStack():
            if lid not in ids:
                continue
            area = _zone_area(z, lid)
            if lid not in pours or area > pours[lid][0]:
                pours[lid] = (area, _short(z.GetNetname()))
    out = []
    for pos, lid in enumerate(ids, 1):
        name = board.GetStandardLayerName(lid)
        user = board.GetLayerName(lid)
        if user != name:
            what = user
        elif name == "F.Cu":
            what = "Top"
        elif name == "B.Cu":
            what = "Bottom"
        else:
            what = "Inner " + name[2:-3]
        if lid in pours:
            what += f": {pours[lid][1]} Pour"
        out.append(CopperLayer(name, pos, colour_for(name), what))
    return out


def fingerprint(board):
    """Cheap hash of what the board looks like (item boxes, layers, nets, footprint
    refs/values/rotations, zone fills). Equal fingerprints = no real edit.
    None if the API misbehaves."""
    try:
        h = []
        for kind, items in (("F", board.Footprints()), ("T", board.GetTracks()),
                            ("D", board.GetDrawings()), ("Z", board.Zones())):
            for it in items:
                bb = it.GetBoundingBox()
                h.append((kind, bb.GetX(), bb.GetY(), bb.GetWidth(), bb.GetHeight(), it.GetLayer(),
                          it.GetNetCode() if hasattr(it, "GetNetCode") else 0))
        h += [(f.GetReference(), f.GetValue(), f.GetOrientationDegrees()) for f in board.Footprints()]
        h += [(z.IsFilled(), z.GetOutlineArea()) for z in board.Zones()]
        return hash(tuple(h))
    except Exception:
        return None


def load(path):
    import pcbnew
    return pcbnew.LoadBoard(path)
