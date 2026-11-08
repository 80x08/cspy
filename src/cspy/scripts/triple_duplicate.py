# Duplicates the selected layer three times with the Layer menu's own command, as one undo step.
with csp.undo_group("cspy: duplicate x3"):
    for _ in range(3):
        csp.run("Layer/Duplicate Layer")
print(f"Now on {csp.current_layer().name!r}. Edit > Undo removes all three copies.")
