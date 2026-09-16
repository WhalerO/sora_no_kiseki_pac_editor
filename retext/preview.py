from __future__ import annotations

import math
import re
import struct
import sys
import wave
import zlib
from array import array
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping

from PIL import Image, ImageChops

from .image_decode import open_asset_image

from .media_probe import probe_webm
from .model3d import (
    ModelAnimationClip,
    ModelAnimationNodeTransform,
    ModelAnimationPlayer,
    ModelGeometry,
    animation_model_family,
    model_3d_service,
    model_identity_service,
)
from .model3d.mdl import (
    ModelMaterial,
    ModelSection,
    parse_model_summary,
)


IMAGE_SUFFIXES = frozenset({".png", ".dds"})
AUDIO_SUFFIXES = frozenset({".wav"})
VIDEO_SUFFIXES = frozenset({".webm"})
FONT_SUFFIXES = frozenset({".fnt"})
MODEL_SUFFIXES = frozenset({".mdl"})
MODEL_INFO_SUFFIXES = frozenset({".mi"})
MEDIA_SUFFIXES = (
    IMAGE_SUFFIXES
    | AUDIO_SUFFIXES
    | VIDEO_SUFFIXES
    | FONT_SUFFIXES
    | MODEL_SUFFIXES
    | MODEL_INFO_SUFFIXES
)

MediaKind = Literal["image", "audio", "video", "font", "model", "model_info"]


@dataclass(slots=True, frozen=True)
class FontGlyph:
    index: int
    codepoint: int
    page: int
    atlas_x: int
    atlas_y: int
    width: int
    height: int
    channel_flags: int
    offset_x: int
    offset_y: int
    advance: int

    @property
    def character(self) -> str:
        if self.codepoint == 0x20:
            return "␠"
        if (
            0 <= self.codepoint <= 0x10FFFF
            and not 0xD800 <= self.codepoint <= 0xDFFF
        ):
            character = chr(self.codepoint)
            return character if character.isprintable() else "·"
        return "�"


@dataclass(slots=True)
class MediaPreview:
    kind: MediaKind
    source_path: Path
    metadata: list[tuple[str, str]]
    image: Any = None
    waveform: tuple[float, ...] = ()
    duration_seconds: float = 0.0
    warning: str = ""
    font_glyphs: tuple[FontGlyph, ...] = ()
    font_atlas_image: Any = None
    font_atlas_path: Path | None = None
    font_atlas_label: str = ""
    model_sections: tuple[ModelSection, ...] = ()
    model_materials: tuple[ModelMaterial, ...] = ()
    model_geometry: ModelGeometry | None = None
    model_animation_player: ModelAnimationPlayer | None = None
    model_companion_path: Path | None = None
    model_companion_label: str = ""
    model_info_fields: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class _AnimationSkeletonScore:
    matched_targets: int
    total_targets: int
    compatible_rotations: int
    compared_rotations: int
    mean_rotation_degrees: float

    @property
    def match_ratio(self) -> float:
        return (
            self.matched_targets / self.total_targets
            if self.total_targets
            else 0.0
        )

    @property
    def rotation_ratio(self) -> float:
        return (
            self.compatible_rotations / self.compared_rotations
            if self.compared_rotations
            else 1.0
        )

    @property
    def rank(self) -> tuple[float, float, float]:
        return (
            self.match_ratio,
            self.rotation_ratio,
            -self.mean_rotation_degrees,
        )


@dataclass(slots=True, frozen=True)
class _ModelCompanionCandidate:
    path: Path
    label: str
    sections: tuple[ModelSection, ...]
    materials: tuple[ModelMaterial, ...]
    score: _AnimationSkeletonScore


