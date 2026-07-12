import bpy
from bpy.props import StringProperty

from .utils import runtime_validation_error


class PlumeForgePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    executable_path: StringProperty(
        name="Bridge Executable",
        description="Optional path to a custom Plume Forge bridge executable",
        subtype="FILE_PATH",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "executable_path")
        error = runtime_validation_error()
        status = layout.row()
        status.alert = bool(error)
        status.label(
            text=error or "Bundled bridge and Flow runtime found",
            icon="ERROR" if error else "CHECKMARK",
        )


def register():
    try:
        bpy.utils.unregister_class(PlumeForgePreferences)
    except RuntimeError:
        pass
    bpy.utils.register_class(PlumeForgePreferences)


def unregister():
    try:
        bpy.utils.unregister_class(PlumeForgePreferences)
    except RuntimeError:
        pass
