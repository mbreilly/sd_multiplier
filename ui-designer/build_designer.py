#!/usr/bin/env python3
"""Builds sdmult-colors.html: the colour designer with the real GUI embedded (demo mode)."""
import json, os
here = os.path.dirname(os.path.abspath(__file__))
gui = open(os.path.join(here, "..", "sdmult_gui.html"), encoding="utf-8").read()
tpl = open(os.path.join(here, "designer_template.html"), encoding="utf-8").read()
out = tpl.replace("__GUI_JSON__", json.dumps(gui).replace("</", "<\\/"))
open(os.path.join(here, "sdmult-colors.html"), "w", encoding="utf-8").write(out)
print("wrote sdmult-colors.html", len(out), "bytes")
