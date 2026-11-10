# Opens the "SAM selection" window (Segment Anything 3) on the current layer: pick the object there by clicks, a
# box or text ("hair") and press Apply; the result becomes the canvas selection (one undo step). A rectangle
# selection made before running this becomes the starting box. CSP stays usable meanwhile.
# Needs the SAM folder of the cspy repo set up (SAM\README.md); point CSPY_SAM at it if it isn't D:\cspy\SAM.
import os
import subprocess
import tempfile

here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
candidates = [os.environ.get("CSPY_SAM", ""), os.path.join(here, "..", "..", "..", "SAM"), r"D:\cspy\SAM"]
sam = next((os.path.abspath(c) for c in candidates if c and os.path.isfile(os.path.join(c, "sam_gui.py"))), None)
if not sam:
    raise csp.CspError("SAM folder not found: set the CSPY_SAM environment variable to it (see SAM\\README.md)")
pythonw = os.path.join(sam, ".venv", "Scripts", "pythonw.exe")
if not os.path.isfile(pythonw):
    raise csp.CspError(f"{sam} has no .venv yet: follow SAM\\README.md to set it up")

doc = csp.active_document()
layer = doc.active_layer
box = doc._selection_bounds() if csp.enabled("Select/Deselect") else None
folder = tempfile.mkdtemp(prefix="cspy-sam-")  # the window deletes it when it closes
path = os.path.join(folder, "layer.png")
layer.image().save(path)
args = [pythonw, os.path.join(sam, "sam_gui.py"), path, "--cleanup"]
if box:
    args += ["--box", *map(str, box)]
subprocess.Popen(args, cwd=sam, close_fds=True)
print(f"Opening the SAM selection window for '{layer.name}'" + (f" with box {box}" if box else "")
      + ". SAM 3 needs a few seconds to load.")
