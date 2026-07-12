import time

import bpy
import gpu
from bpy_extras import view3d_utils
from mathutils import Vector

PREVIEW_MARKER = "plume_forge_preview"
PREVIEW_NAME = "PlumeForge Preview"
_PREVIEWS = {}
_DRAW_HANDLE = None
_SHADER = None
_VERTEX_FORMAT = None


def update_density_points(domain, data, payload):
    started = time.perf_counter()
    preview = data.get("preview") or {}
    if preview.get("type") != "density_points" or not payload:
        clear_preview(domain)
        return 0.0

    count = int(preview.get("count", 0))
    if count <= 0:
        clear_preview(domain)
        return 0.0

    stride = int(preview.get("stride", 0))
    expected_bytes = count * stride * 4
    if stride != 3 or len(payload) != expected_bytes:
        raise RuntimeError(
            f"Invalid preview payload: count={count}, stride={stride}, "
            f"bytes={len(payload)}"
        )
    positions = memoryview(payload).cast("f", shape=[count, 3])
    vertex_buffer = gpu.types.GPUVertBuf(format=_vertex_format(), len=count)
    vertex_buffer.attr_fill("pos", positions)
    batch = gpu.types.GPUBatch(type="POINTS", buf=vertex_buffer)
    center = Vector((
        float(preview.get("center_x", domain.location.x)),
        float(preview.get("center_y", domain.location.y)),
        float(preview.get("center_z", domain.location.z)),
    ))

    _PREVIEWS[domain.name] = {
        "center": center,
        "world_size": _world_point_size(domain, preview),
        "color": _point_color(domain),
        "buffer": vertex_buffer,
        "batch": batch,
    }
    _ensure_draw_handler()
    _tag_viewports()
    return (time.perf_counter() - started) * 1000.0


def show_preview_payload(context, domain, data, payload):
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    try:
        return update_density_points(domain, data, payload)
    finally:
        _restore_selection(context, domain, selected, active)


def clear_preview(domain):
    _PREVIEWS.pop(domain.name, None)
    _remove_legacy_preview_objects(domain.name)
    if not _PREVIEWS:
        _remove_draw_handler()
    _tag_viewports()


def clear_all_previews():
    _PREVIEWS.clear()
    _remove_draw_handler()
    for obj in list(bpy.data.objects):
        if obj.get(PREVIEW_MARKER):
            _remove_object(obj)
    _tag_viewports()


def _draw_previews():
    if not _PREVIEWS:
        return
    shader = _shader()
    gpu.state.blend_set("ALPHA")
    try:
        for preview in tuple(_PREVIEWS.values()):
            gpu.state.point_size_set(_screen_point_size(preview))
            shader.bind()
            shader.uniform_float("color", preview["color"])
            preview["batch"].draw(shader)
    finally:
        gpu.state.point_size_set(1.0)
        gpu.state.blend_set("NONE")


def _ensure_draw_handler():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is None:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_previews,
            (),
            "WINDOW",
            "POST_VIEW",
        )


def _remove_draw_handler():
    global _DRAW_HANDLE
    if _DRAW_HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, "WINDOW")
        except (ReferenceError, ValueError):
            pass
        _DRAW_HANDLE = None


def _shader():
    global _SHADER
    if _SHADER is None:
        _SHADER = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _SHADER


def _vertex_format():
    global _VERTEX_FORMAT
    if _VERTEX_FORMAT is None:
        _VERTEX_FORMAT = gpu.types.GPUVertFormat()
        _VERTEX_FORMAT.attr_add(
            id="pos",
            comp_type="F32",
            len=3,
            fetch_mode="FLOAT",
        )
    return _VERTEX_FORMAT


def _world_point_size(domain, preview):
    base = max(0.001, float(preview.get("point_size", 0.1)))
    scale = max(0.05, float(getattr(domain.plume_forge, "preview_dot_size", 1.0)))
    return base * scale * 0.1


def _screen_point_size(preview):
    region = bpy.context.region
    region_data = bpy.context.region_data
    if region is None or region_data is None:
        return 1.0
    center = preview["center"]
    view_right = region_data.view_matrix.inverted().col[0].xyz.normalized()
    first = view3d_utils.location_3d_to_region_2d(region, region_data, center)
    second = view3d_utils.location_3d_to_region_2d(
        region,
        region_data,
        center + view_right * preview["world_size"],
    )
    if first is None or second is None:
        return 1.0
    return max(1.0, min(128.0, (second - first).length))


def _point_color(domain):
    props = domain.plume_forge
    color = tuple(float(value) for value in getattr(props, "preview_color", (0.35, 0.65, 1.0)))
    opacity = max(0.0, min(1.0, float(getattr(props, "preview_opacity", 0.65))))
    return (*color[:3], opacity)


def _restore_selection(context, domain, selected, active):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if bpy.data.objects.get(obj.name):
            obj.select_set(True)
    if active and bpy.data.objects.get(active.name):
        context.view_layer.objects.active = active
    elif domain and bpy.data.objects.get(domain.name):
        context.view_layer.objects.active = domain


def _remove_legacy_preview_objects(domain_name):
    for obj in list(bpy.data.objects):
        if obj.get(PREVIEW_MARKER) == domain_name:
            _remove_object(obj)


def _remove_object(obj):
    data_block = obj.data if obj.type in {"MESH", "POINTCLOUD"} else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if data_block and data_block.users == 0:
        if data_block.id_type == "POINTCLOUD":
            bpy.data.pointclouds.remove(data_block)
        elif data_block.id_type == "MESH":
            bpy.data.meshes.remove(data_block)


def _tag_viewports():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
