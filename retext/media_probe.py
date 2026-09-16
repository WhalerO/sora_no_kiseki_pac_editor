from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class MediaProbeError(ValueError):
    """Raised when a supported media container is malformed or incomplete."""


@dataclass(slots=True, frozen=True)
class WebmProbe:
    duration_seconds: float = 0.0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    audio_codec: str = ""
    audio_channels: int = 0
    audio_sample_rate: float = 0.0


_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEK_HEAD = 0x114D9B74
_INFO = 0x1549A966
_TRACKS = 0x1654AE6B
_CLUSTER = 0x1F43B675

_TIMECODE_SCALE = 0x2AD7B1
_DURATION = 0x4489
_TRACK_ENTRY = 0xAE
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_DEFAULT_DURATION = 0x23E383
_VIDEO = 0xE0
_PIXEL_WIDTH = 0xB0
_PIXEL_HEIGHT = 0xBA
_FRAME_RATE = 0x2383E3
_AUDIO = 0xE1
_SAMPLING_FREQUENCY = 0xB5
_CHANNELS = 0x9F

_VIDEO_TRACK = 1
_AUDIO_TRACK = 2
_MAX_HEADER_SCAN = 64 * 1024 * 1024


def probe_webm(path: str | Path) -> WebmProbe:
    """Read WebM/Matroska header metadata without starting a decoder.

    Only the EBML header, Segment Info and Tracks masters are inspected. The
    first Cluster is never read, so probing remains bounded for large movies.
    """

    source = Path(path).resolve()
    file_size = source.stat().st_size
    if file_size <= 0:
        raise MediaProbeError("WebM 文件为空。")

    with source.open("rb") as stream:
        first = _read_element(stream, file_size)
        if first is None or first.element_id != _EBML:
            raise MediaProbeError("文件缺少有效的 EBML 头。")
        stream.seek(first.end)

        segment = None
        while stream.tell() < file_size:
            candidate = _read_element(stream, file_size)
            if candidate is None:
                break
            if candidate.element_id == _SEGMENT:
                segment = candidate
                break
            stream.seek(candidate.end)
        if segment is None:
            raise MediaProbeError("文件缺少 WebM Segment。")

        segment_end = min(segment.end, file_size)
        scan_end = min(segment_end, segment.payload_offset + _MAX_HEADER_SCAN)
        timecode_scale = 1_000_000
        duration_ticks = 0.0
        tracks: list[_TrackProbe] = []

        stream.seek(segment.payload_offset)
        while stream.tell() < scan_end:
            element = _read_element(stream, segment_end)
            if element is None:
                break
            if element.element_id == _INFO:
                _require_header_element_within_limit(element, scan_end)
                timecode_scale, duration_ticks = _read_info(stream, element)
            elif element.element_id == _TRACKS:
                _require_header_element_within_limit(element, scan_end)
                tracks = _read_tracks(stream, element)
            elif element.element_id == _CLUSTER:
                break
            stream.seek(element.end)
            if duration_ticks > 0.0 and tracks:
                break

    video = next((track for track in tracks if track.track_type == _VIDEO_TRACK), None)
    audio = next((track for track in tracks if track.track_type == _AUDIO_TRACK), None)
    duration = duration_ticks * timecode_scale / 1_000_000_000.0
    return WebmProbe(
        duration_seconds=duration if math.isfinite(duration) and duration > 0 else 0.0,
        video_codec=_friendly_codec(video.codec_id) if video else "",
        width=video.width if video else 0,
        height=video.height if video else 0,
        frame_rate=video.frame_rate if video else 0.0,
        audio_codec=_friendly_codec(audio.codec_id) if audio else "",
        audio_channels=audio.channels if audio else 0,
        audio_sample_rate=audio.sample_rate if audio else 0.0,
    )


def _require_header_element_within_limit(
    element: _Element,
    scan_end: int,
) -> None:
    """Reject an Info/Tracks master that escapes the bounded header window.

    Merely checking the top-level element's starting offset is insufficient:
    a damaged file can declare a very large master and make the nested parser
    walk far beyond ``_MAX_HEADER_SCAN``. Valid WebM Info and Tracks sections
    are small and wholly precede the first media Cluster.
    """

    if element.end > scan_end:
        raise MediaProbeError(
            "WebM 头信息区段超过 64 MiB 安全解析上限。"
        )


@dataclass(slots=True, frozen=True)
class _Element:
    element_id: int
    payload_offset: int
    end: int


@dataclass(slots=True)
class _TrackProbe:
    track_type: int = 0
    codec_id: str = ""
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    channels: int = 0
    sample_rate: float = 0.0


def _read_element(stream: BinaryIO, container_end: int) -> _Element | None:
    if stream.tell() >= container_end:
        return None
    element_id, _ = _read_vint(stream, keep_marker=True)
    size, unknown = _read_vint(stream, keep_marker=False)
    payload_offset = stream.tell()
    end = container_end if unknown else payload_offset + size
    if end < payload_offset or end > container_end:
        raise MediaProbeError("EBML 元素长度超出容器边界。")
    return _Element(element_id=element_id, payload_offset=payload_offset, end=end)


