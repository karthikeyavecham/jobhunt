"""Adapter registry.

Sources are rows in the `sources` table; `adapter` names the class that knows
how to talk to it. Adding a source type means adding one entry here.
"""

from .arbeitnow import ArbeitnowAdapter
from .arbeitsagentur import ArbeitsagenturAdapter
from .ats import ATS_ADAPTERS
from .base import Adapter, RawJob, SourceError, html_to_text, markdown_to_text
from .manual import ManualAdapter

ADAPTERS = {
    ArbeitnowAdapter.name: ArbeitnowAdapter,
    ArbeitsagenturAdapter.name: ArbeitsagenturAdapter,
    ManualAdapter.name: ManualAdapter,
}
ADAPTERS.update({cls.name: cls for cls in ATS_ADAPTERS})


def get_adapter(name):
    """Instantiate an adapter by registry name."""
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise SourceError("Unknown adapter '{0}'".format(name))


def adapter_choices():
    """(name, label) pairs for the config page dropdown."""
    return sorted((n, c.label) for n, c in ADAPTERS.items())


__all__ = [
    "ADAPTERS", "Adapter", "RawJob", "SourceError",
    "get_adapter", "adapter_choices", "html_to_text", "markdown_to_text",
]
