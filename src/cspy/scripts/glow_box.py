# Draws a box with two soft-looking rings around it, each on its own layer, as one undo step.
# Exercises select_rect, expand_selection (round and square corners) and fill.
doc = csp.active_document()
cx, cy = doc.width // 2, doc.height // 2
box = (cx - 150, cy - 100, cx + 150, cy + 100)

with doc.undo_group("cspy: glow box"):
    for name, grow, kind, rgb, opacity in (("outer ring", 40, 0, (255, 220, 120), 40),
                                            ("inner ring", 16, 1, (255, 170, 60), 70),
                                            ("box", 0, 0, (230, 90, 30), 100)):
        layer = doc.new_raster_layer(name)
        doc.select_rect(*box)
        if grow:
            doc.expand_selection(grow, kind=kind)
        doc.fill(rgb)
        layer.opacity = opacity
    doc.deselect()
print("Drew 'box' with 'inner ring' (square corners) and 'outer ring' (round corners).")
