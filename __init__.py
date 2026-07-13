bl_info = {
    "name": "PlumeForge",
    "author": "PlumeForge",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "Properties > Physics",
    "description": "PhysX Flow smoke simulation addon",
    "category": "Physics",
}

from . import properties
from . import operators
from . import ui
from . import preferences


def register():
    properties.register()
    operators.register()
    ui.register()
    preferences.register()


def unregister():
    preferences.unregister()
    ui.unregister()
    operators.unregister()
    properties.unregister()
