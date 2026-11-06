# SPDX-License-Identifier: GPL-3.0-or-later
"""Send to Creality: one button that turns visible parts into a printable STL.

Copies of the parts are merged into one solid, resized, placed with their
bottom at Z = 0, checked, exported and opened in Creality Print. The objects
in the scene are never changed.
"""

import ctypes
import ctypes.wintypes as wintypes
import glob
import os
import re
import subprocess
import sys
import time

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import single_instance
from .single_instance import set_single_instance

SOLID_TYPES = {"MESH", "CURVE", "FONT", "SURFACE", "META"}
WM_COPYDATA = 0x004A
SMTO_ABORTIFHUNG = 0x0002
GW_OWNER = 4
NEW_WINDOW_TIMEOUT = 20.0


class SendError(Exception):
    """A problem to show the user; nothing was exported."""


# Creality Print ------------------------------------------------------------

def _natural_key(path):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", path)]


def find_creality(prefs_path=""):
    if prefs_path and os.path.isfile(bpy.path.abspath(prefs_path)):
        return bpy.path.abspath(prefs_path)
    found = []
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if root:
            found += glob.glob(os.path.join(root, "Creality", "Creality Print*", "CrealityPrint.exe"))
    return sorted(found, key=_natural_key)[-1] if found else ""


class _CopyData(ctypes.Structure):
    _fields_ = [
        ("dwData", ctypes.c_size_t),
        ("cbData", wintypes.DWORD),
        ("lpData", ctypes.c_void_p),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
_user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
_user32.GetPropW.restype = wintypes.HANDLE
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t


def creality_windows():
    """Open Creality Print windows as {process id: (window handle, title)}."""
    found = {}

    def visit(hwnd, _param):
        if (_user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, GW_OWNER)
                and _user32.GetPropW(hwnd, "Instance_Hash_Major")):
            length = _user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, text, length + 1)
            if "CrealityPrint" in text.value:
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found[pid.value] = (hwnd, text.value)
        return True

    _user32.EnumWindows(_EnumProc(visit), 0)
    return found


def add_to_window(hwnd, exe, filepath):
    """Ask one Creality window to load a file, as a second launch of it would."""
    quote = lambda text: '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    text = ctypes.create_unicode_buffer(f"{quote(exe)};{quote(filepath)}")
    data = _CopyData(1, ctypes.sizeof(text), ctypes.cast(text, ctypes.c_void_p))
    answer = ctypes.c_size_t()
    return bool(_user32.SendMessageTimeoutW(
        hwnd, WM_COPYDATA, 0, ctypes.byref(data), SMTO_ABORTIFHUNG, 8000, ctypes.byref(answer)))


def _creality_config():
    pattern = os.path.join(
        os.environ.get("APPDATA", ""), "Creality", "Creality Print", "*", "Creality.conf")
    configs = glob.glob(pattern)
    return max(configs, key=os.path.getmtime) if configs else ""


