# Paints a 24 px frame around the canvas edge on a new layer, in the main color, as one undo step.
# Exercises select_all, shrink_selection, invert_selection and fill.
WIDTH = 24

doc = csp.active_document()
with doc.undo_group("cspy: frame border"):
    doc.new_raster_layer("frame")
    doc.select_all()
    doc.shrink_selection(WIDTH, from_edges=True)
    doc.invert_selection()
    doc.fill()
    doc.deselect()
print(f"Painted a {WIDTH} px frame on the new 'frame' layer.")
