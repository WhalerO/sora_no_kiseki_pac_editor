from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retext.playback import (
    MediaPlaybackController,
    PlaybackClosedError,
    PlaybackOperationError,
    PlaybackState,
    PlaybackUnavailableError,
)


class FakeMedia:
    def __init__(self, path: str) -> None:
        self.path = path
        self.duration_ms = 0
        self.release_count = 0

    def get_duration(self) -> int:
        return self.duration_ms

    def release(self) -> None:
        self.release_count += 1


class FakePlayer:
    def __init__(self) -> None:
        self.media: FakeMedia | None = None
        self.media_history: list[FakeMedia | None] = []
        self.play_result = 0
        self.time_ms = 0
        self.length_ms = 0
        self.raw_state = SimpleNamespace(name="Stopped")
        self.volume = 100
        self.muted = False
        self.seekable = True
        self.video_outputs = 0
        self.stop_count = 0
        self.pause_values: list[int] = []
        self.seek_times: list[int] = []
        self.seek_positions: list[float] = []
        self.window_handles: list[int] = []
        self.release_count = 0

    def set_media(self, media: FakeMedia | None) -> None:
        self.media = media
        self.media_history.append(media)

    def play(self) -> int:
        return self.play_result

    def set_pause(self, value: int) -> None:
        self.pause_values.append(value)

    def pause(self) -> None:
        self.pause_values.append(1)

    def stop(self) -> None:
        self.stop_count += 1

    def set_time(self, milliseconds: int) -> int:
        self.seek_times.append(milliseconds)
        return 0

    def set_position(self, position: float) -> int:
        self.seek_positions.append(position)
        return 0

    def audio_set_volume(self, volume: int) -> int:
        self.volume = volume
        return 0

    def audio_set_mute(self, muted: bool) -> None:
        self.muted = bool(muted)

    def is_seekable(self) -> int:
        return int(self.seekable)

    def has_vout(self) -> int:
        return self.video_outputs

    def get_time(self) -> int:
        return self.time_ms

    def get_length(self) -> int:
        return self.length_ms

    def get_state(self) -> object:
        return self.raw_state

    def set_hwnd(self, handle: int) -> None:
        self.window_handles.append(handle)

    def release(self) -> None:
        self.release_count += 1


class FakeInstance:
    def __init__(self, player: FakePlayer | None = None) -> None:
        self.player = player or FakePlayer()
        self.media_created: list[FakeMedia] = []
        self.release_count = 0

    def media_player_new(self) -> FakePlayer:
        return self.player

    def media_new(self, path: str) -> FakeMedia:
        media = FakeMedia(path)
        self.media_created.append(media)
        return media

    def release(self) -> None:
        self.release_count += 1


class FakeVlcModule:
    def __init__(self, instance: FakeInstance | None = None) -> None:
        self.instance = instance or FakeInstance()
        self.instance_args: tuple[str, ...] | None = None

    def Instance(self, *args: str) -> FakeInstance:
        self.instance_args = args
        return self.instance