class AssetPreviewService:
    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in MEDIA_SUFFIXES

    def load(
        self,
        path: str | Path,
        *,
        font_atlas_path: str | Path | None = None,
        font_atlas_label: str = "",
        model_texture_resolver: Callable[[str], Path | None] | None = None,
        model_companion_resolver: (
            Callable[[str], tuple[Path, str] | None] | None
        ) = None,
        model_identity_tables: Mapping[str, str | Path] | None = None,
        model_logical_path: str = "",
    ) -> MediaPreview:
        source = Path(path).resolve()
        suffix = source.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return self._load_image(source)
        if suffix in AUDIO_SUFFIXES:
            return self._load_audio(source)
        if suffix in VIDEO_SUFFIXES:
            return self._load_video(source)
        if suffix in FONT_SUFFIXES:
            atlas = (
                Path(font_atlas_path).resolve()
                if font_atlas_path is not None
                else infer_font_atlas_path(source)
            )
            return self._load_font(source, atlas, font_atlas_label)
        if suffix in MODEL_SUFFIXES:
            return self._load_model(
                source,
                model_texture_resolver,
                model_companion_resolver,
                model_identity_tables,
                model_logical_path,
            )
        if suffix in MODEL_INFO_SUFFIXES:
            return self._load_model_info(source)
        raise ValueError(f"不支持的媒体预览格式：{suffix or '<无扩展名>'}")

    @staticmethod
    def _load_image(path: Path) -> MediaPreview:
        with open_asset_image(path) as source:
            format_name = source.format or path.suffix.lstrip(".").upper()
            width, height = source.size
            source_mode = source.mode
            frame_count = int(getattr(source, "n_frames", 1))
            source.seek(0)
            display = source.copy()
            display.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            display = display.convert("RGBA")
            metadata = [
                ("格式", format_name),
                ("尺寸", f"{width} × {height}"),
                ("颜色模式", source_mode),
                ("帧数", str(frame_count)),
                ("文件大小", _format_bytes(path.stat().st_size)),
            ]
            for key in ("asset_wrapper", "compression", "pixel_format", "gamma"):
                value = source.info.get(key)
                if isinstance(value, (str, int, float)):
                    metadata.append((key, str(value)))
        return MediaPreview(
            kind="image",
            source_path=path,
            metadata=metadata,
            image=display,
        )

    @staticmethod
    def _load_audio(path: Path) -> MediaPreview:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            duration = frame_count / sample_rate if sample_rate else 0.0
            waveform = (
                _sample_waveform(source, sample_width, channels, frame_count)
                if compression == "NONE"
                else ()
            )
        warning = "" if compression == "NONE" else f"暂不支持绘制 {compression} 压缩波形。"
        return MediaPreview(
            kind="audio",
            source_path=path,
            metadata=[
                ("格式", "WAV"),
                ("时长", _format_duration(duration)),
                ("声道", str(channels)),
                ("采样率", f"{sample_rate:,} Hz"),
                ("位深", f"{sample_width * 8} bit"),
                ("采样帧", f"{frame_count:,}"),
                ("编码", "PCM" if compression == "NONE" else compression),
                ("文件大小", _format_bytes(path.stat().st_size)),
            ],
            waveform=waveform,
            duration_seconds=duration,
            warning=warning,
        )

    @staticmethod
    def _load_video(path: Path) -> MediaPreview:
        metadata, duration, warning = _probe_video(path)
        return MediaPreview(
            kind="video",
            source_path=path,
            metadata=metadata,
            duration_seconds=duration,
            warning=warning,
        )

    @staticmethod
    def _load_font(
        path: Path,
        atlas_path: Path | None,
        atlas_label: str,
    ) -> MediaPreview:
        header, glyphs = _parse_font(path)
        atlas_image = None
        display = None
        warning_parts: list[str] = []
        invalid_bounds = 0
        if atlas_path is None or not atlas_path.is_file():
            warning_parts.append(
                "未加载可选的 DDS 字体图集；FNT 字形记录仍可正常查看。"
            )
            atlas_path = None
        else:
            try:
                with open_asset_image(atlas_path) as source:
                    atlas_image = source.convert("RGBA")
                display = _font_atlas_display(atlas_image)
                width, height = atlas_image.size
                invalid_bounds = sum(
                    1
                    for glyph in glyphs
                    if (
                        glyph.atlas_x < 0
                        or glyph.atlas_y < 0
                        or glyph.width < 0
                        or glyph.height < 0
                        or glyph.atlas_x + glyph.width > width
                        or glyph.atlas_y + glyph.height > height
                    )
                )
                if invalid_bounds:
                    warning_parts.append(
                        f"{invalid_bounds} 个字形的图集范围超出图片尺寸，已保留原始记录。"
                    )
            except Exception as exc:
                warning_parts.append(f"字体图集读取失败：{exc}")
                atlas_image = None
                display = None

        metadata = [
            ("格式", "Falcom FCV Font"),
            ("格式版本", str(header["version_major"])),
            ("基准尺寸", str(header["version_minor"])),
            ("字形数量", f"{len(glyphs):,}"),
            ("记录大小", f"{header['record_size']} B"),
            ("数据区大小", _format_bytes(header["payload_size"])),
            ("文件大小", _format_bytes(path.stat().st_size)),
        ]
        if atlas_image is not None and atlas_path is not None:
            metadata.extend(
                [
                    ("字体图集", atlas_label or atlas_path.name),
                    (
                        "图集尺寸",
                        f"{atlas_image.width} × {atlas_image.height}",
                    ),
                ]
            )
        return MediaPreview(
            kind="font",
            source_path=path,
            metadata=metadata,
            image=display,
            warning="\n".join(warning_parts),
            font_glyphs=glyphs,
            font_atlas_image=atlas_image,
            font_atlas_path=atlas_path,
            font_atlas_label=atlas_label or (
                atlas_path.name if atlas_path is not None else ""
            ),
        )

    @staticmethod
    def _load_model(
        path: Path,
        texture_resolver: Callable[[str], Path | None] | None,
        companion_resolver: (
            Callable[[str], tuple[Path, str] | None] | None
        ),
        identity_tables: Mapping[str, str | Path] | None,
        logical_path: str,
    ) -> MediaPreview:
        header, sections, source_materials = parse_model_summary(path)
        section_counts = {
            section.section_type: section.item_count
            for section in sections
        }
        has_source_mesh = 1 in section_counts and 4 in section_counts
        has_animation = 3 in section_counts
        identity_stem = (
            PurePosixPath(logical_path.replace("\\", "/")).stem
            if logical_path.strip()
            else path.stem
        )
        model_kind = (
            "网格动画模型"
            if has_source_mesh and has_animation
            else "骨骼动画资源"
            if has_animation
            else "网格模型"
            if has_source_mesh
            else "结构模型"
        )

        animation_clip = None
        animation_error = ""
        if has_animation:
            try:
                animation_clip = model_3d_service.load_animation(
                    path,
                    name=logical_path or path.name,
                )
            except Exception as exc:
                animation_error = str(exc)

        render_path = path
        render_materials = source_materials
        companion_path: Path | None = None
        companion_label = ""
        companion_sections: tuple[ModelSection, ...] = ()
        association_warnings: list[str] = []
        identity_warnings: list[str] = []
        animation_family = animation_model_family(identity_stem)

        if animation_clip is not None and not has_source_mesh:
            association_keys: tuple[str, ...] = ()
            if animation_family and identity_tables:
                association = model_identity_service.resolve_companion_models(
                    identity_stem,
                    identity_tables,
                )
                identity_warnings.extend(association.warnings)
                association_keys = association.model_keys

            selected_companion, selection_warnings = (
                _select_animation_companion(
                    animation_clip,
                    animation_family,
                    companion_resolver,
                    association_keys,
                    expected_version=header["version"],
                )
            )
            association_warnings.extend(selection_warnings)
            if selected_companion is not None:
                render_path = selected_companion.path
                render_materials = selected_companion.materials
                companion_path = selected_companion.path
                companion_label = selected_companion.label
                companion_sections = selected_companion.sections

        geometry = None
        geometry_error = ""
        texture_warnings: list[str] = []
        animation_player: ModelAnimationPlayer | None = None
        compatibility_player: ModelAnimationPlayer | None = None
        render_section_counts = {
            section.section_type: section.item_count
            for section in (
                companion_sections if companion_sections else sections
            )
        }
        if 1 in render_section_counts and 4 in render_section_counts:
            try:
                geometry = model_3d_service.load_geometry(render_path)
            except Exception as exc:
                geometry_error = str(exc)

        if geometry is not None and animation_clip is not None:
            try:
                compatibility_player = (
                    model_3d_service.create_animation_player(
                        geometry,
                        animation_clip,
                    )
                )
            except Exception as exc:
                animation_error = str(exc)
            if (
                compatibility_player is not None
                and not compatibility_player.compatible
                and not has_source_mesh
            ):
                # Animation-only camera/game-space files can share a broad
                # filename family with a character mesh.  Showing that mesh
                # would imply a false association, so retain only the parsed
                # track summary when compatibility is low.
                geometry = None

        base_color_material_count = sum(
            bool(material.base_color_texture)
            for material in render_materials
        )
        resolved_by_material: dict[int, Path] = {}
        missing_base_color_textures: tuple[str, ...] = ()
        if geometry is not None and texture_resolver is not None:
            resolved_by_name: dict[str, Path | None] = {}
            for material_index, material in enumerate(render_materials):
                texture_name = material.base_color_texture
                if not texture_name:
                    continue
                if texture_name not in resolved_by_name:
                    try:
                        resolved_by_name[texture_name] = texture_resolver(
                            texture_name
                        )
                    except Exception as exc:
                        resolved_by_name[texture_name] = None
                        texture_warnings.append(
                            f"贴图 {texture_name}.dds 查找失败：{exc}"
                        )
                texture_path = resolved_by_name[texture_name]
                if texture_path is not None and texture_path.is_file():
                    resolved_by_material[material_index] = texture_path
            missing_base_color_textures = tuple(
                texture_name
                for texture_name, texture_path in resolved_by_name.items()
                if texture_path is None or not texture_path.is_file()
            )
            if missing_base_color_textures:
                display_names = [
                    (
                        texture_name
                        if Path(texture_name).suffix
                        else f"{texture_name}.dds"
                    )
                    for texture_name in missing_base_color_textures
                ]
                displayed = "、".join(display_names[:6])
                if len(display_names) > 6:
                    displayed += f"，另有 {len(display_names) - 6} 个"
                texture_warnings.append(
                    f"未找到 {len(display_names):,} 个基础颜色 DDS："
                    f"{displayed}。请同时打开对应的 image*.pac；若已打开，"
                    "则当前游戏资源包本身没有提供这些 MDL 引用。"
                )
        if geometry is not None:
            geometry, color_warnings = model_3d_service.apply_textures(
                geometry,
                resolved_by_material,
                material_infos={
                    material_index: material.render_info(material_index)
                    for material_index, material in enumerate(render_materials)
                },
            )
            texture_warnings.extend(color_warnings)
            if (
                animation_clip is not None
                and compatibility_player is not None
                and compatibility_player.compatible
            ):
                animation_player = (
                    model_3d_service.create_animation_player(
                        geometry,
                        animation_clip,
                    )
                )

        texture_names = tuple(
            dict.fromkeys(
                texture
                for material in render_materials
                for texture in material.textures
                if texture
            )
        )
        metadata = [
            ("格式", "Falcom MDL"),
            ("格式版本", str(header["version"])),
            ("模型类型", model_kind),
            ("渲染后端", model_3d_service.backend_preference_label),
            ("区段数量", str(len(sections))),
            ("材质", f"{section_counts.get(0, 0):,}"),
            ("网格组", f"{section_counts.get(1, 0):,}"),
            ("层级节点", f"{section_counts.get(2, 0):,}"),
            ("动画记录", f"{section_counts.get(3, 0):,}"),
            ("图元数据块", f"{section_counts.get(4, 0):,}"),
            ("引用纹理", f"{len(texture_names):,}"),
            ("文件大小", _format_bytes(path.stat().st_size)),
        ]
        if animation_clip is not None:
            type_counts: dict[int, int] = {}
            for track in animation_clip.tracks:
                type_counts[track.track_type] = (
                    type_counts.get(track.track_type, 0) + 1
                )
            type_labels = {
                9: "位移",
                10: "旋转",
                11: "缩放",
                12: "标量/材质",
                13: "UV/二维",
                14: "颜色/三维",
            }
            metadata.extend(
                [
                    (
                        "动画时间",
                        f"{animation_clip.start:.3f}–"
                        f"{animation_clip.end:.3f} 秒",
                    ),
                    ("动画时长", f"{animation_clip.duration:.3f} 秒"),
                    ("动画轨道", f"{len(animation_clip.tracks):,}"),
                    ("动画关键帧", f"{animation_clip.keyframe_count:,}"),
                    (
                        "动画节点基准",
                        f"{len(animation_clip.node_transforms):,}",
                    ),
                    (
                        "节点目标",
                        f"{len(animation_clip.animated_bone_names):,}",
                    ),
                    (
                        "轨道类型",
                        "；".join(
                            f"{type_labels.get(track_type, str(track_type))}"
                            f" {count:,}"
                            for track_type, count in sorted(
                                type_counts.items()
                            )
                        )
                        or "无",
                    ),
                ]
            )
        if companion_path is not None:
            metadata.extend(
                [
                    ("绑定基础模型", companion_label or companion_path.name),
                    (
                        "基础模型大小",
                        _format_bytes(companion_path.stat().st_size),
                    ),
                    (
                        "基础模型网格组",
                        f"{render_section_counts.get(1, 0):,}",
                    ),
                ]
            )
        if compatibility_player is not None:
            metadata.extend(
                [
                    (
                        "节点匹配",
                        f"{compatibility_player.matched_target_count:,} / "
                        f"{compatibility_player.skeletal_target_count:,}",
                    ),
                    (
                        "有效蒙皮顶点",
                        f"{compatibility_player.skinned_vertex_count:,}",
                    ),
                    (
                        "刚性节点顶点",
                        f"{compatibility_player.rigid_vertex_count:,}",
                    ),
                    (
                        "受动画影响顶点",
                        f"{compatibility_player.affected_vertex_count:,}",
                    ),
                    (
                        "几何覆盖率",
                        f"{compatibility_player.affected_vertex_ratio:.1%}",
                    ),
                    (
                        "匹配率",
                        f"{compatibility_player.compatibility_ratio:.1%}",
                    ),
                    (
                        "姿态采样",
                        (
                            "NumPy 向量化"
                            if compatibility_player.uses_numpy
                            else "Python 兼容模式"
                        ),
                    ),
                ]
            )
        if identity_tables:
            identity = model_identity_service.resolve(
                identity_stem,
                identity_tables,
            )
            identity_warnings.extend(identity.warnings)
            if identity.matches:
                names = [match.name for match in identity.matches]
                displayed_names = "；".join(names[:8])
                if len(names) > 8:
                    displayed_names += f"；另有 {len(names) - 8} 个候选"
                first_match = identity.matches[0]
                metadata[3:3] = [
                    ("对象类别", first_match.kind),
                    ("对象名称", displayed_names),
                    (
                        "名称追溯",
                        f"{first_match.source_table} · "
                        f"{first_match.match_mode} "
                        f"{first_match.model_key}",
                    ),
                ]
            else:
                searched = "、".join(identity.searched_tables)
                metadata[3:3] = [
                    ("对象名称", "未匹配到名称记录"),
                    (
                        "名称追溯",
                        (
                            f"{searched} · 检索键 {identity_stem}"
                            if searched
                            else "未找到可用名称表"
                        ),
                    ),
                ]
        else:
            metadata[3:3] = [
                (
                    "对象名称",
                    "未追溯（未找到 t_name.tbl / t_status.tbl）",
                )
            ]
        if geometry is not None:
            metadata.extend(
                [
                    ("三维顶点", f"{geometry.source_vertex_count:,}"),
                    ("三角形", f"{geometry.source_triangle_count:,}"),
                    ("预览三角形", f"{len(geometry.faces):,}"),
                    (
                        "预览网格",
                        "完整拓扑（未丢弃面元）",
                    ),
                    (
                        "基础颜色贴图",
                        f"{geometry.textured_material_count:,} / "
                        f"{base_color_material_count:,} 个材质",
                    ),
                ]
            )
            if missing_base_color_textures:
                metadata.append(
                    (
                        "缺失基础颜色 DDS",
                        f"{len(missing_base_color_textures):,} 个唯一引用",
                    )
                )
        for section in sections:
            metadata.append(
                (
                    f"区段 0x{section.section_type:02X} · {section.label}",
                    f"{section.item_count:,} 项 / {_format_bytes(section.stored_size)}",
                )
            )
        for index, material in enumerate(render_materials, start=1):
            details = [material.name or "<未命名>"]
            if material.shader:
                details.append(f"shader={material.shader}")
            if material.variant and material.variant != material.shader:
                details.append(f"variant={material.variant}")
            metadata.append((f"材质 {index}", " · ".join(details)))
        for index, texture in enumerate(texture_names, start=1):
            display_name = (
                texture
                if Path(texture).suffix
                else f"{texture}.dds"
            )
            metadata.append((f"纹理 {index}", display_name))
        if geometry is not None:
            if geometry.textured_material_count:
                warning_parts = [
                    "已使用外部 DDS，并按材质的 UV、采样器、剔除和混合"
                    "状态绘制基础颜色；"
                    "尚未复现遮罩、法线贴图、toon 和游戏着色器。"
                ]
            elif base_color_material_count:
                warning_parts = [
                    "MDL 只保存贴图引用；当前未加载外部 DDS，"
                    "因此使用材质分组色显示。"
                ]
            else:
                warning_parts = [
                    (
                        "当前显示动画网格；"
                        if animation_player is not None
                        else "当前显示绑定网格；"
                    )
                    + "该模型没有可用的基础颜色贴图引用，"
                    "当前使用材质分组色显示。"
                ]
            warning_parts.extend(geometry.warnings)
            warning_parts.extend(texture_warnings)
        elif geometry_error:
            warning_parts = [
                "MDL 结构摘要可用，但三维几何无法载入："
                f"{geometry_error}"
            ]
        else:
            warning_parts = ["该 MDL 不包含可直接渲染的网格。"]
        if animation_clip is not None:
            if not animation_clip.tracks:
                warning_parts.append(
                    "该文件包含零轨道、零时长的空动画记录。"
                )
            elif animation_player is not None:
                warning_parts.append(
                    "动画已解析并绑定，并按 KuroTools 约定处理："
                    "位移/缩放采用线性插值，旋转键作为相对动画节点"
                    "基准姿态的增量并采用四元数 SLERP。"
                )
                if animation_player.warning:
                    warning_parts.append(animation_player.warning)
                if not animation_player.uses_numpy:
                    warning_parts.append(
                        "当前运行环境未启用 NumPy 向量化；"
                        "自动播放已降为低刷新率，仍可拖动时间轴逐帧查看。"
                    )
            elif compatibility_player is not None:
                warning_parts.append(compatibility_player.warning)
                if not compatibility_player.compatible:
                    warning_parts.append(
                        "低兼容轨道可能属于镜头、GameSpace 或演出控制，"
                        "已阻止其强行套用到基础网格。"
                    )
            elif animation_error:
                warning_parts.append(
                    f"动画区段无法应用到网格：{animation_error}"
                )
            elif not has_source_mesh and companion_path is None:
                if animation_family:
                    warning_parts.append(
                        f"动画已解析，但未找到可明确绑定的基础模型 "
                        f"{animation_family}.mdl。"
                    )
                else:
                    warning_parts.append(
                        "动画已解析，但文件名未提供可安全推断的基础模型。"
                    )
            if any(
                track.track_type in {12, 13, 14}
                for track in animation_clip.tracks
            ):
                warning_parts.append(
                    "材质标量、UV 和颜色轨道已列入结构信息，"
                    "当前版本暂未把它们应用到实时材质。"
                )
        elif animation_error:
            warning_parts.append(f"动画区段解析失败：{animation_error}")
        warning_parts.extend(association_warnings)
        warning_parts.extend(identity_warnings)
        if not identity_tables:
            warning_parts.append(
                "如需追溯角色、NPC 或怪物名称，可同时打开对应语言的 "
                "table_tc.pac/table_sc.pac，或在解包目录中保留同级 table_*。"
            )
        return MediaPreview(
            kind="model",
            source_path=path,
            metadata=metadata,
            duration_seconds=(
                animation_clip.duration
                if animation_clip is not None
                else 0.0
            ),
            warning="\n".join(warning_parts),
            model_sections=sections,
            model_materials=render_materials,
            model_geometry=geometry,
            model_animation_player=animation_player,
            model_companion_path=companion_path,
            model_companion_label=companion_label,
        )

    @staticmethod
    def _load_model_info(path: Path) -> MediaPreview:
        header, fields = _parse_model_info_summary(path)
        known_sections = [
            name
            for name in (
                "Bounding",
                "Collider",
                "Animation",
                "Occluder",
                "TwoBoneIK",
                "LookIK",
                "DynamicBone",
                "DynamicBoneCollider",
                "LOD",
                "Lights",
                "Locators",
                "DrivenKeys",
                "Extra",
            )
            if name in fields
        ]
        metadata = [
            ("格式", "Falcom Binary JSON (MI)"),
            ("格式版本", str(header["version"])),
            ("字段字典", f"{len(fields):,} 项"),
            ("已知结构名", f"{len(known_sections):,} 项"),
            ("二进制载荷", _format_bytes(header["payload_size"])),
            ("文件大小", _format_bytes(path.stat().st_size)),
        ]
        for index, name in enumerate(known_sections, start=1):
            metadata.append((f"结构名 {index}", name))
        for index, name in enumerate(fields, start=1):
            metadata.append((f"字段 {index}", name))
        return MediaPreview(
            kind="model_info",
            source_path=path,
            metadata=metadata,
            warning=(
                "MI 是带校验字段字典的 Falcom 二进制 JSON；"
                "当前提供只读结构目录，尚未展开内部配置值。"
            ),
            model_info_fields=fields,
        )


