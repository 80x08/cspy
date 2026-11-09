# Exports the front canvas as a PNG into Pictures\cspy, named after the canvas and the time. No dialogs.
import os
import time

doc = csp.active_document()
folder = os.path.join(os.path.expanduser("~"), "Pictures", "cspy")
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, f"{doc.name}-{time.strftime('%Y%m%d-%H%M%S')}.png")
doc.export(path)
print(f"Saved {doc.width} x {doc.height} PNG:\n{path}")
