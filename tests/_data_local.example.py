"""Template for local test-artifact paths — copy to ``_data_local.py`` and edit.

``tests/_data_local.py`` is gitignored (never committed): it lets the integration
tests find local artifacts on your machine without exporting ``$TT_EMU_*`` env vars,
while keeping every machine-specific path out of this public repo. Define any subset
of the names below (each a ``str`` or ``pathlib.Path``); omit or leave ``None`` to let
that artifact resolve from its env var / download / skip instead. See ``_data.py``.
"""

#: Firmware ``.upd`` containers.
FIRMWARE = None  # e.g. "/path/to/update3202MT.upd"  (2N "MT")
FIRMWARE_ZC3201 = None  # e.g. "/path/to/update.upd"  (1st-gen ZC3201)

#: A tiptoi game directory holding ``<name>.gme`` + tttool ``<name>.yaml``.
GAME_DIR = None  # e.g. "/path/to/tiptoi-taschenrechner"

#: Hand-built ZC3201 test game (product 42, content OIDs 8065-8067).
GME_ZC3201 = None  # e.g. "/path/to/example.gme"

#: A second, more complex ``.gme`` used to cross-check the idle-restart bug.
GME2 = None  # e.g. "/path/to/WWW Bauernhof.gme"

#: Reference provisioned NAND image the provisioning test diffs against.
REF_NAND_IMG = None  # e.g. "/path/to/producer_nand.img"
