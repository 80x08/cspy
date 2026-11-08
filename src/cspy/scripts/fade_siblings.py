# Sets every layer next to the selected one (same folder, including itself) to 50% opacity, as one undo step.
doc = csp.active_document()
current = doc.active_layer
siblings = current.parent.children if current.parent else doc.layers
with doc.undo_group("cspy: fade siblings"):
    for layer in siblings:
        if not layer.locked:
            layer.opacity = 50
print(f"Set {len(siblings)} layers to 50%. Edit > Undo restores them in one step.")
