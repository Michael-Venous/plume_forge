import os
import time

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator

from .bake_state import mark_bake_cancelled, mark_bake_complete, mark_bake_running
from .cache import (
    cache_lock,
    invalidate_cache,
    invalidate_preview_cache,
    recover_cache_lock,
)
from .exporters import build_session, session_structure_signature
from .importers import (
    VOLUME_MARKER,
    delete_generated_data,
    delete_temporary_writes,
    import_sequence,
    migrate_legacy_cache,
)
from .jobs import FrameRangeJob
from .preview import clear_all_previews, clear_preview, show_preview_payload
from .utils import (
    base_output_directory_for_object,
    output_directory_for_object,
    simulation_frame_range,
)
from .runtime import (
    active_job,
    active_mode,
    claim_job,
    clear_geometry_revisions,
    record_geometry_updates,
    release_job,
)


class PLUME_FORGE_OT_bake(FrameRangeJob, Operator):
    bl_idname = "plume_forge.bake"
    bl_label = "Bake"
    bl_description = "Bake the Plume Forge simulation range to an imported VDB sequence"

    _timer = None
    _worker = None
    _domain_name = ""
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = True
    _participants = ()
    _prefix = "plume_forge_"
    _started_at = 0.0
    _submitted = False
    _completed_frames = ()
    _timer_interval = 0.001

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(domain and not active_job())

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Plume Forge domain")
            return {"CANCELLED"}

        self._domain_name = domain.name
        try:
            claim_job(self, "baking")
            invalidate_cache(domain)
            invalidate_preview_cache(domain)
            self._configure_job(
                context,
                domain,
                clear_cache=True,
                write_vdb=True,
                preview_enabled=bool(domain.plume_forge.preview_bake),
                show_progress=True,
            )
        except Exception as error:
            release_job(self)
            self._cleanup_volume_staging()
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        mark_bake_running(domain)
        domain.plume_forge.baked_frames = 0
        return self._start_modal(context)

    def _after_frame_complete(self, context, domain, completed, data, payload):
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                context,
                domain,
                data,
                payload,
            )

    def _complete(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        try:
            _import_cache(context, domain, self._directory, self._prefix)
        except Exception as error:
            if domain:
                mark_bake_cancelled(domain)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        if domain:
            if self._stop_requested:
                mark_bake_cancelled(domain)
            else:
                mark_bake_complete(domain)
            domain.plume_forge.bake_elapsed = time.monotonic() - self._started_at
            _activate(context, domain)
        self.report({"INFO"}, "Plume Forge bake stopped" if self._stop_requested else "Plume Forge bake complete")
        return {"FINISHED"}

    def _cancelled(self, context, data):
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            invalidate_cache(domain)
            clear_preview(domain)
            mark_bake_cancelled(domain)
            domain.plume_forge.bake_elapsed = time.monotonic() - self._started_at
        self._finish(context)
        message = data.get("message", "Bake stopped")
        stderr = data.get("stderr") or self._worker.stderr()
        if stderr:
            message = f"{message}: {stderr}"
        print(f"Plume Forge bake stopped: {message}")
        self.report({"ERROR"}, message)
        return {"CANCELLED"}


class PLUME_FORGE_OT_preview_play(FrameRangeJob, Operator):
    bl_idname = "plume_forge.preview_play"
    bl_label = "Preview Play"
    bl_description = "Play a looping live Flow preview over the Plume Forge simulation range without writing VDB files"

    _timer = None
    _worker = None
    _domain_name = ""
    _directory = ""
    _frame = 0
    _end_frame = 0
    _original_frame = 0
    _restore_frame = False
    _participants = ()
    _prefix = "plume_forge_"
    _started_at = 0.0
    _submitted = False
    _completed_frames = ()
    _session_signature = None
    _timer_interval = 0.001
    _next_submit_at = 0.0

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(
            domain
            and not active_job()
            and domain.plume_forge.simulation_state not in {"baking", "baked"}
        )

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Plume Forge domain")
            return {"CANCELLED"}
        if _is_playing(context):
            self.report({"WARNING"}, "Stop Blender playback before starting Plume Forge preview")
            return {"CANCELLED"}

        self._domain_name = domain.name
        try:
            claim_job(self, "previewing")
            self._start_preview_session(context, domain, clear_preview_points=True)
        except Exception as error:
            release_job(self)
            self._cleanup_volume_staging()
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        domain.plume_forge.baked_frames = 0
        print(f"Plume Forge preview started for {domain.name} frame {self._frame}")
        return self._start_modal(context)

    def _start_preview_session(self, context, domain, *, clear_preview_points=False):
        signature = session_structure_signature(domain)
        if clear_preview_points:
            clear_preview(domain)
        self._configure_job(
            context,
            domain,
            clear_cache=False,
            start_frame=self._preview_start_frame(context, domain),
            end_frame=simulation_frame_range(domain)[1],
            restore_frame=False,
            write_vdb=False,
            preview_enabled=True,
            resolution_scale=_preview_resolution_scale(domain),
            show_progress=False,
            keep_alive=True,
        )
        self._session_signature = signature
        self._next_submit_at = 0.0

    def _restart_preview_session(self, context, domain):
        signature = session_structure_signature(domain)
        start, end = simulation_frame_range(domain)
        self._frame = start
        self._end_frame = end
        self._submitted = False
        self._accepted = False
        self._stop_requested = False
        self._ending_session = False
        self._completed_frames = []
        self._resolution_scale = _preview_resolution_scale(domain)
        session, self._participants = build_session(
            context,
            start_frame=start,
            end_frame=end,
            domain=domain,
            write_vdb=False,
            preview_enabled=True,
            resolution_scale=self._resolution_scale,
            log_participants=False,
        )
        self._prefix = session["output_prefix"]
        self._session_signature = signature
        self._next_submit_at = 0.0
        self._loop_reset_started = time.perf_counter()
        self._worker.reset_session(session)

    def _ready_to_submit(self, context):
        return time.monotonic() >= getattr(self, "_next_submit_at", 0.0)

    def _submit_frame(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            raise RuntimeError("The active Plume Forge domain was deleted")
        signature = session_structure_signature(domain)
        if signature != self._session_signature:
            print(f"Plume Forge preview restarted for {domain.name}: session structure changed")
            if getattr(self, "_worker", None):
                self._worker.close()
            self._cleanup_volume_staging()
            self._started_at = time.monotonic()
            self._completed_frames = []
            self._submitted = False
            self._accepted = False
            self._stop_requested = False
            self._ending_session = False
            self._start_preview_session(context, domain, clear_preview_points=True)
            return
        super()._submit_frame(context)

    def _preview_start_frame(self, context, domain):
        start, end = simulation_frame_range(domain)
        current = int(context.scene.frame_current)
        return current if start <= current <= end else start

    def _after_frame_complete(self, context, domain, completed, data, payload):
        if domain:
            data["blender_preview_upload_ms"] = show_preview_payload(
                context,
                domain,
                data,
                payload,
            )
        started = getattr(self, "_last_frame_submit_started", time.monotonic())
        self._next_submit_at = started + _scene_frame_duration(context.scene)

    def _complete(self, context):
        domain = bpy.data.objects.get(self._domain_name)
        self._finish(context)
        if domain:
            domain.plume_forge.simulation_state = "idle"
            domain.plume_forge.bake_elapsed = time.monotonic() - self._started_at
        print(f"Plume Forge preview complete for {self._domain_name}")
        return {"FINISHED"}

    def _end_of_range(self, context, completed):
        domain = bpy.data.objects.get(self._domain_name)
        if domain is None:
            return self._cancelled(context, {"message": "The active Plume Forge domain was deleted"})
        start, _end = simulation_frame_range(domain)
        try:
            context.scene.frame_set(start)
            self._restart_preview_session(context, domain)
        except Exception as error:
            return self._cancelled(context, {"message": str(error)})
        return None

    def _cancelled(self, context, data):
        domain = bpy.data.objects.get(self._domain_name)
        if domain:
            domain.plume_forge.simulation_state = "idle"
            domain.plume_forge.bake_elapsed = time.monotonic() - self._started_at
        self._finish(context)
        message = data.get("message", "Preview stopped")
        stderr = data.get("stderr") or self._worker.stderr()
        if stderr:
            message = f"{message}: {stderr}"
        print(f"Plume Forge preview stopped: {message}")
        return {"CANCELLED"}


class PLUME_FORGE_OT_preview_stop(Operator):
    bl_idname = "plume_forge.preview_stop"
    bl_label = "Preview Stop"
    bl_description = "Stop the active Plume Forge live preview and close its bridge process"

    @classmethod
    def poll(cls, context):
        return active_mode() == "previewing"

    def execute(self, context):
        job = active_job() if active_mode() == "previewing" else None
        if job is None:
            self.report({"WARNING"}, "This domain has no active preview")
            return {"CANCELLED"}
        _cancel_job(context, job)
        return {"FINISHED"}


def _cancel_job(context, job):
    job._finished = True
    domain = bpy.data.objects.get(getattr(job, "_domain_name", ""))
    if domain:
        domain.plume_forge.simulation_state = "idle"
    job.request_cancel()
    if getattr(job, "_worker", None):
        job._worker.close()
    job._finish(context)


def _restore_selection(context, domain, selected, active):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if bpy.data.objects.get(obj.name):
            obj.select_set(True)
    if active and bpy.data.objects.get(active.name):
        context.view_layer.objects.active = active
    else:
        context.view_layer.objects.active = domain


def _import_cache(context, domain, directory, prefix):
    if domain is None:
        return
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    clear_preview(domain)
    props = domain.plume_forge
    import_sequence(
        directory,
        simulation_frame_range(domain)[0],
        prefix,
        material=props.volume_material,
        selectable=props.volume_selectable,
    )
    _restore_selection(context, domain, selected, active)


def _stop_active_job():
    job = active_job()
    if job is not None:
        _cancel_job(bpy.context, job)


class PLUME_FORGE_OT_delete(Operator):
    bl_idname = "plume_forge.delete"
    bl_label = "Delete Baked"
    bl_description = "Delete Plume Forge VDB cache files, imported volumes, and live preview dots for this domain"

    @classmethod
    def poll(cls, context):
        domain = _active_domain(context)
        return bool(domain and not active_job())

    def execute(self, context):
        domain = _active_domain(context)
        if not domain:
            self.report({"ERROR"}, "Select a Plume Forge domain")
            return {"CANCELLED"}
        prefix = domain.plume_forge.output_prefix or "plume_forge_"
        directory = output_directory_for_object(domain)
        try:
            legacy_directory = base_output_directory_for_object(domain)
            if os.path.normpath(legacy_directory) != os.path.normpath(directory):
                with cache_lock(legacy_directory):
                    migrate_legacy_cache(legacy_directory, directory, prefix)
            with cache_lock(directory):
                invalidate_cache(domain)
                invalidate_preview_cache(domain)
                clear_preview(domain)
                delete_generated_data(directory, prefix)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        domain.plume_forge.simulation_state = "idle"
        domain.plume_forge.baked_frames = 0
        domain.plume_forge.bake_elapsed = 0.0
        return {"FINISHED"}


class _ActiveBakeOperator:
    @classmethod
    def poll(cls, context):
        return active_mode() == "baking"

    def _active_job(self, context):
        return active_job() if active_mode() == "baking" else None


class PLUME_FORGE_OT_stop(_ActiveBakeOperator, Operator):
    bl_idname = "plume_forge.stop"
    bl_label = "Stop"
    bl_description = "Stop the active bake and import the frames written so far"

    def execute(self, context):
        job = self._active_job(context)
        if job is None:
            self.report({"WARNING"}, "This domain has no active bake")
            return {"CANCELLED"}
        job.request_stop(context)
        return {"FINISHED"}


def _activate(context, obj):
    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _is_imported_volume(obj):
    return bool(obj and obj.get(VOLUME_MARKER))


def _active_domain(context):
    obj = context.object
    if (
        obj
        and hasattr(obj, "plume_forge")
        and obj.plume_forge.smoke_object_type == "domain"
        and not _is_imported_volume(obj)
    ):
        return obj
    return None


CLASSES = (
    PLUME_FORGE_OT_bake,
    PLUME_FORGE_OT_preview_play,
    PLUME_FORGE_OT_preview_stop,
    PLUME_FORGE_OT_delete,
    PLUME_FORGE_OT_stop,
)


def _remove_handler_named(handlers, name):
    for handler in list(handlers):
        if getattr(handler, "__name__", "") == name:
            handlers.remove(handler)


def _recover_orphaned_bakes():
    objects = getattr(bpy.data, "objects", None)
    if objects is None:
        return
    for obj in objects:
        if not hasattr(obj, "plume_forge"):
            continue
        props = obj.plume_forge
        if props.smoke_object_type != "domain":
            continue
        directory = output_directory_for_object(obj)
        recover_cache_lock(directory)
        delete_temporary_writes(
            directory,
            props.output_prefix or "plume_forge_",
        )
        if props.simulation_state == "baking":
            props.simulation_state = "stopped"


@persistent
def _recover_orphaned_bakes_after_load(_dummy=None):
    _recover_orphaned_bakes()


@persistent
def _clear_previews_before_load(_dummy=None):
    _stop_active_job()
    clear_all_previews()
    clear_geometry_revisions()


@persistent
def _track_geometry_updates(_scene, depsgraph):
    record_geometry_updates(depsgraph)


def _is_playing(context):
    return bool(context.screen and context.screen.is_animation_playing)


def _scene_frame_duration(scene):
    fps = float(getattr(scene.render, "fps", 24.0))
    fps_base = float(getattr(scene.render, "fps_base", 1.0))
    if fps <= 0.0 or fps_base <= 0.0:
        return 1.0 / 24.0
    return fps_base / fps


def _preview_resolution_scale(domain):
    percent = float(getattr(domain.plume_forge, "preview_resolution_percent", 100.0))
    return max(0.0, min(100.0, percent)) / 100.0


@persistent
def _cancel_preview_before_native_playback(_scene=None, _depsgraph=None):
    if active_mode() != "previewing":
        return
    print("Plume Forge preview stopped because Blender playback started")
    _cancel_job(bpy.context, active_job())


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    _recover_orphaned_bakes()
    _remove_handler_named(bpy.app.handlers.load_post, "_recover_orphaned_bakes_after_load")
    if _recover_orphaned_bakes_after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_recover_orphaned_bakes_after_load)
    _remove_handler_named(bpy.app.handlers.load_pre, "_clear_previews_before_load")
    if _clear_previews_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_previews_before_load)
    _remove_handler_named(bpy.app.handlers.depsgraph_update_post, "_track_geometry_updates")
    if _track_geometry_updates not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_track_geometry_updates)
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_cancel_preview_before_native_playback")
    if _cancel_preview_before_native_playback not in bpy.app.handlers.animation_playback_pre:
        bpy.app.handlers.animation_playback_pre.append(_cancel_preview_before_native_playback)


def unregister():
    _stop_active_job()
    clear_all_previews()
    _remove_handler_named(bpy.app.handlers.load_pre, "_clear_previews_before_load")
    _remove_handler_named(bpy.app.handlers.load_post, "_recover_orphaned_bakes_after_load")
    _remove_handler_named(bpy.app.handlers.depsgraph_update_post, "_track_geometry_updates")
    _remove_handler_named(bpy.app.handlers.animation_playback_pre, "_cancel_preview_before_native_playback")
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
