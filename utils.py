import os
import sys
import hashlib
import stat
import tempfile

import bpy


def addon_directory():
    return os.path.dirname(os.path.abspath(__file__))


def executable_path():
    name = "plume_forge_bridge.exe" if sys.platform == "win32" else "plume_forge_bridge"
    prefs = get_addon_preferences()
    if prefs and getattr(prefs, "executable_path", ""):
        return bpy.path.abspath(prefs.executable_path)
    return os.path.join(addon_directory(), "bin", name)


def required_flow_libraries():
    if sys.platform == "win32":
        return ("nvflow.dll", "nvflowext.dll")
    return ("libnvflow.so", "libnvflowext.so")


def runtime_validation_error():
    executable = executable_path()
    if not executable or not os.path.isfile(executable):
        return f"Plume Forge bridge executable is missing: {executable}"
    prefs = get_addon_preferences()
    if prefs and getattr(prefs, "executable_path", ""):
        if sys.platform != "win32" and not os.access(executable, os.X_OK):
            return f"Custom Plume Forge bridge is not executable: {executable}"
        return ""
    permission_error = _repair_bundled_executable(executable)
    if permission_error:
        return permission_error
    libs = os.path.join(addon_directory(), "bin", "libs")
    missing = [
        name for name in required_flow_libraries()
        if not os.path.isfile(os.path.join(libs, name))
    ]
    if missing:
        return "Plume Forge runtime is missing: " + ", ".join(missing)
    return ""


def _repair_bundled_executable(executable):
    if sys.platform == "win32" or os.access(executable, os.X_OK):
        return ""
    try:
        mode = os.stat(executable).st_mode
        os.chmod(executable, mode | stat.S_IXUSR)
    except OSError as error:
        return f"Plume Forge could not make its bridge executable: {error}"
    if not os.access(executable, os.X_OK):
        return (
            "Plume Forge bridge execution is blocked by the filesystem: "
            f"{executable}"
        )
    return ""


def process_environment():
    env = os.environ.copy()
    binary = os.path.join(addon_directory(), "bin")
    libs = os.path.join(addon_directory(), "bin", "libs")
    variable = "PATH" if sys.platform == "win32" else "LD_LIBRARY_PATH"
    previous = env.get(variable, "")
    paths = os.pathsep.join((libs, binary))
    env[variable] = paths if not previous else paths + os.pathsep + previous
    return env


def output_directory(context):
    return output_directory_for_object(context.object)


def _safe_cache_slot(value):
    value = (value or "main").strip()
    value = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    ).strip("._")
    return value or "main"


def base_output_directory_for_object(obj):
    configured = obj.plume_forge.output_dir.strip()
    if not configured:
        configured = "//plume_forge_cache/"
    resolved = bpy.path.abspath(configured)
    if os.path.isabs(resolved):
        return os.path.normpath(resolved)
    if not bpy.data.filepath:
        user_cache = bpy.utils.user_resource(
            "DATAFILES",
            path="plume_forge_cache",
            create=True,
        )
        if user_cache and os.path.isabs(user_cache):
            return os.path.normpath(user_cache)
        return os.path.join(tempfile.gettempdir(), "plume_forge_cache")
    return os.path.normpath(os.path.abspath(resolved))


def cache_identity_for_object(obj):
    props = obj.plume_forge
    identity = props.cache_id.strip()
    duplicate = any(
        other is not obj
        and hasattr(other, "plume_forge")
        and getattr(other.plume_forge, "cache_id", "").strip() == identity
        for other in bpy.data.objects
    ) if identity else False
    if not identity or duplicate:
        name = getattr(obj, "name_full", obj.name)
        identity = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
        props.cache_id = identity
    return identity


def output_directory_for_object(obj):
    return os.path.join(
        base_output_directory_for_object(obj),
        ".plume_forge_caches",
        cache_identity_for_object(obj),
        _safe_cache_slot(getattr(obj.plume_forge, "cache_slot", "main")),
    )


def simulation_frame_range(domain):
    props = domain.plume_forge
    start = int(getattr(props, "sim_start_frame", 1))
    end = int(getattr(props, "sim_end_frame", start))
    return start, max(start, end)


def get_addon_preferences():
    addon_name = __package__.split(".")[0]
    addon = bpy.context.preferences.addons.get(addon_name)
    return addon.preferences if addon else None
