#!/usr/bin/env python3
"""Builds tools/apk2switch.py from the template, embedding the controls file."""
import os

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "apk2switch.template.py"), encoding="utf-8") as f:
    template = f.read()
with open(os.path.join(here, "..", "switch", "game_addons", "zz_switch_controls.rpy"), encoding="utf-8") as f:
    controls = f.read()

assert "'''" not in controls and "\\" not in controls
out = template.replace("@@CONTROLS_RPY@@", controls)
with open(os.path.join(here, "apk2switch.py"), "w", encoding="utf-8", newline="\n") as f:
    f.write(out)
print("wrote tools/apk2switch.py")
