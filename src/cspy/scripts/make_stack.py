# Adds a folder with three raster layers at different opacities and blend modes. One Undo removes it all.
doc = csp.active_document()
with doc.undo_group("cspy: make stack"):
    folder = doc.new_folder("cspy stack")
    for name, opacity, blend in (("base", 100, 0), ("shade (Multiply)", 60, 2), ("light (Screen)", 35, 8)):
        layer = doc.new_raster_layer(name)  # lands inside the selected folder
        layer.opacity = opacity
        layer.blend_mode = blend
    doc.active_layer = folder
print(f"Made '{folder.name}' with {len(folder.children)} layers. Edit > Undo removes all of it in one step.")
