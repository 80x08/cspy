# Paints seven color bars across the canvas on a new layer, as one undo step. Your main color is kept.
BARS = [(192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0), (192, 0, 192), (192, 0, 0), (0, 0, 192)]

doc = csp.active_document()
with doc.undo_group("cspy: color bars"):
    doc.new_raster_layer("color bars")
    for i, rgb in enumerate(BARS):
        left, right = doc.width * i // len(BARS), doc.width * (i + 1) // len(BARS)
        doc.select_rect(left, 0, right, doc.height)
        doc.fill(rgb)
    doc.deselect()
print(f"Painted {len(BARS)} bars on 'color bars'. Edit > Undo removes them in one step.")
