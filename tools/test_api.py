"""Live check of the csp API, run inside CSP with a canvas open:  python console.py <repo>\\tools\\test_api.py
Adds a few layers to the active canvas and undoes them again. Prints "ok" or raises."""
CspError = csp.CspError
doc = csp.active_document()
docs = csp.documents()
assert doc in docs and len(set(docs)) == len(docs), docs
for d in docs:
    d.active_layer  # readable in background tabs too

start = doc.active_layer
assert start == csp.current_layer() and {start: 1}[csp.current_layer()] == 1  # Layer is hashable

cmds = csp.commands()
assert len(cmds) > 100, len(cmds)
assert cmds["Layer/New Raster Layer"][0] and cmds["Edit/Undo"][0]

layer = doc.new_raster_layer("cspy test")
assert layer != start and layer.name == "cspy test" and doc.active_layer == layer
assert csp.enabled("edit/undo")  # case-insensitive
csp.run("Edit/Undo")
assert doc.active_layer == start, doc.active_layer

csp.run("Layer/New Raster Layer")
via_menu = doc.active_layer
assert via_menu != start
csp.run(cmds["Edit/Undo"][0])  # by id
assert doc.active_layer == start

for bad, msg in (("No/Such/Item", "no menu command"), (0x7FFF1234, "nothing handles")):
    try:
        csp.run(bad)
    except CspError as e:
        assert msg in str(e), e
    else:
        raise AssertionError(f"run({bad!r}) did not raise")

# layer properties, set on a fresh layer while another one is selected
before_layers = doc.layers
assert start in before_layers and start.parent is None and not start.children
layer = doc.new_raster_layer("cspy props")
doc.active_layer = start
assert doc.active_layer == start
assert (layer.visible, layer.opacity, layer.blend_mode) == (True, 100, 0)
assert not (layer.locked or layer.lock_transparency or layer.clip or layer.is_folder)
for prop in ("visible", "locked", "lock_transparency", "clip"):
    want = not getattr(layer, prop)
    setattr(layer, prop, want)
    assert getattr(layer, prop) == want, prop
    setattr(layer, prop, not want)
    assert getattr(layer, prop) == (not want), prop
layer.opacity, layer.blend_mode = 40, csp.BlendMode.MULTIPLY
assert (layer.opacity, layer.blend_mode) == (40, 2)
assert doc.active_layer == start, "setters must restore the selection"
csp.run("Edit/Undo")  # the blend mode
csp.run("Edit/Undo")  # the opacity
assert (layer.opacity, layer.blend_mode) == (100, 0)
folder = doc.new_folder("cspy folder")
inner = doc.new_raster_layer("cspy inner")  # created inside the selected folder
assert folder.is_folder and folder.children == [inner] and inner.parent == folder and folder in doc.layers
try:
    layer.opacity = 101
except CspError:
    pass
else:
    raise AssertionError("opacity 101 accepted")
while layer in doc.layers:  # undo the test layers (and the lock/clip toggles in between)
    csp.run("Edit/Undo")
assert doc.layers == before_layers, doc.layers

# undo groups: nested, then one undo / redo for the lot
with doc.undo_group("cspy test group"):
    with csp.undo_group("inner"):
        a = doc.new_raster_layer("cspy g1")
        doc.new_folder("cspy g2")
    a.opacity = 30
csp.run("Edit/Undo")
assert doc.layers == before_layers, doc.layers
csp.run("Edit/Redo")
assert a in doc.layers and a.opacity == 30
csp.run("Edit/Undo")
try:  # a block that raises still ends up as one step
    with doc.undo_group():
        doc.new_raster_layer("cspy g3")
        doc.new_raster_layer("cspy g4")
        raise KeyError("boom")
except KeyError:
    pass
csp.run("Edit/Undo")
assert doc.layers == before_layers, doc.layers
with doc.undo_group():  # empty: records nothing
    pass

# document info
for d in docs:
    assert d.name and isinstance(d.width, int) and d.width > 0 and d.height > 0 and d.resolution > 0, d
    assert d.modified in (True, False)