def _read_vint(stream: BinaryIO, *, keep_marker: bool) -> tuple[int, bool]:
    raw = stream.read(1)
    if not raw:
        raise MediaProbeError("EBML 可变长整数被截断。")
    first = raw[0]
    mask = 0x80
    length = 1
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8 or (keep_marker and length > 4):
        raise MediaProbeError("EBML 可变长整数无效。")
    tail = stream.read(length - 1)
    if len(tail) != length - 1:
        raise MediaProbeError("EBML 可变长整数被截断。")
    value = first if keep_marker else first & (mask - 1)
    for byte in tail:
        value = (value << 8) | byte
    unknown = not keep_marker and value == (1 << (7 * length)) - 1
    return value, unknown


def _read_info(stream: BinaryIO, master: _Element) -> tuple[int, float]:
    scale = 1_000_000
    duration = 0.0
    stream.seek(master.payload_offset)
    while stream.tell() < master.end:
        child = _read_element(stream, master.end)
        if child is None:
            break
        if child.element_id == _TIMECODE_SCALE:
            scale = _read_uint(stream, child)
        elif child.element_id == _DURATION:
            duration = _read_float(stream, child)
        stream.seek(child.end)
    return max(scale, 1), duration


def _read_tracks(stream: BinaryIO, master: _Element) -> list[_TrackProbe]:
    tracks: list[_TrackProbe] = []
    stream.seek(master.payload_offset)
    while stream.tell() < master.end:
        child = _read_element(stream, master.end)
        if child is None:
            break
        if child.element_id == _TRACK_ENTRY:
            tracks.append(_read_track(stream, child))
        stream.seek(child.end)
    return tracks


def _read_track(stream: BinaryIO, master: _Element) -> _TrackProbe:
    track = _TrackProbe()
    default_duration = 0
    stream.seek(master.payload_offset)
    while stream.tell() < master.end:
        child = _read_element(stream, master.end)
        if child is None:
            break
        if child.element_id == _TRACK_TYPE:
            track.track_type = _read_uint(stream, child)
        elif child.element_id == _CODEC_ID:
            track.codec_id = _read_text(stream, child)
        elif child.element_id == _DEFAULT_DURATION:
            default_duration = _read_uint(stream, child)
        elif child.element_id == _VIDEO:
            _read_video(stream, child, track)
        elif child.element_id == _AUDIO:
            _read_audio(stream, child, track)
        stream.seek(child.end)
    if track.frame_rate <= 0.0 and default_duration > 0:
        track.frame_rate = 1_000_000_000.0 / default_duration
    return track


def _read_video(stream: BinaryIO, master: _Element, track: _TrackProbe) -> None:
    stream.seek(master.payload_offset)
    while stream.tell() < master.end:
        child = _read_element(stream, master.end)
        if child is None:
            break
        if child.element_id == _PIXEL_WIDTH:
            track.width = _read_uint(stream, child)
        elif child.element_id == _PIXEL_HEIGHT:
            track.height = _read_uint(stream, child)
        elif child.element_id == _FRAME_RATE:
            track.frame_rate = _read_float(stream, child)
        stream.seek(child.end)


def _read_audio(stream: BinaryIO, master: _Element, track: _TrackProbe) -> None:
    stream.seek(master.payload_offset)
    while stream.tell() < master.end:
        child = _read_element(stream, master.end)
        if child is None:
            break
        if child.element_id == _SAMPLING_FREQUENCY:
            track.sample_rate = _read_float(stream, child)
        elif child.element_id == _CHANNELS:
            track.channels = _read_uint(stream, child)
        stream.seek(child.end)


def _read_uint(stream: BinaryIO, element: _Element) -> int:
    size = element.end - element.payload_offset
    if size < 1 or size > 8:
        raise MediaProbeError("EBML 无符号整数长度无效。")
    stream.seek(element.payload_offset)
    raw = stream.read(size)
    if len(raw) != size:
        raise MediaProbeError("EBML 无符号整数被截断。")
    return int.from_bytes(raw, "big")


def _read_float(stream: BinaryIO, element: _Element) -> float:
    size = element.end - element.payload_offset
    if size not in (4, 8):
        raise MediaProbeError("EBML 浮点数长度无效。")
    stream.seek(element.payload_offset)
    raw = stream.read(size)
    if len(raw) != size:
        raise MediaProbeError("EBML 浮点数被截断。")
    return float(struct.unpack(">f" if size == 4 else ">d", raw)[0])


def _read_text(stream: BinaryIO, element: _Element) -> str:
    size = element.end - element.payload_offset
    if size > 4096:
        raise MediaProbeError("EBML 文本字段异常过长。")
    stream.seek(element.payload_offset)
    raw = stream.read(size)
    if len(raw) != size:
        raise MediaProbeError("EBML 文本字段被截断。")
    return raw.rstrip(b"\0").decode("utf-8", errors="replace")


def _friendly_codec(codec_id: str) -> str:
    names = {
        "V_VP8": "VP8",
        "V_VP9": "VP9",
        "V_AV1": "AV1",
        "A_OPUS": "Opus",
        "A_VORBIS": "Vorbis",
        "A_PCM/INT/LIT": "PCM",
    }
    return names.get(codec_id, codec_id)


__all__ = ["MediaProbeError", "WebmProbe", "probe_webm"]
