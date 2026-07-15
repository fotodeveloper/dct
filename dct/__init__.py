from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("dctracker")
except PackageNotFoundError:
    __version__ = "1.1.0"
