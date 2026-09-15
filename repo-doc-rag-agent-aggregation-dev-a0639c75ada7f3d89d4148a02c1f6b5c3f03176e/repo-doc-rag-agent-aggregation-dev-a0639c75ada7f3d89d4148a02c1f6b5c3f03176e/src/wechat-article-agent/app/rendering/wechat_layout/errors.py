class LayoutError(RuntimeError):
    """The deterministic renderer and its fallback could not produce safe HTML."""


class ThemeRegistryError(LayoutError):
    """A configured theme is invalid or unavailable."""
