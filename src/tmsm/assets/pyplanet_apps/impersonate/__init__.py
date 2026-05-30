"""Impersonate addon: lets a master temporarily lower their effective tmsm
permission level so they can verify how the UI looks for operators / admins /
players. The override is wiped on disconnect and on PyPlanet restart.
"""
from .app import ImpersonateApp  # noqa: F401