def _select_animation_companion(
    clip: ModelAnimationClip,
    animation_family: str,
    resolver: Callable[[str], tuple[Path, str] | None] | None,
    association_keys: tuple[str, ...],
    *,
    expected_version: int,
) -> tuple[_ModelCompanionCandidate | None, tuple[str, ...]]:
    """Choose a mesh without silently binding an animation to a wrong outfit."""

    if not animation_family or resolver is None:
        return None, ()
    warnings: list[str] = []
    candidates: list[_ModelCompanionCandidate] = []
    candidates_by_path: dict[Path, _ModelCompanionCandidate] = {}
    inspected_paths: set[Path] = set()
    validation_results: dict[Path, tuple[bool, str]] = {}

    def add_candidate(
        model_key: str,
        *,
        report_error: bool,
    ) -> _ModelCompanionCandidate | None:
        try:
            resolved = resolver(model_key)
        except Exception as exc:
            if report_error:
                warnings.append(
                    f"基础模型 {model_key} 查找失败：{exc}"
                )
            return None
        if resolved is None:
            return None
        candidate_path, candidate_label = resolved
        normalized_path = Path(candidate_path).resolve()
        existing = candidates_by_path.get(normalized_path)
        if existing is not None:
            return existing
        if normalized_path in inspected_paths:
            return None
        inspected_paths.add(normalized_path)
        try:
            candidate = _inspect_animation_companion(
                clip,
                normalized_path,
                candidate_label or normalized_path.name,
                expected_version=expected_version,
            )
        except Exception as exc:
            if report_error:
                warnings.append(
                    f"基础模型 {candidate_label or normalized_path.name} "
                    f"无法用于预览：{exc}"
                )
            return None
        candidates.append(candidate)
        candidates_by_path[normalized_path] = candidate
        return candidate

    def candidate_is_playable(
        candidate: _ModelCompanionCandidate,
        *,
        report_error: bool,
    ) -> bool:
        result = validation_results.get(candidate.path)
        if result is None:
            try:
                _validate_animation_companion(clip, candidate.path)
            except Exception as exc:
                result = (False, str(exc))
            else:
                result = (True, "")
            validation_results[candidate.path] = result
        playable, error = result
        if not playable and report_error:
            warnings.append(
                f"基础模型 {candidate.label} 无法用于预览：{error}"
            )
        return playable

    primary_candidate = add_candidate(
        animation_family,
        report_error=True,
    )
    primary = (
        primary_candidate
        if (
            primary_candidate is not None
            and candidate_is_playable(
                primary_candidate,
                report_error=True,
            )
        )
        else None
    )
    associated = tuple(
        dict.fromkeys(
            key
            for key in association_keys
            if key.casefold() != animation_family.casefold()
        )
    )
    numeric_family = re.fullmatch(
        r"(?:chr|mon)\d{4}",
        animation_family,
        flags=re.IGNORECASE,
    )
    should_probe = (
        primary_candidate is None
        or primary is None
        or _companion_score_needs_alternatives(primary_candidate.score)
    )
    if should_probe:
        probe_keys: list[str] = []
        if len(associated) <= 20:
            probe_keys.extend(associated)
        elif primary is None:
            warnings.append(
                f"名称表给出 {len(associated):,} 个可能的基础模型；"
                "候选过多，未自动猜测角色或服装。"
            )
        if numeric_family is not None:
            probe_keys.extend(
                f"{animation_family}_c{index:02d}"
                for index in range(100)
            )
        for key in dict.fromkeys(probe_keys):
            add_candidate(key, report_error=False)

    if not candidates:
        if len(associated) > 1 and not warnings:
            warnings.append(
                f"名称表给出 {len(associated):,} 个可能的基础模型；"
                "为避免套用错误角色或服装，未自动选择。"
            )
        return None, tuple(dict.fromkeys(warnings))
    ordered = sorted(
        candidates,
        key=lambda candidate: candidate.score.rank,
        reverse=True,
    )
    playable_candidates: list[_ModelCompanionCandidate] = []
    for candidate in ordered:
        if candidate_is_playable(
            candidate,
            report_error=(candidate is primary_candidate),
        ):
            playable_candidates.append(candidate)
            # Candidates are ordered by the exact score used for selection.
            # Once two playable candidates are found, a later candidate
            # cannot displace either the best or the runner-up.  Avoid
            # decoding every costume's multi-megabyte geometry.
            if len(playable_candidates) == 2:
                break

    if not playable_candidates:
        if not warnings:
            warnings.append(
                "找到基础模型候选，但没有文件同时通过可渲染几何、"
                "兼容节点和受动画影响顶点检查。"
            )
        return None, tuple(dict.fromkeys(warnings))
    if len(playable_candidates) == 1:
        selected = playable_candidates[0]
        if primary is None:
            warnings.append(
                f"已使用唯一通过可动画几何/节点检查的基础模型 "
                f"{selected.label}。"
            )
        return selected, tuple(dict.fromkeys(warnings))

    best, runner_up = playable_candidates
    if _companion_score_clearly_better(best.score, runner_up.score):
        if (
            primary_candidate is not None
            and best.path != primary_candidate.path
        ):
            warnings.append(
                "同名基础模型无法播放，或其动画骨骼覆盖、节点基准"
                f"不完整；已按兼容度改用 {best.label}"
                f"（{best.score.matched_targets} / "
                f"{best.score.total_targets} 个目标）。"
            )
        else:
            warnings.append(
                f"已从基础模型候选中按骨骼覆盖和节点基准选择 "
                f"{best.label}"
                f"（{best.score.matched_targets} / "
                f"{best.score.total_targets} 个目标）。"
            )
        return best, tuple(dict.fromkeys(warnings))
    warnings.append(
        "找到至少 2 个可播放但兼容度接近的基础模型；"
        "最佳候选未相对第二名清晰胜出，为避免猜测角色或服装，"
        "未自动选择。"
    )
    return None, tuple(dict.fromkeys(warnings))


