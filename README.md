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
folder = doc.new_folder("group")
doc.active_layer.name = "renamed"     # not undoable
layer.opacity = 50                    # 0..100; also blend_mode, visible, locked, lock_transparency, clip
doc.active_layer = layer              # select it
doc.layers, folder.children, layer.parent, layer.is_folder   # the layer tree, top to bottom
with doc.undo_group("My script"):      # everything inside is one undo step (csp.undo_group: active canvas)
    ...
csp.run("Layer/Duplicate Layer")      # any main-menu command, like clicking it (dialogs wait until closed)
csp.enabled("Edit/Undo")              # greyed out or not
csp.commands()                        # {"Edit/Undo": (command id, param), ...}
```
`csp.current_layer()`, `csp.new_raster_layer()` and `csp.new_folder()` are shortcuts for the active canvas.
Failures raise `csp.CspError`; calling `csp` off CSP's UI thread raises it too.

## License
GPL-3.0, see `LICENSE`.
