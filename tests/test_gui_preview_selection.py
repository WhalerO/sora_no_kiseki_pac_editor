from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retext.archive.collection import PacNodeRef
from retext.playback import PlaybackState
from tk_gui import RetextTkApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _SearchTree:
    def __init__(self, parents: dict[str, str]) -> None:
        self.parents = parents
        self.opened: list[str] = []
        self.selected: list[str] = []
        self.focused = ""
        self.visible = ""

    def parent(self, item: str) -> str:
        return self.parents.get(item, "")

    def item(self, item: str, **kwargs) -> None:
        if kwargs.get("open"):
            self.opened.append(item)

    def selection_set(self, item: str) -> None:
        self.selected.append(item)

    def focus(self, item: str) -> None:
        self.focused = item

    def see(self, item: str) -> None:
        self.visible = item


class _TimerRoot:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []
        self.cancelled: list[object] = []

    def after(self, delay: int, callback) -> str:
        self.scheduled.append((delay, callback))
        return f"timer-{len(self.scheduled)}"

    def after_idle(self, callback) -> str:
        self.scheduled.append((-1, callback))
        return f"timer-{len(self.scheduled)}"

    def after_cancel(self, timer_id: object) -> None:
        self.cancelled.append(timer_id)


class _ConfiguredWidget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)


class _PrimePlayback:
    def __init__(
        self,
        *,
        pause_immediately: bool = True,
        seek_updates_position: bool = True,
    ) -> None:
        self.state_value = PlaybackState.PLAYING
        self.position_value = 0.0
        self.volume_values: list[float] = []
        self.mute_values: list[bool] = []
        self.seek_values: list[float] = []
        self.play_count = 0
        self.pause_count = 0
        self.pause_immediately = pause_immediately
        self.seek_updates_position = seek_updates_position

    def set_volume(self, value: float) -> None:
        self.volume_values.append(value)

    def set_muted(self, value: bool) -> None:
        self.mute_values.append(value)

    def play(self) -> None:
        self.play_count += 1
        self.state_value = PlaybackState.PLAYING

    def pause(self) -> None:
        self.pause_count += 1
        if self.pause_immediately:
            self.state_value = PlaybackState.PAUSED

    def state(self) -> PlaybackState:
        return self.state_value

    def is_seekable(self) -> bool:
        return True

    def seek_seconds(self, value: float) -> None:
        self.seek_values.append(value)
        if self.seek_updates_position:
            self.position_value = value

    def position(self) -> float:
        return self.position_value

    def has_video_output(self) -> bool:
        return True


