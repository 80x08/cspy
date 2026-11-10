# Experimental - run Python on Clip Studio Paint 1.13.2.0

Runs an embedded Python 3.14 inside CSP: a **Scripts** menu in the menu bar, a local console, and a ctypes API (`csp`)
built from reverse engineering. Only works with the exact CSP build in `mem.BUILD_TIMESTAMP`.

## Layout
| Path | What |
|---|---|
| `src/loader/loader.c` | `cspy.dll`: CSP loads it as a plugin; boots Python and runs `startup.py` |
| `src/cspy/` | runtime copied to `<CSP>\cspy\`: `startup.py` (UI-thread runner + console), `csp.py` (API), `mem.py` (raw memory helpers), `menu.py` (Scripts menu), `console.py` (client), `scripts/` |
| `tools/inject.py` | dev only: inject the DLL into a running CSP |
| `tools/test_api.py` | live API check: `console.py <repo>	ools	est_api.py` with a canvas open (adds layers, undoes them) |
| `build.ps1` | builds `dist\` (downloads + hash-checks the embeddable Python into `build\`) |
| `install.ps1` | copies `dist\` into the CSP folder (`-Uninstall` removes the plugin DLL) |
| `dist/`, `build/` | generated, not in git |

## Build & install
```powershell
powershell -ExecutionPolicy Bypass -File build.ps1      # needs VS 2026 C++ tools
# elevated, CSP closed:
powershell -ExecutionPolicy Bypass -File install.ps1
```
For another machine, copy `dist\` + `install.ps1` there and run the install step.

## Use
- **Scripts menu**: put `.py` files in `<CSP>\cspy\scripts\`; the menu re-reads the folder each time it opens.
  `csp` is predefined; `print` output and errors show in a message box.
- **Console** (from `<CSP>\cspy`): `.\python\python.exe console.py "csp.current_layer()"`, `console.py demo`,
  `console.py path\to\file.py`, or no argument for a prompt. A leading `!` runs off the UI thread (diagnostics only).

```python
doc = csp.active_document()           # csp.documents(): all open canvases, in tab order
layer = doc.new_raster_layer("ink")   # undoable, like Layer > New Raster Layer
folder = doc.new_folder("group")       # also doc.new_vector_layer()
doc.active_layer.name = "renamed"     # not undoable
layer.opacity = 50                    # 0..100; also visible, locked, lock_transparency, clip
layer.blend_mode = csp.BlendMode.MULTIPLY   # SCREEN, OVERLAY, ... DIVIDE; THROUGH for folders
doc.active_layer = layer              # select it
doc.layers, folder.children, layer.parent, layer.is_folder   # the layer tree, top to bottom
doc.history, doc.undo(2), doc.redo()  # undo step names (oldest first); CSP merges repeated same-kind changes
with doc.undo_group("My script"):      # everything inside is one undo step (csp.undo_group: active canvas)
    ...
layer.duplicate(), layer.merge_down(), layer.remove(), layer.move("up")   # top/up/down/bottom
doc.name, doc.width, doc.height, doc.resolution, doc.modified   # "Illustration", 1600, 1200, 72.0, True
layer.is_text, layer.text                 # text layers, read-only (None for other layers)
csp.main_color(), csp.set_main_color((255, 0, 0))   # also sub_color / set_sub_color; (r, g, b) 0..255
doc.select_rect(100, 100, 400, 300)   # left, top, right, bottom (undoable)
doc.fill(), doc.fill((0, 128, 255))   # Edit > Fill with the main color, or with this color
doc.export(r"C:\out\page.png")      # no dialogs; .png .jpg .bmp .tif .tga .psd .psb; last-used settings
img = doc.image()                     # the canvas as you see it; layer.image(): that layer alone
img.width, img.height, img.pixel(10, 20), img.data, img.save(r"C:\out\x.png")   # RGBA bytes, rows from the top
doc.find("ink"), doc.walk()           # first layer by name; every layer, depth first
doc.flatten(), doc.merge_visible(), doc.save(), doc.select_all(), doc.deselect(), doc.invert_selection()
doc = csp.open_document(r"C:\in\page.psd")   # no dialog; returns the (now active) canvas
doc.save_as(r"C:\out\page.clip")      # no dialog, .clip only (other formats: export)
doc.close()                           # refuses unsaved changes: save first
layer = doc.place(r"C:\in\photo.png")  # File > Import > Image as a new layer
layer.bounds                          # (left, top, right, bottom) of its pixels, None if empty
layer.translate(200, -50)             # move its pixels (one undo step; raster layers)
layer.resize(0.5), layer.resize(2, 1) # scale around the center of its bounds (same)
layer.flip("horizontal")              # in place; doc.flip("vertical"), doc.rotate(90 / 180 / -90): whole canvas
doc.crop()                            # canvas to the selection
doc.copy(), doc.cut(), doc.paste()    # paste returns the new layer
csp.run("Layer/Duplicate Layer")      # any main-menu command, like clicking it (dialogs wait until closed)
csp.enabled("Edit/Undo")              # greyed out or not
csp.commands()                        # {"Edit/Undo": (command id, param), ...}
```
These menu-backed methods select the layer first and only work on the active canvas (else `CspError`).
`csp.current_layer()`, `csp.new_raster_layer()` and `csp.new_folder()` are shortcuts for the active canvas.
Failures raise `csp.CspError`; calling `csp` off CSP's UI thread raises it too.

## License
GPL-3.0, see `LICENSE`.