def open_new_window(exe, filepath):
    """Open a file in its own window and return whether one appeared.

    With Creality's single instance option on, a new launch would hand the
    file to an open window, so the option is off only while this one starts.
    That window saves the option as off when it closes, so a small watcher
    outside Blender turns it back on afterwards.
    """
    path = _creality_config()
    single = False
    if path:
        with open(path, encoding="utf-8") as config:
            single = re.search(r'"single_instance"\s*:\s*true', config.read()) is not None
    before = set(creality_windows())
    if not single or not before:
        subprocess.Popen([exe, filepath], close_fds=True)
        return True
    new = set()
    set_single_instance(path, "false")
    try:
        launch = subprocess.Popen([exe, filepath], close_fds=True)
        deadline = time.monotonic() + NEW_WINDOW_TIMEOUT
        while time.monotonic() < deadline and not new:
            new = set(creality_windows()) - before
            time.sleep(0.25)
    finally:
        set_single_instance(path, "true")
    for pid in new or {launch.pid}:
        subprocess.Popen(
            [sys.executable, "-I", single_instance.__file__, str(pid), path],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
    return bool(new)


# Geometry ------------------------------------------------------------------

def collect_objects(context, only_selected):
    objects = context.selected_objects if only_selected else context.view_layer.objects
    return [o for o in objects if o.type in SOLID_TYPES and o.visible_get()]


def live_size(context, settings):
    """Printed size at the current height, from the objects' bounding boxes."""
    corners = [
        obj.matrix_world @ Vector(corner)
        for obj in collect_objects(context, settings.only_selected)
        for corner in obj.bound_box
    ]
    if not corners:
        return None
    low = [min(c[i] for c in corners) for i in range(3)]
    high = [max(c[i] for c in corners) for i in range(3)]
    tall = high[2] - low[2]
    if tall <= 0:
        return None
    factor = settings.height_mm / tall
    return tuple((high[i] - low[i]) * factor for i in range(3))


def world_mesh(obj, depsgraph):
    """A new mesh of the object as displayed (modifiers applied), in world space."""
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
    mesh.transform(obj.matrix_world)
    return mesh


def split_islands(mesh, label, temp_meshes):
    """Split into connected closed parts, so overlapping letters merge cleanly."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        bm.verts.ensure_lookup_table()
        seen = set()
        groups = []
        for start in bm.verts:
            if start.index in seen or not start.link_faces:
                continue
            seen.add(start.index)
            stack = [start]
            group = [start]
            while stack:
                vert = stack.pop()
                for edge in vert.link_edges:
                    other = edge.other_vert(vert)
                    if other.index not in seen:
                        seen.add(other.index)
                        stack.append(other)
                        group.append(other)
            groups.append(group)

        open_edges = sum(1 for e in bm.edges if e.is_boundary)
        if open_edges:
            raise SendError(f'"{label}" is not a closed solid ({open_edges} open edges)')

        parts = []
        for number, group in enumerate(groups):
            remap = {v.index: i for i, v in enumerate(group)}
            faces = {f.index: f for v in group for f in v.link_faces}
            part = bpy.data.meshes.new(f"_send_part_{number}")
            temp_meshes.append(part)
            part.from_pydata(
                [tuple(v.co) for v in group], [],
                [[remap[v.index] for v in f.verts] for f in faces.values()],
            )
            part.update()
            parts.append(part)
        return parts
    finally:
        bm.free()


def fix_normals_and_volume(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    volume = abs(bm.calc_volume(signed=True))
    bm.to_mesh(mesh)
    bm.free()
    return volume


def bounds(meshes):
    low = Vector((float("inf"),) * 3)
    high = Vector((float("-inf"),) * 3)
    for mesh in meshes:
        for vert in mesh.vertices:
            for axis in range(3):
                low[axis] = min(low[axis], vert.co[axis])
                high[axis] = max(high[axis], vert.co[axis])
    return low, high


def union_parts(context, parts, temp_meshes, temp_objects):
    """Union every part into the largest one and return the merged mesh."""
    if len(parts) == 1:
        return parts[0]

    parts = sorted(parts, key=lambda m: len(m.vertices), reverse=True)
    collection = context.scene.collection
    carrier = None
    for part in parts:
        obj = bpy.data.objects.new(part.name, part)
        collection.objects.link(obj)
        temp_objects.append(obj)
        if carrier is None:
            carrier = obj
            continue
        modifier = carrier.modifiers.new(part.name, "BOOLEAN")
        modifier.operation = "UNION"
        modifier.solver = "MANIFOLD"
        modifier.object = obj

    context.view_layer.update()
    depsgraph = context.evaluated_depsgraph_get()
    merged = bpy.data.meshes.new_from_object(carrier.evaluated_get(depsgraph))
    temp_meshes.append(merged)
    return merged


def inspect(mesh):
    """Return (non-manifold edges, separate pieces, volume)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        bad = sum(1 for e in bm.edges if not e.is_manifold)
        seen = set()
        pieces = 0
        for start in bm.verts:
            if start in seen:
                continue
            pieces += 1
            seen.add(start)
            stack = [start]
            while stack:
                vert = stack.pop()
                for edge in vert.link_edges:
                    other = edge.other_vert(vert)
                    if other not in seen:
                        seen.add(other)
                        stack.append(other)
        return bad, pieces, abs(bm.calc_volume(signed=True))
    finally:
        bm.free()


def build_print_mesh(context, objects, height_mm, temp_meshes, temp_objects):
    """Merge, check and scale. 1 Blender unit in the result is 1 mm."""
    depsgraph = context.evaluated_depsgraph_get()
    parts = []
    for obj in objects:
        whole = world_mesh(obj, depsgraph)
        temp_meshes.append(whole)
        parts += split_islands(whole, obj.name, temp_meshes)
    if not parts:
        raise SendError("Nothing to send: no solid geometry found")

    largest = max(fix_normals_and_volume(p) for p in parts)
    want_low, want_high = bounds(parts)

    merged = union_parts(context, parts, temp_meshes, temp_objects)

    size = want_high - want_low
    if size.z <= 0:
        raise SendError("The model has no height")
    low, high = bounds([merged])
    tolerance = max(size) * 0.001
    if (low - want_low).length > tolerance or (high - want_high).length > tolerance:
        raise SendError("Merging lost part of the model, nothing was exported")

    bad_edges, pieces, volume = inspect(merged)
    if volume < largest * 0.999:
        raise SendError("Merging lost part of the model, nothing was exported")
    if bad_edges:
        raise SendError(f"The merged solid has {bad_edges} mesh errors, nothing was exported")

    factor = height_mm / size.z
    center = (want_low + want_high) / 2
    merged.transform(
        Matrix.Scale(factor, 4)
        @ Matrix.Translation((-center.x, -center.y, -want_low.z))
    )
    merged.update()
    return merged, pieces, size * factor


def export_stl(context, mesh, filepath, temp_objects):
    obj = bpy.data.objects.new("_send_export", mesh)
    context.scene.collection.objects.link(obj)
    temp_objects.append(obj)
    layer = context.view_layer
    selected = list(context.selected_objects)
    active = layer.objects.active
    try:
        for other in selected:
            other.select_set(False)
        obj.select_set(True)
        layer.objects.active = obj
        bpy.ops.wm.stl_export(
            filepath=filepath, export_selected_objects=True, ascii_format=False,
            global_scale=1.0, use_scene_unit=False, forward_axis="Y", up_axis="Z",
        )
    finally:
        obj.select_set(False)
        for other in selected:
            other.select_set(True)
        layer.objects.active = active


def safe_file_name(name, fallback):
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip()).strip(". ")
    return cleaned or fallback


