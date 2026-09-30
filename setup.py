"""Bundle the canonical top-level agent assets as installed package resources."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def _assets(self):
        root = Path(__file__).parent / "skills"
        files = [root / "AGENTS.template.md"]
        for name in ("kicad-design", "kicad-schematic", "kicad-layout"):
            files.extend(sorted((root / name).rglob("*.md")))
        return [(p, Path(self.build_lib) / "kat" / "_agent_assets" / p.relative_to(root))
                for p in files]

    def run(self):
        super().run()
        for source, destination in self._assets():
            self.mkpath(str(destination.parent))
            self.copy_file(str(source), str(destination))

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + [str(dst) for _, dst in self._assets()]


setup(cmdclass={"build_py": BuildPy})