def _inspect_animation_companion(
    clip: ModelAnimationClip,
    path: Path,
    label: str,
    *,
    expected_version: int,
) -> _ModelCompanionCandidate:
    header, sections, materials = parse_model_summary(path)
    section_types = {section.section_type for section in sections}
    if header["version"] != expected_version:
        raise ValueError("基础模型与动画文件的 MDL 版本不一致。")
    if not {1, 4}.issubset(section_types):
        raise ValueError("候选文件不包含完整网格区段。")
    transforms = model_3d_service.load_node_transforms(path)
    return _ModelCompanionCandidate(
        path=path,
        label=label,
        sections=sections,
        materials=materials,
        score=_score_animation_skeleton(clip, transforms),
    )


def _validate_animation_companion(
    clip: ModelAnimationClip,
    path: Path,
) -> None:
    """Reject structurally plausible meshes that cannot play the clip."""

    geometry = model_3d_service.load_geometry(path)
    if geometry is None or not geometry.vertices or not geometry.faces:
        raise ValueError("候选文件没有可渲染的三角网格。")
    if geometry.skeleton is None:
        raise ValueError("候选网格没有可用于动画的骨骼层级。")
    player = model_3d_service.create_animation_player(geometry, clip)
    if not player.compatible:
        raise ValueError(player.warning or "动画与候选模型节点不兼容。")
    if not player.affected_vertex_count:
        raise ValueError(
            "候选模型的匹配节点没有影响任何可渲染顶点。"
        )
    # Validate the hierarchy and bind matrices without performing a full
    # vertex skinning pass.  Candidate geometry is deliberately not retained:
    # keeping several outfits and their NumPy caches alive can consume
    # hundreds of MiB.  Only the selected file is loaded again for display.
    player.sample_pose(0.0)


