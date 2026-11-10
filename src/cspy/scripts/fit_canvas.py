# Trims (or grows) the canvas to the current layer's pixels plus a 40 px margin (doc.resize_canvas).
# One undo step.
MARGIN = 40

doc = csp.active_document()
layer = doc.active_layer
box = layer.bounds
if box is None:
    print(f"'{layer.name}' is empty: nothing to fit to.")
else:
    left, top, right, bottom = box
    before = (doc.width, doc.height)
    doc.resize_canvas(right - left + 2 * MARGIN, bottom - top + 2 * MARGIN, x=left - MARGIN, y=top - MARGIN)
    print(f"Canvas {before[0]} x {before[1]} -> {doc.width} x {doc.height}, fitted to '{layer.name}'.")
