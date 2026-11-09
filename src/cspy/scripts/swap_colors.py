# Swaps the main and sub drawing colors.
main, sub = csp.main_color(), csp.sub_color()
csp.set_main_color(sub)
csp.set_sub_color(main)
print(f"Main is now {sub}, sub is now {main}.")