# Blender interface ---------------------------------------------------------

class SendToCrealityPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    creality_path: bpy.props.StringProperty(
        name="Creality Print program",
        description="Leave empty to find CrealityPrint.exe automatically",
        subtype="FILE_PATH",
    )

    def draw(self, context):
        self.layout.prop(self, "creality_path")


_project_cache = []


def _project_items(_self, _context):
    """Choices for the project list. Blender needs these strings kept alive."""
    _project_cache.clear()
    for pid, (_hwnd, title) in sorted(creality_windows().items()):
        name = title.replace(" - CrealityPrint", "") or title
        _project_cache.append((str(pid), name, f"Creality window {pid}"))
    if not _project_cache:
        _project_cache.append(("NONE", "No project open", "Creality Print is not open"))
    return _project_cache


class SendToCrealitySettings(bpy.types.PropertyGroup):
    height_mm: bpy.props.FloatProperty(
        name="Height", description="Overall printed height in millimeters",
        default=100.0, min=1.0, soft_max=250.0, precision=1, unit="NONE",
    )
    file_name: bpy.props.StringProperty(
        name="File name",
        description="Leave empty to use the name of the Blender file or active object",
    )
    folder: bpy.props.StringProperty(
        name="Folder", description="Where the STL is saved",
        default=os.path.join(os.path.expanduser("~"), "Documents", "Blender Prints"),
        subtype="DIR_PATH",
    )
    only_selected: bpy.props.BoolProperty(
        name="Selected objects only",
        description="Send only the selected objects instead of everything visible",
        default=False,
    )
    send_mode: bpy.props.EnumProperty(
        name="Send to",
        items=(
            ("ADD", "Open project", "Add the model to a Creality project that is already open"),
            ("NEW", "New window", "Open the model in a new Creality window"),
            ("SAVE", "File only", "Save the STL without opening Creality"),
        ),
        default="ADD",
    )
    target_window: bpy.props.EnumProperty(
        name="Project",
        description="Which open Creality project receives the model",
        items=_project_items,
    )
    last_result: bpy.props.StringProperty(options={"SKIP_SAVE"})


