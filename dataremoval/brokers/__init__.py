"""Broker plugin system — base class, registry, and config loader."""

from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel

from dataremoval.core.models import Listing, Profile

# ---------------------------------------------------------------------------
# Broker metadata (loaded from YAML config or defined in code)
# ---------------------------------------------------------------------------


class OptOutMethod(StrEnum):
    ONLINE_FORM = "online_form"
    EMAIL = "email"
    MAIL = "mail"
    ACCOUNT = "account"
    API = "api"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class BrokerInfo(BaseModel):
    """Static metadata about a broker site."""

    id: str  # unique slug, e.g. "whitepages"
    name: str  # display name
    url: str  # homepage
    category: str = "people_search"  # people_search, background, marketing
    opt_out_method: OptOutMethod = OptOutMethod.ONLINE_FORM
    opt_out_url: str = ""
    difficulty: Difficulty = Difficulty.MEDIUM
    expected_days: int = 3  # typical removal time
    recheck_days: int = 90  # how often to re-check
    mail_address: str = ""  # postal address for mail-in opt-out
    notes: str = ""


# ---------------------------------------------------------------------------
# Abstract broker plugin
# ---------------------------------------------------------------------------


class BrokerPlugin(ABC):
    """
    Base class for all broker plugins.

    To add a new broker:
      1. Create a file in dataremoval/brokers/  (e.g. spokeo.py)
      2. Subclass BrokerPlugin
      3. Implement info(), search(), submit_opt_out()
      4. Optionally implement check_status()

    The plugin is auto-discovered on import.
    """

    @abstractmethod
    def info(self) -> BrokerInfo:
        """Return static metadata about this broker."""
        ...

    @property
    def id(self) -> str:
        return self.info().id

    @abstractmethod
    async def search(self, profile: Profile) -> list[Listing]:
        """
        Search the broker site for listings matching the profile.
        Returns a list of Listing objects with URLs and extracted data.
        """
        ...

    @abstractmethod
    async def submit_opt_out(self, listing: Listing) -> bool:
        """
        Submit an opt-out / removal request for a listing.
        Returns True if the submission appeared to succeed.
        """
        ...

    async def check_status(self, listing: Listing) -> str:
        """
        Re-check whether a listing still exists.
        Returns 'removed', 'still_listed', or 'unknown'.
        Default: re-run search and check if URL still resolves.
        """
        return "unknown"

    def __repr__(self) -> str:
        return f"<BrokerPlugin: {self.id}>"


# ---------------------------------------------------------------------------
# Plugin registry — auto-discovers broker modules
# ---------------------------------------------------------------------------


class BrokerRegistry:
    """Discovers and manages broker plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, BrokerPlugin] = {}

    def register(self, plugin: BrokerPlugin) -> None:
        self._plugins[plugin.id] = plugin

    def get(self, broker_id: str) -> BrokerPlugin | None:
        return self._plugins.get(broker_id)

    def all(self) -> list[BrokerPlugin]:
        return list(self._plugins.values())

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    def discover(self) -> None:
        """Auto-import all modules in dataremoval.brokers to trigger registration."""
        import dataremoval.brokers as broker_pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(broker_pkg.__path__):
            if not modname.startswith("_"):
                importlib.import_module(f"dataremoval.brokers.{modname}")

    def __len__(self) -> int:
        return len(self._plugins)


# Global registry
registry = BrokerRegistry()


def register_broker(plugin: BrokerPlugin) -> BrokerPlugin:
    """Convenience: register a plugin instance on import."""
    registry.register(plugin)
    return plugin
