from __future__ import annotations

import importlib
import math
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Sequence


class PlaybackState(str, Enum):
    """Backend-independent media player state."""

    IDLE = "idle"
    OPENING = "opening"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    ENDED = "ended"
    ERROR = "error"
    UNKNOWN = "unknown"
    CLOSED = "closed"


class PlaybackError(RuntimeError):
    """Base error raised by the media playback controller."""


class PlaybackUnavailableError(PlaybackError):
    """Raised when python-vlc or the native LibVLC runtime is unavailable."""


class PlaybackClosedError(PlaybackError):
    """Raised when an operation is attempted after the controller is closed."""


class PlaybackOperationError(PlaybackError):
    """Raised when LibVLC rejects or fails a playback operation."""


class MediaPlaybackController:
    """Small, synchronous LibVLC adapter suitable for a Tk event loop.

    The controller deliberately does not register LibVLC event callbacks or
    schedule any worker thread.  A Tk owner should poll :meth:`position`,
    :meth:`duration`, and :meth:`state` with ``widget.after(...)`` so every
    interaction with both LibVLC and Tk remains on the caller's GUI thread.

    ``vlc_module`` and ``instance`` are injectable to keep the class testable
    without a native LibVLC installation.  An injected instance remains owned
    by its caller; a player and media created by this controller are always
    released by :meth:`close`.
    """

    DEFAULT_INSTANCE_ARGS = ("--quiet", "--no-video-title-show")

    def __init__(
        self,
        *,
        vlc_module: Any | None = None,
        instance: Any | None = None,
        instance_args: Sequence[str] | None = None,
    ) -> None:
        self._closed = False
        self._source_path: Path | None = None
        self._media: Any | None = None
        self._window_handle: int | None = None
        self._instance: Any | None = None
        self._player: Any | None = None
        self._owns_instance = instance is None

        if instance is None:
            module = vlc_module if vlc_module is not None else _import_vlc()
            self._vlc_module = module
            args = tuple(
                self.DEFAULT_INSTANCE_ARGS
                if instance_args is None
                else instance_args
            )
            try:
                instance = module.Instance(*args)
            except Exception as exc:
                raise PlaybackUnavailableError(
                    "无法初始化 LibVLC。请安装匹配程序位数的 VLC，或将 "
                    "LibVLC 运行时放入程序的 libvlc 目录。"
                ) from exc
            if instance is None:
                raise PlaybackUnavailableError(
                    "LibVLC 初始化失败：python-vlc 未能创建播放实例。"
                )
        else:
            self._vlc_module = vlc_module

        self._instance = instance
        try:
            self._player = instance.media_player_new()
        except Exception as exc:
            if self._owns_instance:
                _release_quietly(instance)
            self._instance = None
            raise PlaybackUnavailableError("LibVLC 无法创建媒体播放器。") from exc
        if self._player is None:
            if self._owns_instance:
                _release_quietly(instance)
            self._instance = None
            raise PlaybackUnavailableError("LibVLC 返回了无效的媒体播放器。")

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def is_closed(self) -> bool:
        return self._closed

    def attach_window(self, handle: int) -> None:
        """Attach video output to an already-realized native window handle.

        On Windows, pass ``int(tk_widget.winfo_id())`` after Tk has realized
        the widget.  Reattaching is supported and is useful after rebuilding a
        preview page.
        """

        self._ensure_open()
        if isinstance(handle, bool):
            raise ValueError("视频窗口句柄必须是正整数。")
        try:
            native_handle = int(handle)
        except (TypeError, ValueError) as exc:
            raise ValueError("视频窗口句柄必须是正整数。") from exc
        if native_handle <= 0:
            raise ValueError("视频窗口句柄必须是正整数。")

        player = self._require_player()
        if sys.platform.startswith("win"):
            setter_name = "set_hwnd"
        elif sys.platform == "darwin":
            setter_name = "set_nsobject"
        else:
            setter_name = "set_xwindow"
        setter = getattr(player, setter_name, None)
        if not callable(setter):
            raise PlaybackOperationError(
                f"当前 LibVLC 后端不支持窗口嵌入（缺少 {setter_name}）。"
            )
        self._call("绑定视频窗口", setter, native_handle)
        self._window_handle = native_handle

    def load(
        self,
        path: str | os.PathLike[str],
        *,
        window_handle: int | None = None,
    ) -> Path:
        """Load a local media file without starting playback."""

        self._ensure_open()
        source = Path(path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"媒体文件不存在：{source}")
        if not source.is_file():
            raise ValueError(f"媒体路径不是文件：{source}")

        if window_handle is not None:
            self.attach_window(window_handle)

        instance = self._require_instance()
        player = self._require_player()
        try:
            new_media = instance.media_new(str(source))
        except Exception as exc:
            raise PlaybackOperationError(f"无法载入媒体文件：{source}") from exc
        if new_media is None:
            raise PlaybackOperationError(f"LibVLC 无法创建媒体对象：{source}")

        self._discard_current_media()
        try:
            player.set_media(new_media)
        except Exception as exc:
            _release_quietly(new_media)
            raise PlaybackOperationError(f"无法将媒体交给播放器：{source}") from exc

        self._media = new_media
        self._source_path = source
        return source

    def play(self) -> None:
        self._ensure_media_loaded()
        result = self._call("开始播放", self._require_player().play)
        if isinstance(result, int) and result < 0:
            raise PlaybackOperationError("LibVLC 无法开始播放当前媒体。")

    def pause(self) -> None:
        self._ensure_media_loaded()
        if self.state() is PlaybackState.PAUSED:
            return
        player = self._require_player()
        set_pause = getattr(player, "set_pause", None)
        if callable(set_pause):
            self._call("暂停播放", set_pause, 1)
        else:
            self._call("暂停播放", player.pause)

    def stop(self) -> None:
        self._ensure_open()
        if self._media is None:
            return
        self._call("停止播放", self._require_player().stop)

    def seek_seconds(self, seconds: float) -> None:
        """Seek to a time in seconds, clamped to the known media duration."""

        self._ensure_media_loaded()
        value = _finite_number(seconds, "播放位置")
        duration = self.duration()
        value = max(0.0, value)
        if duration > 0.0:
            value = min(value, duration)

        player = self._require_player()
        set_time = getattr(player, "set_time", None)
        if callable(set_time):
            result = self._call("跳转播放位置", set_time, round(value * 1000.0))
        else:
            set_position = getattr(player, "set_position", None)
            if not callable(set_position) or duration <= 0.0:
                raise PlaybackOperationError("当前媒体尚不能跳转播放位置。")
            result = self._call(
                "跳转播放位置",
                set_position,
                min(1.0, value / duration),
            )
        if isinstance(result, int) and result < 0:
            raise PlaybackOperationError("LibVLC 拒绝了播放位置跳转。")

    def set_volume(self, volume: float) -> int:
        """Set software volume in the inclusive 0..100 range.

        Out-of-range slider values are clamped.  The applied integer volume is
        returned so callers can keep their UI in sync.
        """

        self._ensure_open()
        value = _finite_number(volume, "音量")
        applied = max(0, min(100, round(value)))
        result = self._call(
            "设置音量",
            self._require_player().audio_set_volume,
            applied,
        )
        if isinstance(result, int) and result < 0:
            raise PlaybackOperationError("LibVLC 无法设置音量。")
        return applied

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute audio without changing the configured volume."""

        self._ensure_open()
        setter = getattr(self._require_player(), "audio_set_mute", None)
        if not callable(setter):
            raise PlaybackOperationError("当前媒体后端不支持静音控制。")
        result = self._call("设置静音", setter, bool(muted))
        if isinstance(result, int) and result < 0:
            raise PlaybackOperationError("LibVLC 无法设置静音状态。")

    def is_seekable(self) -> bool:
        """Return whether the loaded media currently accepts time seeking."""

        self._ensure_media_loaded()
        getter = getattr(self._require_player(), "is_seekable", None)
        if not callable(getter):
            return self.state() in {PlaybackState.PLAYING, PlaybackState.PAUSED}
        return bool(self._call("读取媒体跳转能力", getter))

    def has_video_output(self) -> bool:
        """Return whether LibVLC has created at least one video output."""

        self._ensure_media_loaded()
        getter = getattr(self._require_player(), "has_vout", None)
        if not callable(getter):
            return self.state() in {PlaybackState.PLAYING, PlaybackState.PAUSED}
        count = self._call("读取视频输出状态", getter)
        return isinstance(count, (int, float)) and count > 0

    def position(self) -> float:
        """Return the current playback position in seconds."""

        self._ensure_open()
        if self._media is None:
            return 0.0
        milliseconds = self._call(
            "读取播放位置",
            self._require_player().get_time,
        )
        return _milliseconds_to_seconds(milliseconds)

    def duration(self) -> float:
        """Return the current media duration in seconds, or zero if unknown."""

        self._ensure_open()
        if self._media is None:
            return 0.0
        milliseconds = self._call(
            "读取媒体时长",
            self._require_player().get_length,
        )
        if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
            getter = getattr(self._media, "get_duration", None)
            if callable(getter):
                milliseconds = self._call("读取媒体时长", getter)
        return _milliseconds_to_seconds(milliseconds)

    def state(self) -> PlaybackState:
        """Return a backend-independent player state."""

        if self._closed:
            return PlaybackState.CLOSED
        if self._media is None:
            return PlaybackState.IDLE
        raw_state = self._call(
            "读取播放状态",
            self._require_player().get_state,
        )
        normalized = _normalize_state_name(raw_state)
        if normalized == "nothingspecial":
            return PlaybackState.STOPPED
        return _STATE_NAMES.get(normalized, PlaybackState.UNKNOWN)

    def close(self) -> None:
        """Release media, player, and owned instance; safe to call repeatedly."""

        if self._closed:
            return
        self._closed = True

        player = self._player
        media = self._media
        instance = self._instance
        self._player = None
        self._media = None
        self._instance = None
        self._source_path = None
        self._window_handle = None

        if player is not None:
            stop = getattr(player, "stop", None)
            if callable(stop):
                _call_quietly(stop)
            set_media = getattr(player, "set_media", None)
            if callable(set_media):
                _call_quietly(set_media, None)
        _release_quietly(media)
        _release_quietly(player)
        if self._owns_instance:
            _release_quietly(instance)

    def __enter__(self) -> MediaPlaybackController:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _discard_current_media(self) -> None:
        if self._media is None:
            self._source_path = None
            return
        player = self._require_player()
        _call_quietly(player.stop)
        set_media = getattr(player, "set_media", None)
        if callable(set_media):
            _call_quietly(set_media, None)
        _release_quietly(self._media)
        self._media = None
        self._source_path = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise PlaybackClosedError("媒体播放器已关闭。")

    def _ensure_media_loaded(self) -> None:
        self._ensure_open()
        if self._media is None:
            raise PlaybackOperationError("尚未载入媒体文件。")

    def _require_instance(self) -> Any:
        self._ensure_open()
        if self._instance is None:
            raise PlaybackClosedError("媒体播放器已关闭。")
        return self._instance

    def _require_player(self) -> Any:
        self._ensure_open()
        if self._player is None:
            raise PlaybackClosedError("媒体播放器已关闭。")
        return self._player

    @staticmethod
    def _call(label: str, function: Any, *args: Any) -> Any:
        try:
            return function(*args)
        except PlaybackError:
            raise
        except Exception as exc:
            raise PlaybackOperationError(f"{label}失败：{exc}") from exc


_STATE_NAMES = {
    "opening": PlaybackState.OPENING,
    "buffering": PlaybackState.BUFFERING,
    "playing": PlaybackState.PLAYING,
    "paused": PlaybackState.PAUSED,
    "stopped": PlaybackState.STOPPED,
    "ended": PlaybackState.ENDED,
    "error": PlaybackState.ERROR,
}


def _import_vlc() -> Any:
    try:
        from .vlc_runtime import configure_vlc_runtime
    except ImportError:
        # Keeping this import lazy lets the controller remain importable in
        # source-only/test environments where the optional runtime helper is
        # not present yet.
        configure_vlc_runtime = None

    if configure_vlc_runtime is not None:
        try:
            configure_vlc_runtime()
        except Exception as exc:
            raise PlaybackUnavailableError(
                f"LibVLC 运行时配置失败：{exc}"
            ) from exc

    try:
        return importlib.import_module("vlc")
    except (ImportError, OSError, SystemExit) as exc:
        raise PlaybackUnavailableError(
            "媒体播放组件不可用。请安装 python-vlc，并提供匹配程序位数的 "
            "LibVLC 运行时（可设置 TIS_RETEXT_VLC_DIR）。"
        ) from exc
    except Exception as exc:
        raise PlaybackUnavailableError(
            f"载入 python-vlc 失败：{exc}"
        ) from exc


def _finite_number(value: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有效数字。") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数字。")
    return number


def _milliseconds_to_seconds(value: Any) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, float(value) / 1000.0)


def _normalize_state_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        name = str(value).rsplit(".", 1)[-1]
    return "".join(character for character in name.lower() if character.isalnum())


def _call_quietly(function: Any, *args: Any) -> None:
    try:
        function(*args)
    except Exception:
        pass


def _release_quietly(resource: Any | None) -> None:
    if resource is None:
        return
    release = getattr(resource, "release", None)
    if callable(release):
        _call_quietly(release)


__all__ = [
    "MediaPlaybackController",
    "PlaybackClosedError",
    "PlaybackError",
    "PlaybackOperationError",
    "PlaybackState",
    "PlaybackUnavailableError",
]