class OBJECT_OT_send_to_creality(bpy.types.Operator):
    bl_idname = "object.send_to_creality"
    bl_label = "Send to Creality"
    bl_description = "Merge copies of the parts into one solid, export an STL and open it"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        settings = context.scene.send_to_creality
        temp_meshes = []
        temp_objects = []
        context.window.cursor_set("WAIT")
        try:
            objects = collect_objects(context, settings.only_selected)
            if not objects:
                raise SendError("No visible solid objects to send")

            blend_name = bpy.path.display_name_from_filepath(bpy.data.filepath)
            active = context.view_layer.objects.active
            fallback = blend_name or (active.name if active else "model")
            name = safe_file_name(settings.file_name, safe_file_name(fallback, "model"))
            folder = bpy.path.abspath(settings.folder)
            os.makedirs(folder, exist_ok=True)
            filepath = os.path.join(folder, f"{name}_{settings.height_mm:g}mm.stl")

            merged, pieces, size = build_print_mesh(
                context, objects, settings.height_mm, temp_meshes, temp_objects)
            export_stl(context, merged, filepath, temp_objects)
            if not os.path.isfile(filepath):
                raise SendError("The STL file was not written")

            action = self.deliver(context, settings, filepath)
            if pieces > 1:
                action += f". Warning: {pieces} separate pieces"
            settings.last_result = action
            self.report(
                {"WARNING"} if pieces > 1 else {"INFO"}, settings.last_result)
            return {"FINISHED"}
        except SendError as error:
            settings.last_result = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        finally:
            for obj in temp_objects:
                bpy.data.objects.remove(obj)
            for mesh in temp_meshes:
                if mesh.name in bpy.data.meshes:
                    bpy.data.meshes.remove(mesh)
            for mesh in [m for m in bpy.data.meshes if m.name.startswith("_send_") and m.users == 0]:
                bpy.data.meshes.remove(mesh)
            context.window.cursor_set("DEFAULT")

    @staticmethod
    def deliver(context, settings, filepath):
        context.window_manager.clipboard = filepath
        if settings.send_mode == "SAVE":
            return "Saved, path copied"
        exe = find_creality(context.preferences.addons[__package__].preferences.creality_path)
        if not exe:
            return "Saved, path copied. Creality Print was not found"
        if settings.send_mode == "NEW":
            if open_new_window(exe, filepath):
                return "Opened in a new Creality window"
            return "Saved, path copied. No new Creality window appeared"

        windows = creality_windows()
        if not windows:
            subprocess.Popen([exe, filepath], close_fds=True)
            return "No project was open. Opened in Creality Print"
        target = settings.target_window
        window = windows.get(int(target)) if target.isdigit() else None
        if window is None and len(windows) == 1:
            window = next(iter(windows.values()))
        if window is None:
            return "Saved, path copied. That project was closed, pick another"
        if not add_to_window(window[0], exe, filepath):
            return "Saved, path copied. Creality did not respond"
        name = window[1].replace(" - CrealityPrint", "")
        return f"Added to {name}"


class VIEW3D_PT_send_to_creality(bpy.types.Panel):
    bl_label = "Send to Creality"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Print"

    def draw(self, context):
        settings = context.scene.send_to_creality
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(settings, "height_mm")
        size = live_size(context, settings)
        if size:
            row = layout.row()
            row.alignment = "RIGHT"
            row.label(text=f"{size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm")
        layout.prop(settings, "file_name")
        layout.prop(settings, "folder")
        layout.prop(settings, "only_selected")
        layout.prop(settings, "send_mode")
        if settings.send_mode == "ADD":
            layout.prop(settings, "target_window")
        row = layout.row()
        row.scale_y = 1.5
        row.operator(OBJECT_OT_send_to_creality.bl_idname, icon="EXPORT")
        if settings.last_result:
            box = layout.box()
            box.scale_y = 0.8
            for part in settings.last_result.split(". "):
                box.label(text=part)


CLASSES = (
    SendToCrealityPreferences,
    SendToCrealitySettings,
    OBJECT_OT_send_to_creality,
    VIEW3D_PT_send_to_creality,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.send_to_creality = bpy.props.PointerProperty(type=SendToCrealitySettings)


def unregister():
    del bpy.types.Scene.send_to_creality
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
