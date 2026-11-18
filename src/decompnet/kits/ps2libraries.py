"""The EE libraries of the owner's extracted PlayStation 2 SDK."""

from pathlib import Path

from . import Kit, find


class Ps2Libraries(Kit):
    id = "ps2-libraries"
    name = "PlayStation 2 SDK libraries (your own copy)"
    platform = "PS2"
    description = "Choose the EE lib folder of your extracted SDK, or a folder of its EE .a archives."
    files = ("libkernl.a",)
    places = ("ee/lib", "MAINDIR/ee/lib")

    def locate(self, folder: Path) -> Path | None:
        """A full extraction uses ee/lib. An owner can also register a smaller collection of archives."""
        from ..consoles.ps2.sdk_libraries import AR_MAGIC

        roots = [find(folder, place, dirs=True) for place in self.places]
        for base in (*roots, folder):
            if base is None or not base.is_dir():
                continue
            for path in sorted(base.glob("*.a")):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    with path.open("rb") as stream:
                        if stream.read(len(AR_MAGIC)) == AR_MAGIC:
                            return base.resolve()
                except OSError:
                    continue
        return None


PS2_LIBRARIES = Ps2Libraries()
