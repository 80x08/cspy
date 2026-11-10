# Moves the current layer's pixels to the middle of the canvas (layer.bounds + layer.translate). One undo step.
doc = csp.active_document()
layer = doc.active_layer
box = layer.bounds
if box is None:
    print(f"'{layer.name}' is empty: nothing to center.")
else:
    left, top, right, bottom = box
    dx = (doc.width - (right - left)) // 2 - left
    dy = (doc.height - (bottom - top)) // 2 - top
    if dx or dy:
        layer.translate(dx, dy)
    print(f"'{layer.name}': {box} -> {layer.bounds} (moved {dx}, {dy}).")