def _score_animation_skeleton(
    clip: ModelAnimationClip,
    transforms: tuple[ModelAnimationNodeTransform, ...],
) -> _AnimationSkeletonScore:
    targets = tuple(
        dict.fromkeys(
            track.bone_name.casefold()
            for track in clip.tracks
            if (
                track.track_type in {9, 10, 11}
                and track.keyframes
                and track.bone_name
            )
        )
    )
    candidate_by_name = {
        transform.name.casefold(): transform
        for transform in transforms
    }
    source_by_name = {
        transform.name.casefold(): transform
        for transform in clip.node_transforms
    }
    matched = sum(name in candidate_by_name for name in targets)
    rotation_targets = tuple(
        dict.fromkeys(
            track.bone_name.casefold()
            for track in clip.tracks
            if (
                track.track_type == 10
                and track.keyframes
                and track.bone_name
            )
        )
    )
    angles = [
        _quaternion_angle_degrees(
            source_by_name[name].bind_rotation,
            candidate_by_name[name].bind_rotation,
        )
        for name in rotation_targets
        if name in source_by_name and name in candidate_by_name
    ]
    return _AnimationSkeletonScore(
        matched_targets=matched,
        total_targets=len(targets),
        compatible_rotations=sum(angle <= 1.0 for angle in angles),
        compared_rotations=len(angles),
        mean_rotation_degrees=(
            sum(angles) / len(angles) if angles else 0.0
        ),
    )


