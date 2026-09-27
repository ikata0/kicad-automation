"""finish: references beside their parts, board name, title block from the schematic."""
import sys

import kat

PCB = sys.argv[1]
with kat.pcb.edit(PCB) as b:
    b.place_refs()
    title = b.title_from_schematic()
    b.board_text(f"TIA rev {title.get('rev', '')}", (16, 18), size=1.0)