class MediaPlaybackControllerTests(unittest.TestCase):
    def _source(self, root: Path, name: str = "movie.webm") -> Path:
        source = root / name
        source.write_bytes(b"media")
        return source

    def test_load_embeds_window_and_maps_basic_playback_operations(self) -> None:
        instance = FakeInstance()
        controller = MediaPlaybackController(instance=instance)
        with tempfile.TemporaryDirectory() as temp_name:
            source = self._source(Path(temp_name))
            with patch("retext.playback.sys.platform", "win32"):
                loaded = controller.load(source, window_handle=12345)

        self.assertEqual(loaded, source.resolve())
        self.assertEqual(controller.source_path, source.resolve())
        self.assertEqual(instance.player.window_handles, [12345])
        self.assertIs(instance.player.media, instance.media_created[0])
        controller.play()
        controller.pause()
        controller.stop()
        self.assertEqual(instance.player.pause_values, [1])
        self.assertEqual(instance.player.stop_count, 1)

    def test_time_duration_state_and_volume_are_normalized(self) -> None:
        instance = FakeInstance()
        controller = MediaPlaybackController(instance=instance)
        with tempfile.TemporaryDirectory() as temp_name:
            controller.load(self._source(Path(temp_name), "tone.wav"))
            instance.player.time_ms = 1_250
            instance.player.length_ms = 4_000
            instance.player.raw_state = SimpleNamespace(name="Playing")

            self.assertEqual(controller.position(), 1.25)
            self.assertEqual(controller.duration(), 4.0)
            self.assertIs(controller.state(), PlaybackState.PLAYING)
            self.assertEqual(controller.set_volume(-20), 0)
            self.assertEqual(controller.set_volume(42.6), 43)
            self.assertEqual(controller.set_volume(500), 100)
            self.assertEqual(instance.player.volume, 100)
            controller.set_muted(True)
            self.assertTrue(instance.player.muted)
            self.assertTrue(controller.is_seekable())
            self.assertFalse(controller.has_video_output())
            instance.player.video_outputs = 1
            self.assertTrue(controller.has_video_output())

            instance.player.time_ms = -1
            instance.player.length_ms = -1
            instance.media_created[0].duration_ms = 2_500
            self.assertEqual(controller.position(), 0.0)
            self.assertEqual(controller.duration(), 2.5)

    def test_seek_uses_milliseconds_and_clamps_to_known_duration(self) -> None:
        instance = FakeInstance()
        controller = MediaPlaybackController(instance=instance)
        with tempfile.TemporaryDirectory() as temp_name:
            controller.load(self._source(Path(temp_name)))
            instance.player.length_ms = 10_000
            controller.seek_seconds(-5)
            controller.seek_seconds(3.25)
            controller.seek_seconds(100)

        self.assertEqual(instance.player.seek_times, [0, 3_250, 10_000])

    def test_seek_falls_back_to_normalized_position(self) -> None:
        player = FakePlayer()
        player.set_time = None  # type: ignore[assignment]
        player.length_ms = 8_000
        instance = FakeInstance(player)
        controller = MediaPlaybackController(instance=instance)
        with tempfile.TemporaryDirectory() as temp_name:
            controller.load(self._source(Path(temp_name)))
            controller.seek_seconds(2)

        self.assertEqual(player.seek_positions, [0.25])

    def test_loading_another_file_releases_the_previous_media(self) -> None:
        instance = FakeInstance()
        controller = MediaPlaybackController(instance=instance)
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            controller.load(self._source(root, "first.wav"))
            first_media = instance.media_created[0]
            controller.load(self._source(root, "second.webm"))

        self.assertEqual(first_media.release_count, 1)
        self.assertIn(None, instance.player.media_history)
        self.assertIs(instance.player.media, instance.media_created[1])
        self.assertEqual(instance.player.stop_count, 1)

    def test_close_is_idempotent_and_releases_owned_resources(self) -> None:
        module = FakeVlcModule()
        controller = MediaPlaybackController(vlc_module=module)
        with tempfile.TemporaryDirectory() as temp_name:
            controller.load(self._source(Path(temp_name)))
        media = module.instance.media_created[0]

        controller.close()
        controller.close()

        self.assertTrue(controller.is_closed)
        self.assertIs(controller.state(), PlaybackState.CLOSED)
        self.assertEqual(media.release_count, 1)
        self.assertEqual(module.instance.player.release_count, 1)
        self.assertEqual(module.instance.release_count, 1)
        with self.assertRaises(PlaybackClosedError):
            controller.play()

    def test_backend_failures_have_stable_controller_exceptions(self) -> None:
        class BrokenModule:
            @staticmethod
            def Instance(*args: str) -> object:
                del args
                raise OSError("missing libvlc")

        with self.assertRaises(PlaybackUnavailableError):
            MediaPlaybackController(vlc_module=BrokenModule())

        with patch(
            "retext.playback.importlib.import_module",
            side_effect=SystemExit(1),
        ):
            with self.assertRaises(PlaybackUnavailableError):
                MediaPlaybackController()

        instance = FakeInstance()
        instance.player.play_result = -1
        controller = MediaPlaybackController(instance=instance)
        with self.assertRaises(PlaybackOperationError):
            controller.play()
        with tempfile.TemporaryDirectory() as temp_name:
            controller.load(self._source(Path(temp_name)))
            with self.assertRaises(PlaybackOperationError):
                controller.play()


if __name__ == "__main__":
    unittest.main()
