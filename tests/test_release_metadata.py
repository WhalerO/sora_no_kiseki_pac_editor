from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from retext.version import __version__
from scripts import launch_gui, release_check
from scripts.write_version_info import numeric_version, render_version_info


class ReleaseMetadataTests(unittest.TestCase):
    def test_numeric_version_ignores_prerelease_suffix(self) -> None:
        self.assertEqual(numeric_version("1.2.3.dev4"), (1, 2, 3, 0))

    def test_generated_metadata_uses_application_version(self) -> None:
        rendered = render_version_info()
        self.assertIn(f"FileVersion', '{__version__}'", rendered)
        self.assertIn(f"ProductVersion', '{__version__}'", rendered)

    def test_invalid_version_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            numeric_version("release-candidate")

    def test_package_probe_requires_an_output_path(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            ["launch_gui.py", "--package-probe"],
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(launch_gui.main(), 2)
        self.assertIn("OUTPUT.json", stderr.getvalue())

    def test_package_text_pac_probe_roundtrips_all_engines(self) -> None:
        self.assertTrue(launch_gui._probe_text_pac())

    def test_ffmpeg_is_not_a_required_release_dependency(self) -> None:
        self.assertNotIn("imageio-ffmpeg", release_check.REQUIRED_IMPORTS)
        self.assertFalse(
            hasattr(release_check, "_check_ffmpeg_source_evidence")
        )

    def test_vlc_source_evidence_requires_a_reviewed_pinned_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            record_path = Path(temp_name) / "VLC-SOURCE.json"
            self.assertIn(
                "missing",
                release_check._check_vlc_source_evidence(record_path),
            )
            record_path.write_text(
                json.dumps(
                    {
                        "component": "VLC win64 runtime",
                        "version": release_check.VLC_VERSION,
                        "binary_archive": release_check.VLC_BINARY_ARCHIVE,
                        "binary_archive_sha256": release_check.VLC_BINARY_SHA256,
                        "source_archive_url": "https://example.invalid/vlc.tar.xz",
                        "source_archive_sha256": "A" * 64,
                        "corresponding_source_offer": "Bundled beside each release.",
                        "component_notice_inventory": "Reviewed codec/plugin notices.",
                        "reviewed_by": "release reviewer",
                        "reviewed_on": "2026-08-08",
                        "status": "approved",
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(
                release_check._check_vlc_source_evidence(record_path)
            )

    def test_vlc_package_probe_decodes_and_releases_generated_pcm(self) -> None:
        class State:
            Error = "error"
            Playing = "playing"
            Paused = "paused"
            Ended = "ended"

        class Media:
            released = False

            def release(self) -> None:
                self.released = True

        class Player:
            def __init__(self) -> None:
                self.released = False
                self.stopped = False
                self.seek_value = None
                self.times = [100, 1_200]

            def set_media(self, _media) -> None:
                pass

            def audio_set_mute(self, _muted: bool) -> None:
                pass

            def play(self) -> int:
                return 0

            def get_state(self):
                return State.Playing

            def get_length(self) -> int:
                return 2_000

            def get_time(self) -> int:
                if len(self.times) > 1:
                    return self.times.pop(0)
                return self.times[0]

            def is_seekable(self) -> bool:
                return True

            def set_time(self, value: int) -> None:
                self.seek_value = value

            def stop(self) -> None:
                self.stopped = True

            def release(self) -> None:
                self.released = True

        class Instance:
            def __init__(self) -> None:
                self.player = Player()
                self.media = Media()

            def media_player_new(self):
                return self.player

            def media_new_path(self, _path: str):
                return self.media

        vlc_module = type("Vlc", (), {"State": State})
        instance = Instance()
        with tempfile.TemporaryDirectory() as temp_name:
            self.assertTrue(
                launch_gui._probe_vlc_pcm(
                    vlc_module,
                    instance,
                    Path(temp_name),
                )
            )
        self.assertEqual(instance.player.seek_value, 1_200)
        self.assertTrue(instance.player.stopped)
        self.assertTrue(instance.player.released)
        self.assertTrue(instance.media.released)


if __name__ == "__main__":
    unittest.main()