class PreviewRenderTriggerTests(unittest.TestCase):
    def test_pac_integrity_report_groups_problems_before_clean_items(self) -> None:
        def item(path: str, status: str) -> SimpleNamespace:
            return SimpleNamespace(
                logical_path=path,
                status=status,
                text_count=1,
                invalid_pointer_count=0,
                corrected_pointer_count=0,
                silent_pointer_count=0,
                skipped_uncertain_pointer_count=0,
                aggressive_pointer_count=0,
                message="",
            )

        report = SimpleNamespace(
            source_path=Path("target.pac"),
            reference_path=Path("reference.pac"),
            complete=False,
            requested_dat_count=3,
            parsed_dat_count=2,
            text_count=3,
            invalid_pointer_count=0,
            corrected_pointer_count=0,
            silent_pointer_count=0,
            skipped_uncertain_pointer_count=0,
            aggressive_pointer_count=0,
            strategy="conservative",
            items=(
                item("clean.dat", "clean"),
                item("repairable.dat", "repairable"),
                item("broken.dat", "incompatible"),
            ),
        )

        lines = self._bare_app()._format_pac_integrity_report(report)
        rendered = "\n".join(lines)

        self.assertLess(rendered.index("[INCOMPATIBLE]"), rendered.index("[CLEAN]"))
        self.assertLess(rendered.index("[REPAIRABLE]"), rendered.index("[CLEAN]"))
        self.assertLess(rendered.index("=== 错误 / 需处理 ==="), rendered.index("=== CLEAN ==="))

    def test_main_sidebar_sash_recovers_from_collapsed_layout(self) -> None:
        target = RetextTkApp._main_sash_target(2000, 18)

        self.assertEqual(target, 330)
        self.assertIsNone(RetextTkApp._main_sash_target(2000, 420))
        self.assertEqual(
            RetextTkApp._main_sash_target(2000, 420, force=True),
            330,
        )

    @staticmethod
    def _bare_app() -> RetextTkApp:
        app = object.__new__(RetextTkApp)
        app._busy = False
        app._suspend_tree_preview = False
        app.status_var = _Value()
        return app

    def test_unpacked_non_file_selections_do_not_request_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "child.png").write_bytes(b"image")
            second = root / "second.wav"
            second.write_bytes(b"audio")
            app = self._bare_app()
            opened: list[Path] = []
            app._preview_unpacked_file = opened.append

            app.selected_unpacked_paths = lambda: [str(root)]
            app.on_unpacked_tree_select()
            self.assertEqual(opened, [])
            self.assertIn("保持不变", app.status_var.value)

            app.selected_unpacked_paths = lambda: [str(root), str(second)]
            app.on_unpacked_tree_select()
            self.assertEqual(opened, [])

    def test_empty_pac_selection_does_not_request_render(self) -> None:
        app = self._bare_app()
        render_calls: list[object] = []
        app._set_preview_selection = render_calls.append
        app._render_preview_message = render_calls.append
        app.selected_pac_refs = lambda: []

        app.on_pac_tree_select()

        self.assertEqual(render_calls, [])

    def test_pac_non_file_selections_do_not_request_render(self) -> None:
        app = self._bare_app()
        folder = SimpleNamespace(kind="folder", workspace_id="pac", path="movie")
        other = SimpleNamespace(kind="folder", workspace_id="pac", path="sound")
        entry = SimpleNamespace(editable=False)
        render_calls: list[object] = []
        app._set_preview_selection = render_calls.append
        app._render_preview_message = render_calls.append
        app.workbench = SimpleNamespace(
            expand_refs=lambda refs, editable_only=False: [(None, entry)]
        )
        app.describe_pac_ref = lambda ref: ref.path

        app.selected_pac_refs = lambda: [folder]
        app.on_pac_tree_select()
        self.assertEqual(render_calls, [])
        self.assertIn("保持不变", app.status_var.value)

        app.selected_pac_refs = lambda: [folder, other]
        app.on_pac_tree_select()
        self.assertEqual(render_calls, [])

    def test_pac_filename_search_cycles_and_ignores_folder_path(self) -> None:
        app = self._bare_app()
        app.pac_search_var = _Value()
        app.pac_search_status_var = _Value()
        app._pac_search_last_query = ""
        app._pac_search_last_match_keys = ()
        app._pac_search_cursor = -1
        app.pac_tree = _SearchTree(
            {
                "first": "folder-a",
                "folder-a": "pac-a",
                "folder-only": "folder-a",
                "second": "folder-b",
                "folder-b": "pac-b",
                "pac-a": "",
                "pac-b": "",
            }
        )
        app.pac_tree_refs = {
            "pac-a": PacNodeRef("a", "pac"),
            "folder-a": PacNodeRef("a", "folder", "font_0.dds"),
            "first": PacNodeRef("a", "file", "asset/font/font_0.dds"),
            "folder-only": PacNodeRef(
                "a",
                "file",
                "asset/font_0.dds/readme.txt",
            ),
            "pac-b": PacNodeRef("b", "pac"),
            "folder-b": PacNodeRef("b", "folder", "image"),
            "second": PacNodeRef("b", "file", "asset/image/FONT_0.DDS"),
        }

        app.pac_search_var.value = "font_0.dds"
        app.find_next_pac_file()
        app.find_next_pac_file()
        app.find_next_pac_file()

        self.assertEqual(app.pac_tree.selected, ["first", "second", "first"])
        self.assertEqual(app.pac_tree.focused, "first")
        self.assertEqual(app.pac_tree.visible, "first")
        self.assertIn("已循环至首个匹配", app.pac_search_status_var.value)
        self.assertNotIn("folder-only", app.pac_tree.selected)

    def test_model_orbit_zoom_and_reset_only_apply_to_geometry_preview(self) -> None:
        app = self._bare_app()
        app.preview_media = SimpleNamespace(kind="model", model_geometry=object())
        app.preview_model_yaw = 0.45
        app.preview_model_pitch = -0.12
        app.preview_model_zoom = 0.95
        app.preview_model_pan_x = 0.0
        app.preview_model_pan_y = 0.0
        app.preview_model_drag_anchor = None
        app.preview_model_pan_anchor = None
        app._preview_model_full_render_after_id = None
        app._preview_model_low_quality = False
        redraws: list[bool] = []
        app._schedule_preview_model_redraw = (
            lambda **_kwargs: redraws.append(True)
        )

        self.assertEqual(
            app._begin_preview_model_orbit(SimpleNamespace(x=10, y=20)),
            "break",
        )
        app._drag_preview_model_orbit(SimpleNamespace(x=30, y=5))
        app._zoom_preview_model(SimpleNamespace(delta=120, num=0))
        app._begin_preview_model_pan(SimpleNamespace(x=8, y=9))
        app._drag_preview_model_pan(SimpleNamespace(x=20, y=25))

        self.assertAlmostEqual(app.preview_model_yaw, 0.65)
        self.assertAlmostEqual(app.preview_model_pitch, -0.27)
        self.assertGreater(app.preview_model_zoom, 0.95)
        self.assertEqual(app.preview_model_pan_x, 12.0)
        self.assertEqual(app.preview_model_pan_y, 16.0)
        self.assertEqual(len(redraws), 3)

        app._reset_preview_model_view()
        self.assertEqual(app.preview_model_drag_anchor, None)
        self.assertAlmostEqual(app.preview_model_yaw, 0.45)
        self.assertAlmostEqual(app.preview_model_pitch, -0.12)
        self.assertAlmostEqual(app.preview_model_zoom, 0.95)
        self.assertEqual(app.preview_model_pan_x, 0.0)
        self.assertEqual(app.preview_model_pan_y, 0.0)

        app.preview_media = SimpleNamespace(kind="image", model_geometry=None)
        self.assertIsNone(
            app._begin_preview_model_orbit(SimpleNamespace(x=0, y=0))
        )

    def test_pac_dependency_change_reloads_current_model_preview(self) -> None:
        app = self._bare_app()
        model_ref = SimpleNamespace(workspace_id="model-pac")
        selection = SimpleNamespace(
            origin="pac",
            logical_path="asset/common/model/chr0001_c62.mdl",
            pac_ref=model_ref,
        )
        app.preview_selection = selection
        app.workbench = SimpleNamespace(
            get=lambda workspace_id: (
                object()
                if workspace_id == "model-pac"
                else (_ for _ in ()).throw(KeyError(workspace_id))
            )
        )
        reloaded: list[object] = []
        app._start_media_preview = reloaded.append

        app._refresh_pac_dependent_preview()

        self.assertEqual(reloaded, [selection])

        selection.logical_path = "asset/dx11/image/chr0001.dds"
        app._refresh_pac_dependent_preview()
        self.assertEqual(reloaded, [selection])

    def test_model_animation_time_is_clamped_and_advances_by_wall_time(
        self,
    ) -> None:
        app = self._bare_app()
        app.preview_media = SimpleNamespace(
            kind="model",
            model_geometry=object(),
            model_animation_player=SimpleNamespace(
                compatible=True,
                uses_numpy=True,
                clip=SimpleNamespace(duration=1.25),
            ),
        )
        app.preview_model_animation_time = 0.0
        app.preview_model_animation_progress_var = _Value()
        app.preview_model_animation_time_var = _Value()
        app._preview_model_animation_playing = True
        app._preview_model_animation_after_id = None
        app._preview_model_animation_origin = 0.2
        app._preview_model_animation_started_at = 10.0
        app._preview_model_worker_lock = threading.Lock()
        app._preview_model_worker_busy = False
        app._preview_model_pending_request = None
        app._preview_model_render_after_id = None
        app.root = _TimerRoot()
        redraws: list[float] = []
        app._perform_preview_model_redraw = lambda: redraws.append(
            app.preview_model_animation_time
        )

        app._set_preview_model_animation_time(9.0, redraw=False)
        self.assertEqual(app.preview_model_animation_time, 1.25)
        self.assertEqual(
            app.preview_model_animation_time_var.value,
            "0:01.250 / 0:01.250",
        )

        app._preview_model_animation_origin = 0.2
        with patch("tk_gui.time.perf_counter", return_value=10.4):
            app._advance_preview_model_animation()

        self.assertAlmostEqual(app.preview_model_animation_time, 0.6)
        self.assertEqual(len(redraws), 1)
        self.assertEqual(app.root.scheduled[0][0], 33)

    def test_camera_changes_reuse_the_same_sampled_animation_pose(
        self,
    ) -> None:
        app = self._bare_app()
        app._preview_model_pose_cache_key = None
        app._preview_model_pose_cache_geometry = None
        sample_calls: list[float] = []
        posed = object()
        player = SimpleNamespace(
            compatible=True,
            sample=lambda seconds: (
                sample_calls.append(seconds),
                posed,
            )[1],
        )
        request = SimpleNamespace(
            preview=SimpleNamespace(
                model_geometry=object(),
                model_animation_player=player,
            ),
            animation_seconds=0.5,
        )

        first = app._prepare_preview_model_geometry(request)
        second = app._prepare_preview_model_geometry(request)
        request.animation_seconds = 0.75
        third = app._prepare_preview_model_geometry(request)

        self.assertIs(first, posed)
        self.assertIs(second, posed)
        self.assertIs(third, posed)
        self.assertEqual(sample_calls, [0.5, 0.75])

    def test_pausing_busy_animation_flushes_the_latest_slider_frame(
        self,
    ) -> None:
        app = self._bare_app()
        app.preview_media = SimpleNamespace(
            kind="model",
            model_geometry=object(),
            model_animation_player=SimpleNamespace(
                compatible=True,
                uses_numpy=True,
                clip=SimpleNamespace(duration=1.0),
            ),
        )
        app._preview_model_animation_playing = True
        app._preview_model_animation_frame_dirty = False
        app._preview_model_animation_after_id = "animation-timer"
        app._preview_model_worker_lock = threading.Lock()
        app._preview_model_worker_busy = True
        app._preview_model_pending_request = None
        app._preview_model_render_after_id = None
        app.root = _TimerRoot()
        redraws: list[bool] = []
        app._perform_preview_model_redraw = lambda: redraws.append(True)

        app._pause_preview_model_animation()

        self.assertTrue(app._preview_model_animation_frame_dirty)
        self.assertEqual(redraws, [])
        self.assertEqual(app.root.cancelled, ["animation-timer"])

        app._preview_model_worker_busy = False
        app._flush_preview_model_animation_frame()

        self.assertFalse(app._preview_model_animation_frame_dirty)
        self.assertEqual(redraws, [True])

    def test_model_worker_shutdown_keeps_a_still_live_thread_reference(self) -> None:
        app = self._bare_app()

        class StuckWorker:
            def __init__(self) -> None:
                self.join_timeouts: list[float] = []

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float) -> None:
                self.join_timeouts.append(timeout)

        worker = StuckWorker()
        app._preview_model_generation = 0
        app._preview_model_worker_stop = threading.Event()
        app._preview_model_worker_lock = threading.Lock()
        app._preview_model_pending_request = object()
        app._preview_model_request_event = threading.Event()
        app._preview_model_worker_thread = worker

        self.assertFalse(app._stop_preview_model_worker())
        self.assertIs(app._preview_model_worker_thread, worker)
        self.assertEqual(worker.join_timeouts, [2.0])
        self.assertTrue(app._preview_model_worker_stop.is_set())

    def test_model_worker_shutdown_clears_a_finished_thread_reference(self) -> None:
        app = self._bare_app()

        class FinishingWorker:
            alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float) -> None:
                self.alive = False

        app._preview_model_generation = 0
        app._preview_model_worker_stop = threading.Event()
        app._preview_model_worker_lock = threading.Lock()
        app._preview_model_pending_request = object()
        app._preview_model_request_event = threading.Event()
        app._preview_model_worker_thread = FinishingWorker()

        self.assertTrue(app._stop_preview_model_worker())
        self.assertIsNone(app._preview_model_worker_thread)

    def test_video_prime_seeks_muted_then_pauses_on_the_requested_frame(self) -> None:
        app = self._bare_app()
        playback = _PrimePlayback(pause_immediately=False)
        app.root = _TimerRoot()
        app.preview_media = SimpleNamespace(
            kind="video",
            duration_seconds=10.0,
            source_path=Path("D:/preview/movie.webm"),
        )
        app.preview_playback = playback
        app.preview_media_volume_var = _Value()
        app.preview_media_volume_var.set(80.0)
        app.preview_media_progress_var = _Value()
        app.preview_media_time_var = _Value()
        app.preview_media_warning_var = _Value()
        app.preview_media_play_button = _ConfiguredWidget()
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_after_id = None
        app._preview_video_prime_generation = 0
        app._preview_video_prime_target = None
        app._preview_video_prime_phase = "idle"
        app._preview_video_prime_deadline = 0.0
        app._preview_video_prime_seeked_at = 0.0
        app._preview_video_prime_origin_position = None
        app._preview_video_prime_ready_polls = 0
        app._preview_video_user_started = False
        app._ensure_preview_playback = lambda: playback

        with patch("tk_gui.time.monotonic", return_value=10.0):
            app._prime_preview_video_frame(3.25)
        generation = app._preview_video_prime_generation
        with patch("tk_gui.time.monotonic", return_value=10.05):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=10.25):
            app._poll_preview_video_prime(generation)
        self.assertEqual(playback.mute_values, [True])
        self.assertEqual(app._preview_video_prime_phase, "seeking")
        with patch("tk_gui.time.monotonic", return_value=10.30):
            app._poll_preview_video_prime(generation)
        self.assertEqual(playback.mute_values, [True])
        self.assertEqual(app._preview_video_prime_phase, "pausing")
        with patch("tk_gui.time.monotonic", return_value=10.35):
            app._poll_preview_video_prime(generation)
        self.assertEqual(playback.mute_values, [True])
        playback.state_value = PlaybackState.PAUSED
        with patch("tk_gui.time.monotonic", return_value=10.40):
            app._poll_preview_video_prime(generation)

        self.assertEqual(playback.seek_values, [3.25])
        self.assertEqual(playback.mute_values, [True, False])
        self.assertEqual(playback.volume_values, [0, 80.0])
        self.assertEqual(playback.pause_count, 1)
        self.assertEqual(app.preview_media_progress_var.get(), 3.25)
        self.assertEqual(app.preview_media_play_button.options["text"], "播放")
        self.assertEqual(app._preview_video_prime_phase, "idle")

    def test_new_video_prime_cancels_the_stale_target(self) -> None:
        app = self._bare_app()
        playback = _PrimePlayback()
        app.root = _TimerRoot()
        app.preview_media = SimpleNamespace(
            kind="video",
            duration_seconds=10.0,
            source_path=Path("D:/preview/movie.webm"),
        )
        app.preview_playback = playback
        app.preview_media_volume_var = _Value()
        app.preview_media_volume_var.set(80.0)
        app.preview_media_progress_var = _Value()
        app.preview_media_time_var = _Value()
        app.preview_media_warning_var = _Value()
        app.preview_media_play_button = _ConfiguredWidget()
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_after_id = None
        app._preview_video_prime_generation = 0
        app._preview_video_prime_target = None
        app._preview_video_prime_phase = "idle"
        app._preview_video_prime_deadline = 0.0
        app._preview_video_prime_seeked_at = 0.0
        app._preview_video_prime_origin_position = None
        app._preview_video_prime_ready_polls = 0
        app._preview_video_user_started = False
        app._ensure_preview_playback = lambda: playback

        with patch("tk_gui.time.monotonic", return_value=20.0):
            app._prime_preview_video_frame(1.0)
        stale_generation = app._preview_video_prime_generation
        with patch("tk_gui.time.monotonic", return_value=20.1):
            app._prime_preview_video_frame(7.0)
        current_generation = app._preview_video_prime_generation
        with patch("tk_gui.time.monotonic", return_value=20.15):
            app._poll_preview_video_prime(stale_generation)
            app._poll_preview_video_prime(current_generation)

        self.assertEqual(playback.seek_values, [7.0])
        self.assertIn("timer-1", app.root.cancelled)

    def test_video_prime_does_not_accept_an_old_vout_at_the_wrong_time(self) -> None:
        app = self._bare_app()
        playback = _PrimePlayback(seek_updates_position=False)
        app.root = _TimerRoot()
        app.preview_media = SimpleNamespace(
            kind="video",
            duration_seconds=10.0,
            source_path=Path("D:/preview/movie.webm"),
        )
        app.preview_playback = playback
        app.preview_media_volume_var = _Value()
        app.preview_media_volume_var.set(80.0)
        app.preview_media_progress_var = _Value()
        app.preview_media_progress_var.set(0.0)
        app.preview_media_time_var = _Value()
        app.preview_media_warning_var = _Value()
        app.preview_media_play_button = _ConfiguredWidget()
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_after_id = None
        app._preview_video_prime_generation = 0
        app._preview_video_prime_target = None
        app._preview_video_prime_phase = "idle"
        app._preview_video_prime_deadline = 0.0
        app._preview_video_prime_seeked_at = 0.0
        app._preview_video_prime_origin_position = None
        app._preview_video_prime_ready_polls = 0
        app._preview_video_user_started = False
        app._ensure_preview_playback = lambda: playback

        with patch("tk_gui.time.monotonic", return_value=30.0):
            app._prime_preview_video_frame(7.0)
        generation = app._preview_video_prime_generation
        with patch("tk_gui.time.monotonic", return_value=30.05):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=31.0):
            app._poll_preview_video_prime(generation)

        self.assertEqual(playback.pause_count, 0)
        self.assertEqual(app._preview_video_prime_phase, "seeking")
        self.assertEqual(app.preview_media_progress_var.get(), 0.0)

        playback.position_value = 7.0
        with patch("tk_gui.time.monotonic", return_value=31.05):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=31.10):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=31.15):
            app._poll_preview_video_prime(generation)
        self.assertEqual(playback.pause_count, 1)
        self.assertEqual(app.preview_media_progress_var.get(), 7.0)

    def test_video_prime_rejects_a_nearby_unchanged_old_position(self) -> None:
        app = self._bare_app()
        playback = _PrimePlayback(seek_updates_position=False)
        playback.position_value = 6.4
        app.root = _TimerRoot()
        app.preview_media = SimpleNamespace(
            kind="video",
            duration_seconds=10.0,
            source_path=Path("D:/preview/movie.webm"),
        )
        app.preview_playback = playback
        app.preview_media_volume_var = _Value()
        app.preview_media_volume_var.set(80.0)
        app.preview_media_progress_var = _Value()
        app.preview_media_progress_var.set(6.4)
        app.preview_media_time_var = _Value()
        app.preview_media_warning_var = _Value()
        app.preview_media_play_button = _ConfiguredWidget()
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_after_id = None
        app._preview_video_prime_generation = 0
        app._preview_video_prime_target = None
        app._preview_video_prime_phase = "idle"
        app._preview_video_prime_deadline = 0.0
        app._preview_video_prime_seeked_at = 0.0
        app._preview_video_prime_origin_position = None
        app._preview_video_prime_ready_polls = 0
        app._preview_video_user_started = False
        app._ensure_preview_playback = lambda: playback

        with patch("tk_gui.time.monotonic", return_value=60.0):
            app._prime_preview_video_frame(7.0)
        generation = app._preview_video_prime_generation
        with patch("tk_gui.time.monotonic", return_value=60.05):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=60.30):
            app._poll_preview_video_prime(generation)
        with patch("tk_gui.time.monotonic", return_value=60.40):
            app._poll_preview_video_prime(generation)

        self.assertEqual(playback.pause_count, 0)
        self.assertEqual(app._preview_video_prime_phase, "seeking")
        self.assertEqual(app._preview_video_prime_ready_polls, 0)

    def test_begin_scrub_keeps_an_active_prime_muted_until_reposition(self) -> None:
        app = self._bare_app()
        playback = _PrimePlayback()
        app.root = _TimerRoot()
        app.preview_playback = playback
        app.preview_media_volume_var = _Value()
        app.preview_media_volume_var.set(80.0)
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_after_id = None
        app._preview_video_prime_generation = 1
        app._preview_video_prime_target = 2.0
        app._preview_video_prime_phase = "seeking"
        app._preview_video_prime_deadline = 50.0
        app._preview_video_prime_seeked_at = 40.0
        app._preview_video_prime_origin_position = 0.0
        app._preview_video_prime_ready_polls = 0

        app._begin_preview_scrub()

        self.assertEqual(playback.pause_count, 1)
        self.assertEqual(playback.mute_values, [])
        self.assertEqual(playback.volume_values, [])

    def test_hidden_auto_prime_callback_cannot_recreate_playback(self) -> None:
        app = self._bare_app()
        root = _TimerRoot()
        app.root = root
        preview = SimpleNamespace(kind="video")
        app.preview_media = preview
        app.preview_playback = None
        app.preview_media_progress_var = _Value()
        app.preview_media_progress_var.set(0.0)
        app._preview_video_auto_prime_after_id = None
        app._preview_video_prime_phase = "idle"
        app._preview_video_user_started = False
        visible = [True]
        app._preview_tabs_are_visible = lambda: visible[0]
        primed: list[float] = []
        app._prime_preview_video_frame = primed.append

        app._schedule_preview_video_auto_prime(preview)
        callback = root.scheduled[-1][1]
        visible[0] = False
        callback()

        self.assertEqual(primed, [])
        self.assertIsNone(app._preview_video_auto_prime_after_id)


if __name__ == "__main__":
    unittest.main()
