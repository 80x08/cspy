# Makes 5 copies of the current layer, each smaller and shifted further right and down, as one undo step.
# Exercises layer.duplicate, layer.resize and layer.translate.
COPIES, STEP, SHRINK = 5, 60, 0.8

doc = csp.active_document()
layer = doc.active_layer
if layer.bounds is None:
    print(f"'{layer.name}' is empty: draw something on it first.")
else:
    with doc.undo_group("cspy: scatter copies"):
        copy = layer
        for i in range(1, COPIES + 1):
            copy = copy.duplicate()
            copy.name = f"{layer.name} copy {i}"
            copy.resize(SHRINK)
            copy.translate(STEP, STEP)
            copy.opacity = max(20, 100 - i * 15)
    print(f"Added {COPIES} copies of '{layer.name}'. Edit > Undo removes them in one step.")
