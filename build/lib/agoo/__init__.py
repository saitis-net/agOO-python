# agoo/__init__.py
#
# Makes `agoo` a Python package and exposes the main client class at the
# top-level so callers can write:
#
#   from agoo import Agoo
#
# instead of the longer `from agoo.client import Agoo`.

from .client import Agoo

__all__ = ["Agoo"]
