# Saves each top-level layer (folders as one image) as its own transparent PNG in Pictures\cspy\<canvas>-layers.
import os

doc = csp.active_document()
folder = os.path.join(os.path.expanduser("~"), "Pictures", "cspy", f"{doc.name}-layers")
os.makedirs(folder, exist_ok=True)
for i, layer in enumerate(doc.layers):
    safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in layer.name)
    layer.image().save(os.path.join(folder, f"{i:02d} {safe}.png"))
print(f"Saved {len(doc.layers)} layers to\n{folder}")
