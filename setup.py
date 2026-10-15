"""Offline build guard: authored workflow contracts and outputs must agree."""
import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.editable_wheel import editable_wheel
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent


def check_workflows():
    # Do not import the runtime package or any optional dependency during build.
    runpy.run_path(str(ROOT / "dct/skill_render.py"))["check_freshness"](ROOT)


class CheckedBuildPy(build_py):
    def run(self):
        check_workflows()
        super().run()


class CheckedSdist(sdist):
    def run(self):
        check_workflows()
        super().run()


class CheckedEditableWheel(editable_wheel):
    def run(self):
        check_workflows()
        super().run()


setup(cmdclass={"build_py": CheckedBuildPy, "sdist": CheckedSdist, "editable_wheel": CheckedEditableWheel})
