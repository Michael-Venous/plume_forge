import glob
import os
import shutil
import time

import bpy
try:
    import openvdb
except ImportError:
    openvdb = None

PREFIX = "plume_forge_"
IMPORT_DIRECTORY = ".plume_forge_import"
VOLUME_MARKER = "plume_forge_volume"
VOLUME_PREFIX_MARKER = "plume_forge_volume_prefix"


def delete_generated_data(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if obj.get(VOLUME_MARKER) == normalized:
            volume = obj.data if obj.type == "VOLUME" else None
            bpy.data.objects.remove(obj, do_unlink=True)
            if volume and volume.users == 0:
                bpy.data.volumes.remove(volume)

    if os.path.isdir(directory):
        for path in glob.glob(os.path.join(directory, f"{prefix}*.vdb")):
            os.remove(path)
        delete_temporary_writes(directory, prefix)
        _delete_import_copies(directory)


def delete_temporary_writes(directory, prefix=PREFIX):
    for path in glob.glob(os.path.join(directory, f"{prefix}*.vdb.tmp")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def migrate_legacy_cache(legacy_directory, directory, prefix=PREFIX):
    legacy_directory = os.path.normpath(legacy_directory)
    directory = os.path.normpath(directory)
    if legacy_directory == directory:
        return False
    legacy_files = sorted(
        glob.glob(os.path.join(legacy_directory, f"{prefix}*.vdb"))
    )
    if not legacy_files:
        return False
    if glob.glob(os.path.join(directory, f"{prefix}*.vdb")):
        return False

    os.makedirs(directory, exist_ok=True)
    for source in legacy_files:
        os.replace(source, os.path.join(directory, os.path.basename(source)))
    _remove_volume_for_directory(legacy_directory, prefix)
    print(
        f"Plume Forge migrated {len(legacy_files)} legacy VDB frame(s) to "
        f"{directory}"
    )
    return True


def add_density_material(volume_object):
    material = bpy.data.materials.new("PlumeForge Material")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    volume_info = nodes.new("ShaderNodeVolumeInfo")
    shader = nodes.new("ShaderNodeVolumePrincipled")
    output = nodes.new("ShaderNodeOutputMaterial")
    volume_info.location = (-360, 0)
    shader.location = (-100, 0)
    output.location = (180, 0)

    links.new(volume_info.outputs["Density"], shader.inputs["Density"])
    links.new(shader.outputs["Volume"], output.inputs["Volume"])
    volume_object.data.materials.append(material)


def import_sequence(directory, frame_start, prefix=PREFIX, material=None, selectable=True):
    files = sorted(glob.glob(os.path.join(directory, f"{prefix}*.vdb")))
    if not files:
        raise RuntimeError("The bridge completed without writing VDB files")
    files = _files_with_grids(files)
    if not files:
        raise RuntimeError("The bridge wrote VDB files, but none contained readable grids")

    previous_active = bpy.context.view_layer.objects.active
    previous_selection = list(bpy.context.selected_objects)
    _remove_volume_for_directory(directory, prefix)
    import_directory, import_files = _stage_import_sequence(directory, files)
    existing = set(bpy.data.objects)
    bpy.ops.object.volume_import(
        filepath=import_files[0],
        files=[{"name": os.path.basename(path)} for path in import_files],
        directory=import_directory,
        use_sequence_detection=True,
    )

    created = [obj for obj in bpy.data.objects if obj not in existing and obj.type == "VOLUME"]
    if not created:
        _restore_selection(previous_selection, previous_active)
        raise RuntimeError("Blender did not create a Volume object")

    volume_object = created[0]
    volume_object.name = "PlumeForge Volume"
    volume_object.location = (0.0, 0.0, 0.0)
    volume_object[VOLUME_MARKER] = os.path.normpath(directory)
    volume_object[VOLUME_PREFIX_MARKER] = prefix
    sequence_start = _frame_number(files[0], prefix) or frame_start
    _configure_volume(volume_object, import_files[0], len(import_files), sequence_start)
    volume_object.select_set(False)
    volume_object.hide_select = not bool(selectable)
    if material is not None:
        volume_object.data.materials.append(material)
    else:
        add_density_material(volume_object)
    _restore_selection(previous_selection, previous_active)
    return volume_object


def _files_with_grids(files):
    started = time.perf_counter()
    valid = []
    skipped = []
    for path in files:
        try:
            if _grid_metadata(path):
                valid.append(path)
        except Exception:
            skipped.append(path)
    if skipped:
        print(
            f"Plume Forge skipped {len(skipped)} unreadable VDB frame(s): "
            + ", ".join(os.path.basename(path) for path in skipped)
        )
    print(
        "Plume Forge VDB metadata validation: "
        f"{len(valid)}/{len(files)} frames in "
        f"{(time.perf_counter() - started) * 1000.0:.3f}ms"
    )
    return valid


def _grid_metadata(path):
    if openvdb is not None:
        return openvdb.readAllGridMetadata(path)
    volume = bpy.data.volumes.new("PlumeForge VDB Probe")
    try:
        volume.filepath = path
        volume.grids.load()
        return tuple(volume.grids)
    finally:
        bpy.data.volumes.remove(volume)


def _frame_number(path, prefix):
    name = os.path.basename(path)
    if not name.startswith(prefix) or not name.endswith(".vdb"):
        return None
    try:
        return int(name[len(prefix):-4])
    except ValueError:
        return None


def _stage_import_sequence(directory, files):
    root = os.path.join(directory, IMPORT_DIRECTORY)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, str(time.time_ns()))
    os.makedirs(target, exist_ok=False)

    staged = []
    for path in files:
        destination = os.path.join(target, os.path.basename(path))
        try:
            os.symlink(path, destination)
        except OSError:
            shutil.copy2(path, destination)
        staged.append(destination)

    _prune_import_copies(root, target)
    return target, staged


def _delete_import_copies(directory):
    root = os.path.join(directory, IMPORT_DIRECTORY)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def _prune_import_copies(root, keep):
    keep = os.path.normpath(keep)
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.normpath(path) == keep:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _restore_selection(selected, active):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if active and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def _configure_volume(volume_object, filepath, frame_count, frame_start):
    volume = volume_object.data
    volume.filepath = bpy.path.relpath(filepath) if bpy.data.filepath else filepath
    volume.frame_start = frame_start
    volume.frame_duration = frame_count
    volume.sequence_mode = "EXTEND"
    volume.display.density = 1.0
    volume.display.use_slice = False
    volume.display.interpolation_method = "LINEAR"
    volume.render.space = "OBJECT"
    volume.render.step_size = 0.0
    scene = bpy.context.scene
    current_frame = scene.frame_current
    load_frame = frame_start + max(0, frame_count - 1)
    try:
        if current_frame != load_frame:
            scene.frame_set(load_frame)
        volume.grids.load()
    finally:
        if scene.frame_current != current_frame:
            scene.frame_set(current_frame)
    if len(volume.grids) == 0:
        volume.grids.load()
    volume.update_tag()
    volume_object.update_tag()


def _remove_volume_for_directory(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        volume = obj.data if obj.type == "VOLUME" else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if volume and volume.users == 0:
            bpy.data.volumes.remove(volume)


def _is_generated_volume(obj, directory, prefix):
    marker = obj.get(VOLUME_MARKER)
    if marker:
        return (
            os.path.normpath(marker) == directory
            and obj.get(VOLUME_PREFIX_MARKER, prefix) == prefix
        )
    if obj.type != "VOLUME":
        return False
    filepath = getattr(obj.data, "filepath", "")
    if not filepath:
        return False
    absolute = os.path.normpath(bpy.path.abspath(filepath))
    import_root = os.path.normpath(os.path.join(directory, IMPORT_DIRECTORY))
    in_import_root = os.path.commonpath((absolute, import_root)) == import_root
    return (
        (os.path.dirname(absolute) == directory or in_import_root)
        and os.path.basename(absolute).startswith(prefix)
    )