def _quaternion_angle_degrees(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    left_length = math.sqrt(sum(value * value for value in left))
    right_length = math.sqrt(sum(value * value for value in right))
    if left_length <= 1e-12 or right_length <= 1e-12:
        return 180.0
    dot = abs(
        sum(a * b for a, b in zip(left, right))
        / (left_length * right_length)
    )
    return math.degrees(2.0 * math.acos(min(1.0, max(-1.0, dot))))


def _companion_score_needs_alternatives(
    score: _AnimationSkeletonScore,
) -> bool:
    if not score.total_targets:
        return False
    return score.match_ratio < 0.95 or score.rotation_ratio < 0.95


def _companion_score_clearly_better(
    candidate: _AnimationSkeletonScore,
    baseline: _AnimationSkeletonScore,
) -> bool:
    if candidate.match_ratio >= baseline.match_ratio + 0.04:
        return True
    if abs(candidate.match_ratio - baseline.match_ratio) > 0.02:
        return False
    if candidate.rotation_ratio >= baseline.rotation_ratio + 0.15:
        return True
    return (
        candidate.rotation_ratio > baseline.rotation_ratio
        and candidate.mean_rotation_degrees
        <= baseline.mean_rotation_degrees - 3.0
    )


def infer_font_atlas_path(path: str | Path) -> Path | None:
    source = Path(path)
    lowered = [part.casefold() for part in source.parts]
    for index in range(len(lowered) - 1):
        if lowered[index : index + 2] == ["common", "font"]:
            return Path(
                *source.parts[:index],
                "dx11",
                "image",
                f"{source.stem}.dds",
            )
    return None


def infer_model_identity_table_paths(
    path: str | Path,
) -> dict[str, Path]:
    """Find localized model-name tables near an unpacked asset tree."""

    source = Path(path).resolve()
    for ancestor in source.parents:
        candidate_directories = (
            ancestor / "table_tc",
            ancestor / "table_sc",
            ancestor / "table",
            ancestor,
        )
        for directory in candidate_directories:
            tables = {
                key: directory / f"{key}.tbl"
                for key in ("t_name", "t_status")
                if (directory / f"{key}.tbl").is_file()
            }
            if tables:
                return tables
    return {}


def infer_font_atlas_entry(logical_path: str) -> str | None:
    parts = logical_path.replace("\\", "/").split("/")
    lowered = [part.casefold() for part in parts]
    for index in range(len(lowered) - 1):
        if lowered[index : index + 2] == ["common", "font"]:
            stem = Path(parts[-1]).stem
            return "/".join([*parts[:index], "dx11", "image", f"{stem}.dds"])
    return None


def infer_font_atlas_pac_name(logical_path: str) -> str | None:
    return infer_image_pac_name(logical_path)


def infer_image_pac_name(logical_path: str) -> str | None:
    parts = [
        part
        for part in logical_path.replace("\\", "/").split("/")
        if part
    ]
    if not parts:
        return None
    asset_root = parts[0].casefold()
    if asset_root == "asset":
        return "image.pac"
    if asset_root.startswith("asset_") and len(asset_root) > len("asset_"):
        language = asset_root[len("asset_") :]
        return f"image_{language}.pac"
    return None


def infer_model_texture_entry(
    logical_path: str,
    texture_name: str,
) -> str | None:
    parts = [
        part
        for part in logical_path.replace("\\", "/").split("/")
        if part
    ]
    texture_parts = _safe_model_texture_parts(texture_name)
    if not parts or texture_parts is None:
        return None
    return "/".join((parts[0], "dx11", "image", *texture_parts))


def infer_model_texture_path(
    model_path: str | Path,
    texture_name: str,
) -> Path | None:
    source = Path(model_path)
    lowered = [part.casefold() for part in source.parts]
    texture_parts = _safe_model_texture_parts(texture_name)
    if texture_parts is None:
        return None
    for index in range(len(lowered) - 1):
        if lowered[index : index + 2] == ["common", "model"]:
            return Path(
                *source.parts[:index],
                "dx11",
                "image",
                *texture_parts,
            )
    return None


def _safe_model_texture_parts(texture_name: str) -> tuple[str, ...] | None:
    parts = tuple(texture_name.replace("\\", "/").split("/"))
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
    ):
        return None
    if not Path(parts[-1]).suffix:
        parts = (*parts[:-1], f"{parts[-1]}.dds")
    return parts


