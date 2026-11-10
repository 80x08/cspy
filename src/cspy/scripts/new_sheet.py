# Creates a new 2480 x 3508 (A4 at 300 dpi) white canvas without the File > New dialog, places the front
# canvas onto it as a layer, centers that layer and saves the result as a .clip in Pictures\cspy.
# Exercises new_document, export, place, bounds, translate and save_as.
import os
import tempfile
import time

source = csp.active_document()
folder = os.path.join(os.path.expanduser("~"), "Pictures", "cspy")
os.makedirs(folder, exist_ok=True)

with tempfile.TemporaryDirectory(prefix="cspy-") as tmp:
    png = os.path.join(tmp, f"{source.name}.png")
    source.export(png)
    sheet = csp.new_document(2480, 3508, dpi=300, name="sheet")
    layer = sheet.place(png)

left, top, right, bottom = layer.bounds
layer.translate((sheet.width - (right - left)) // 2 - left, (sheet.height - (bottom - top)) // 2 - top)
path = os.path.join(folder, f"sheet-{time.strftime('%Y%m%d-%H%M%S')}.clip")
sheet.save_as(path)
print(f"New {sheet.width} x {sheet.height} canvas at {sheet.resolution:g} dpi with '{source.name}' centered,\n"
      f"saved as {path}")
