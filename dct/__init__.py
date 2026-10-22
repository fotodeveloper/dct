from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dctracker")
except PackageNotFoundError:
    __version__ = "1.7.0"