def render_font_glyph(
    preview: MediaPreview,
    glyph: FontGlyph,
    *,
    target_size: int = 360,
) -> Image.Image | None:
    atlas = preview.font_atlas_image
    if preview.kind != "font" or atlas is None:
        return None
    if (
        glyph.width <= 0
        or glyph.height <= 0
        or glyph.atlas_x < 0
        or glyph.atlas_y < 0
        or glyph.atlas_x + glyph.width > atlas.width
        or glyph.atlas_y + glyph.height > atlas.height
    ):
        return None
    crop = atlas.crop(
        (
            glyph.atlas_x,
            glyph.atlas_y,
            glyph.atlas_x + glyph.width,
            glyph.atlas_y + glyph.height,
        )
    )
    if glyph.channel_flags & 0x100:
        mask = crop.getchannel("G")
    elif glyph.channel_flags & 0x200:
        mask = crop.getchannel("R")
    else:
        red, green, blue, _alpha = crop.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    usable = max(target_size - 48, 1)
    scale = max(1, min(12, usable // max(glyph.width, glyph.height, 1)))
    mask = mask.resize(
        (max(glyph.width * scale, 1), max(glyph.height * scale, 1)),
        Image.Resampling.NEAREST,
    )
    result = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    glyph_image = Image.new("RGBA", mask.size, (255, 255, 255, 0))
    glyph_image.putalpha(mask)
    result.alpha_composite(
        glyph_image,
        (
            (target_size - glyph_image.width) // 2,
            (target_size - glyph_image.height) // 2,
        ),
    )
    return result


def _parse_font(path: Path) -> tuple[dict[str, int], tuple[FontGlyph, ...]]:
    data = path.read_bytes()
    if len(data) < 40:
        raise ValueError("FNT 文件不足 40 字节，无法读取 FCV 头。")
    (
        magic,
        version_major,
        version_minor,
        glyph_count,
        header_value_0,
        header_value_1,
        header_value_2,
        header_value_3,
        header_value_4,
        header_value_5,
        header_value_6,
        table_magic,
        payload_size,
    ) = struct.unpack_from("<4sHHIHHHHIII4sI", data, 0)
    if magic != b"FCV\0":
        raise ValueError(f"不是支持的 FCV 字体文件：magic={magic!r}")
    if table_magic != b"FLTI":
        raise ValueError(f"FNT 字形表 magic 无效：{table_magic!r}")
    record_size = 24
    expected_size = glyph_count * record_size
    if payload_size != expected_size:
        raise ValueError(
            f"FNT 字形表大小不一致：头中为 {payload_size}，"
            f"{glyph_count} 条记录应为 {expected_size}。"
        )
    if len(data) != 40 + payload_size:
        raise ValueError(
            f"FNT 文件长度不一致：实际 {len(data)}，预期 {40 + payload_size}。"
        )
    glyphs: list[FontGlyph] = []
    seen_codepoints: set[int] = set()
    for index in range(glyph_count):
        offset = 40 + index * record_size
        (
            codepoint,
            page,
            atlas_x,
            atlas_y,
            width,
            height,
            channel_flags,
            offset_x,
            offset_y,
            advance,
        ) = struct.unpack_from("<IIHHHHHhHH", data, offset)
        if codepoint in seen_codepoints:
            raise ValueError(f"FNT 包含重复码位：U+{codepoint:04X}")
        seen_codepoints.add(codepoint)
        glyphs.append(
            FontGlyph(
                index=index,
                codepoint=codepoint,
                page=page,
                atlas_x=atlas_x,
                atlas_y=atlas_y,
                width=width,
                height=height,
                channel_flags=channel_flags,
                offset_x=offset_x,
                offset_y=offset_y,
                advance=advance,
            )
        )
    return {
        "version_major": version_major,
        "version_minor": version_minor,
        "record_size": record_size,
        "payload_size": payload_size,
        "header_value_0": header_value_0,
        "header_value_1": header_value_1,
        "header_value_2": header_value_2,
        "header_value_3": header_value_3,
        "header_value_4": header_value_4,
        "header_value_5": header_value_5,
        "header_value_6": header_value_6,
    }, tuple(glyphs)


def _font_atlas_display(atlas: Image.Image) -> Image.Image:
    red, green, blue, _alpha = atlas.split()
    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    display = Image.new("RGBA", atlas.size, (255, 255, 255, 0))
    display.putalpha(mask)
    display.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    return display


def _parse_model_info_summary(
    path: Path,
) -> tuple[dict[str, int], tuple[str, ...]]:
    file_size = path.stat().st_size
    if file_size < 22:
        raise ValueError("MI 文件不足 22 字节，无法读取二进制 JSON 头。")
    with path.open("rb") as source:
        raw_header = source.read(21)
        magic, version, payload_offset, header_value, root_parent = struct.unpack(
            "<4sIIII",
            raw_header[:20],
        )
        if magic != b"JSON":
            raise ValueError(f"不是支持的 MI 二进制 JSON：magic={magic!r}")
        if raw_header[20] != 0:
            raise ValueError("MI 字段字典缺少起始空字符串标记。")
        if payload_offset < 21 or payload_offset >= file_size:
            raise ValueError(
                f"MI 载荷偏移无效：0x{payload_offset:X}，"
                f"文件大小为 0x{file_size:X}。"
            )
        dictionary_size = payload_offset - 21
        if dictionary_size > 16 * 1024 * 1024:
            raise ValueError("MI 字段字典超过 16 MiB，拒绝整体载入。")
        dictionary = source.read(dictionary_size)
        if len(dictionary) != dictionary_size:
            raise ValueError("MI 字段字典数据不完整。")

    fields: list[str] = []
    seen_fields: set[str] = set()
    offset = 0
    while offset < len(dictionary):
        if offset + 5 > len(dictionary):
            raise ValueError("MI 字段字典末尾记录不完整。")
        stored_hash = struct.unpack_from("<I", dictionary, offset)[0]
        offset += 4
        terminator = dictionary.find(b"\0", offset)
        if terminator < 0:
            raise ValueError("MI 字段字典字符串缺少 NUL 结尾。")
        raw_name = dictionary[offset:terminator]
        offset = terminator + 1
        if not raw_name:
            raise ValueError("MI 字段字典包含空字段名。")
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("MI 字段字典包含无效 UTF-8。") from exc
        expected_hash = (~zlib.crc32(raw_name)) & 0xFFFFFFFF
        if stored_hash != expected_hash:
            raise ValueError(
                f"MI 字段 {name!r} 的校验值无效："
                f"0x{stored_hash:08X} != 0x{expected_hash:08X}。"
            )
        if name in seen_fields:
            raise ValueError(f"MI 字段字典包含重复名称：{name!r}。")
        seen_fields.add(name)
        fields.append(name)
        if len(fields) > 100_000:
            raise ValueError("MI 字段数量异常，已停止解析。")
    return {
        "version": version,
        "payload_offset": payload_offset,
        "payload_size": file_size - payload_offset,
        "header_value": header_value,
        "root_parent": root_parent,
    }, tuple(fields)


def _sample_waveform(
    source: wave.Wave_read,
    sample_width: int,
    channels: int,
    frame_count: int,
    *,
    bins: int = 900,
    window_frames: int = 1024,
) -> tuple[float, ...]:
    if frame_count <= 0 or sample_width not in {1, 2, 3, 4}:
        return ()
    count = min(bins, frame_count)
    window = min(window_frames, frame_count)
    last_start = max(0, frame_count - window)
    peaks: list[float] = []
    for index in range(count):
        start = round(last_start * index / max(count - 1, 1))
        source.setpos(start)
        raw = source.readframes(window)
        peaks.append(_pcm_peak(raw, sample_width, channels))
    return tuple(peaks)


def _pcm_peak(raw: bytes, sample_width: int, channels: int) -> float:
    del channels
    if not raw:
        return 0.0
    if sample_width == 1:
        return min(1.0, max(abs(value - 128) for value in raw) / 128)
    if sample_width == 2:
        values = array("h")
        values.frombytes(raw[: len(raw) - len(raw) % 2])
        if sys.byteorder != "little":
            values.byteswap()
        scale = 32768
    elif sample_width == 4:
        values = array("i")
        values.frombytes(raw[: len(raw) - len(raw) % 4])
        if sys.byteorder != "little":
            values.byteswap()
        scale = 2147483648
    else:
        peak = 0
        for offset in range(0, len(raw) - 2, 3):
            value = int.from_bytes(raw[offset : offset + 3], "little", signed=True)
            peak = max(peak, abs(value))
        return min(1.0, peak / 8388608)
    if not values:
        return 0.0
    return min(1.0, max(abs(value) for value in values) / scale)


def _probe_video(path: Path) -> tuple[list[tuple[str, str]], float, str]:
    try:
        probe = probe_webm(path)
    except Exception as exc:
        return [
            ("格式", "WebM"),
            ("文件大小", _format_bytes(path.stat().st_size)),
        ], 0.0, f"WebM 头信息解析失败：{exc}；仍可尝试在预览页内播放。"

    duration = probe.duration_seconds
    metadata: list[tuple[str, str]] = [
        ("格式", "WebM"),
        ("时长", _format_duration(duration)),
        ("文件大小", _format_bytes(path.stat().st_size)),
    ]
    if probe.video_codec:
        metadata.append(("视频编码", probe.video_codec))
    if probe.width > 0 and probe.height > 0:
        metadata.append(("画面尺寸", f"{probe.width} × {probe.height}"))
    if probe.frame_rate > 0.0:
        metadata.append(("帧率", f"{probe.frame_rate:.3f} fps"))
    if probe.audio_codec:
        metadata.append(("音频编码", probe.audio_codec))
    if probe.audio_channels > 0:
        metadata.append(("音频声道", str(probe.audio_channels)))
    if probe.audio_sample_rate > 0.0:
        metadata.append(("音频采样率", f"{probe.audio_sample_rate:g} Hz"))
    return metadata, duration, ""


def _format_duration(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"
    return f"{minutes}:{whole_seconds:02d}.{millis:03d}"


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"
