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
layer.opacity, layer.blend_mode = 40, 2
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

try:
    csp.Layer(0x10, doc).name
except CspError:
    pass
else:
    raise AssertionError("bad layer pointer not caught")
print("ok", len(docs), "docs,", len(cmds), "commands")