if not any(layer.is_text for layer in doc.walk()):  # rotating asks to rasterize text (and 3D) layers
    w, h = doc.width, doc.height
    csp.run("Edit/Rotate/Flip canvas/Rotate by 90 Degrees Clockwise")
    assert (doc.width, doc.height) == (h, w) and doc.modified
    csp.run("Edit/Undo")
    assert (doc.width, doc.height) == (w, h)

# text layers (read-only)
for layer in doc.walk():
    assert layer.is_text == (layer.text is not None), layer

# menu-backed layer / document methods, all in one undo step
with doc.undo_group("cspy menu methods"):
    a = doc.new_raster_layer("cspy m1")
    b = a.duplicate()
    assert b != a and doc.active_layer == b and doc.find("cspy m1") in (a, b) and doc.find("cspy none") is None
    top = doc.layers
    b.move("down")
    assert doc.layers.index(b) == top.index(b) + 1, doc.layers
    b.move("up")
    assert doc.layers == top, doc.layers
    m = doc.new_raster_layer("cspy m2").merge_down()
    m.remove()
    assert m not in doc.layers
    doc.select_all(); doc.invert_selection(); doc.select_all(); doc.deselect()
    assert len(list(doc.walk())) >= len(doc.layers)
csp.run("Edit/Undo")
assert doc.layers == before_layers, doc.layers

# vector layers, named blend modes, history (undo by how much the history grew: CSP merges repeated changes)
h0 = doc.history
v = doc.new_vector_layer("cspy vec")
assert v.name == "cspy vec" and not v.is_text and v.blend_mode == csp.BlendMode.NORMAL
assert len(doc.history) == len(h0) + 1
v.blend_mode = csp.BlendMode.DIVIDE
assert v.blend_mode is csp.BlendMode.DIVIDE
try:
    v.blend_mode = 37
except CspError:
    pass
else:
    raise AssertionError("blend mode 37 accepted")
doc.undo(len(doc.history) - len(h0))
assert doc.history == h0 and doc.layers == before_layers, doc.history

# drawing colors, rectangle selection, fill (colors are put back at the end)
main0, sub0 = csp.main_color(), csp.sub_color()
try:
    csp.set_sub_color((1, 2, 3))
    csp.set_main_color((250, 10, 10))
    assert (csp.main_color(), csp.sub_color()) == ((250, 10, 10), (1, 2, 3))
    for bad in ((1, 2), (1, 2, 300), (1.5, 2, 3)):
        try:
            csp.set_main_color(bad)
        except CspError:
            pass
        else:
            raise AssertionError(f"color {bad!r} accepted")
    h0 = doc.history
    doc.new_raster_layer("cspy fill")
    doc.select_rect(10, 10, 60, 40)
    assert csp.enabled("Select/Deselect")
    doc.fill()
    doc.fill((0, 200, 0))
    assert csp.main_color() == (250, 10, 10), "fill(rgb) must put the main color back"
    assert doc.history[len(h0):] == ["New Raster Layer", "Select rectangle", "Fill", "Fill"], doc.history
    try:
        doc.select_rect(5, 5, 5, 9)
    except CspError:
        pass
    else:
        raise AssertionError("empty rectangle accepted")
    doc.undo(len(doc.history) - len(h0))
    assert doc.history == h0 and doc.layers == before_layers and not csp.enabled("Select/Deselect")
finally:
    csp.set_main_color(main0)
    csp.set_sub_color(sub0)

# export without dialogs (into the temp folder; the file is removed again)
import os, struct, tempfile
out = os.path.join(tempfile.gettempdir(), "cspy_test_export.png")
doc.export(out)
with open(out, "rb") as f:
    head = f.read(24)
os.remove(out)
assert head[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", head[16:24]) == (doc.width, doc.height), head
for bad in ("x.gif", os.path.join(tempfile.gettempdir(), "no such folder", "x.png")):
    try:
        doc.export(bad)
    except CspError:
        pass
    else:
        raise AssertionError(f"export to {bad!r} accepted")

try:
    csp.Layer(0x10, doc).name
except CspError:
    pass
else:
    raise AssertionError("bad layer pointer not caught")
print("ok", len(docs), "docs,", len(cmds), "commands")
