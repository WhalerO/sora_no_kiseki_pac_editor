from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .geometry import ModelMaterialRenderInfo


@dataclass(slots=True, frozen=True)
class ModelSection:
    section_type: int
    offset: int
    stored_size: int
    item_count: int

    @property
    def label(self) -> str:
        return {
            0: "材质",
            1: "网格",
            2: "节点层级",
            3: "动画",
            4: "图元数据",
        }.get(self.section_type, f"未知类型 {self.section_type}")


@dataclass(slots=True, frozen=True)
class ModelMaterial:
    name: str
    shader: str
    variant: str
    textures: tuple[str, ...]
    texture_slots: tuple[int, ...] = ()
    texture_wrap_s: tuple[int, ...] = ()
    texture_wrap_t: tuple[int, ...] = ()
    shader_parameters: tuple[
        tuple[str, int, tuple[float, ...]],
        ...,
    ] = ()
    material_switches: tuple[tuple[str, int], ...] = ()
    uv_map_indices: tuple[int, ...] = ()
    trailing_properties: tuple[int, int, int, float, int] = (
        0,
        0,
        0,
        0.0,
        0,
    )

    @property
    def base_color_texture(self) -> str:
        base_index = self._base_texture_index()
        return self.textures[base_index] if base_index is not None else ""

    @property
    def diffuse_texture(self) -> str:
        return self.base_color_texture

    def render_info(self, material_index: int) -> ModelMaterialRenderInfo:
        base_index = self._base_texture_index()
        base_texture_name = (
            self.textures[base_index]
            if base_index is not None
            else ""
        )
        wrap_s = (
            self.texture_wrap_s[base_index]
            if (
                base_index is not None
                and base_index < len(self.texture_wrap_s)
            )
            else 1
        )
        wrap_t = (
            self.texture_wrap_t[base_index]
            if (
                base_index is not None
                and base_index < len(self.texture_wrap_t)
            )
            else 1
        )
        uv_selector = 0
        if base_index is not None:
            slot = self.texture_slots[base_index]
            uv_selector = slot % 3 if slot < 6 else 0
        uv_channel = (
            self.uv_map_indices[uv_selector]
            if uv_selector < len(self.uv_map_indices)
            else 0
        )
        enabled_switches = {
            name.upper()
            for name, value in self.material_switches
            if value
        }
        render_flag = self.trailing_properties[0]
        blend_mode = (
            "mask"
            if "SWITCH_ALPHATEST" in enabled_switches
            else "blend"
            if render_flag == 1
            else "additive"
            if render_flag == 2
            else "opaque"
        )
        cull_mode = self.trailing_properties[1]
        if "SWITCH_DOUBLESIDE" in enabled_switches:
            cull_mode = 0
        alpha_cutoff = next(
            (
                values[0]
                for name, _parameter_type, values in self.shader_parameters
                if name == "alphaTestThreshold_g" and values
            ),
            0.5,
        )
        return ModelMaterialRenderInfo(
            material_index=material_index,
            base_texture_name=base_texture_name,
            uv_channel=uv_channel,
            wrap_s=wrap_s,
            wrap_t=wrap_t,
            blend_mode=blend_mode,
            cull_mode=cull_mode,
            shadow_only="SWITCH_SHADOWONLY" in enabled_switches,
            alpha_cutoff=min(1.0, max(0.0, alpha_cutoff)),
        )

    def _base_texture_index(self) -> int | None:
        """Resolve the enabled diffuse layer without assuming slot zero.

        KuroTools normally obtains this mapping from ``asset_config``.  That
        game configuration is not embedded in every PAC, but MDL materials do
        retain SWITCH_DIFFUSEMAP0/1/2 flags.  Use those explicit flags first;
        slot zero remains only the compatibility fallback for older models.
        """

        enabled_diffuse_slots: list[int] = []
        for name, value in self.material_switches:
            normalized = name.upper()
            if not value or not normalized.startswith("SWITCH_DIFFUSEMAP"):
                continue
            suffix = normalized.removeprefix("SWITCH_DIFFUSEMAP")
            if suffix.isdigit():
                enabled_diffuse_slots.append(int(suffix))
        for preferred_slot in sorted(set(enabled_diffuse_slots)):
            for index, slot in enumerate(self.texture_slots):
                if slot == preferred_slot and index < len(self.textures):
                    return index
        for index, slot in enumerate(self.texture_slots):
            if slot == 0 and index < len(self.textures):
                return index
        return None


class _ByteCursor:
    def __init__(self, data: bytes, *, label: str) -> None:
        self._data = memoryview(data)
        self._label = label
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self.offset

    def read(self, size: int, *, field: str) -> bytes:
        if size < 0 or size > self.remaining:
            raise ValueError(
                f"{self._label} 中的{field}越界："
                f"需要 {size} 字节，仅剩 {self.remaining} 字节。"
            )
        start = self.offset
        self.offset += size
        return self._data[start : self.offset].tobytes()

    def read_u8(self, *, field: str) -> int:
        return self.read(1, field=field)[0]

    def read_u32(self, *, field: str) -> int:
        return struct.unpack("<I", self.read(4, field=field))[0]

    def read_text(self, *, field: str) -> str:
        size = self.read_u8(field=f"{field}长度")
        raw = self.read(size, field=field)
        return raw.decode("utf-8", errors="replace")


def parse_model_summary(
    path: Path,
) -> tuple[
    dict[str, int],
    tuple[ModelSection, ...],
    tuple[ModelMaterial, ...],
]:
    file_size = path.stat().st_size
    if file_size < 16:
        raise ValueError("MDL 文件不足 16 字节，无法读取文件头和结束标记。")
    sections: list[ModelSection] = []
    materials: list[ModelMaterial] = []
    with path.open("rb") as source:
        raw_header = source.read(12)
        magic, version, header_value = struct.unpack("<4sII", raw_header)
        if magic != b"MDL ":
            raise ValueError(f"不是支持的 MDL 文件：magic={magic!r}")

        offset = 12
        while True:
            if offset + 4 > file_size:
                raise ValueError("MDL 缺少 0xFFFFFFFF 结束标记。")
            source.seek(offset)
            raw_type = source.read(4)
            section_type = struct.unpack("<I", raw_type)[0]
            if section_type == 0xFFFFFFFF:
                if offset + 4 != file_size:
                    raise ValueError("MDL 结束标记后仍包含未解析数据。")
                break
            if len(sections) >= 1024:
                raise ValueError("MDL 区段数量异常，已停止解析。")
            raw_section_header = source.read(8)
            if len(raw_section_header) != 8:
                raise ValueError("MDL 区段头不完整。")
            stored_size, item_count = struct.unpack("<II", raw_section_header)
            if stored_size < 4:
                raise ValueError(
                    f"MDL 区段 0x{section_type:02X} 的长度小于最小值 4。"
                )
            next_offset = offset + 8 + stored_size
            if next_offset > file_size:
                raise ValueError(
                    f"MDL 区段 0x{section_type:02X} 越过文件末尾："
                    f"结束于 0x{next_offset:X}，文件大小为 0x{file_size:X}。"
                )
            section = ModelSection(
                section_type=section_type,
                offset=offset,
                stored_size=stored_size,
                item_count=item_count,
            )
            sections.append(section)
            if section_type == 0:
                body_size = stored_size - 4
                if body_size > 64 * 1024 * 1024:
                    raise ValueError("MDL 材质区段超过 64 MiB，拒绝整体载入。")
                if item_count > 100_000:
                    raise ValueError("MDL 材质数量异常，已停止解析。")
                body = source.read(body_size)
                if len(body) != body_size:
                    raise ValueError("MDL 材质区段数据不完整。")
                materials.extend(_parse_model_materials(body, item_count))
            offset = next_offset
    return (
        {"version": version, "header_value": header_value},
        tuple(sections),
        tuple(materials),
    )


def _parse_model_materials(
    data: bytes,
    material_count: int,
) -> tuple[ModelMaterial, ...]:
    errors: list[str] = []
    for texture_property_size in (20, 12):
        try:
            return _parse_model_material_layout(
                data,
                material_count,
                texture_property_size=texture_property_size,
            )
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(
        "MDL 材质区段不符合已知的新旧布局："
        + "；".join(errors)
    )


def _parse_model_material_layout(
    data: bytes,
    material_count: int,
    *,
    texture_property_size: int,
) -> tuple[ModelMaterial, ...]:
    cursor = _ByteCursor(data, label="MDL 材质区段")
    materials: list[ModelMaterial] = []
    parameter_value_counts = {
        0: 1,
        1: 1,
        2: 2,
        3: 3,
        4: 1,
        5: 2,
        6: 3,
        7: 4,
        8: 16,
    }

    for material_index in range(material_count):
        prefix = f"材质 {material_index + 1}"
        name = cursor.read_text(field=f"{prefix}名称")
        shader = cursor.read_text(field=f"{prefix}着色器")
        variant = cursor.read_text(field=f"{prefix}变体")

        texture_count = cursor.read_u32(field=f"{prefix}纹理数量")
        if texture_count > 100_000:
            raise ValueError(f"{prefix}纹理数量异常：{texture_count}。")
        textures: list[str] = []
        texture_slots: list[int] = []
        texture_wrap_s: list[int] = []
        texture_wrap_t: list[int] = []
        for texture_index in range(texture_count):
            texture_prefix = f"{prefix}纹理 {texture_index + 1}"
            textures.append(cursor.read_text(field=f"{texture_prefix}名称"))
            properties = cursor.read(
                texture_property_size,
                field=f"{texture_prefix}属性",
            )
            texture_slots.append(struct.unpack_from("<I", properties)[0])
            if texture_property_size == 20:
                wrap_s, wrap_t = struct.unpack_from("<ii", properties, 8)
            else:
                wrap_s, wrap_t = struct.unpack_from("<ii", properties, 4)
            texture_wrap_s.append(wrap_s)
            texture_wrap_t.append(wrap_t)

        parameter_count = cursor.read_u32(field=f"{prefix}参数数量")
        if parameter_count > 100_000:
            raise ValueError(f"{prefix}参数数量异常：{parameter_count}。")
        shader_parameters: list[tuple[str, int, tuple[float, ...]]] = []
        for parameter_index in range(parameter_count):
            parameter_prefix = f"{prefix}参数 {parameter_index + 1}"
            parameter_name = cursor.read_text(field=f"{parameter_prefix}名称")
            parameter_type = cursor.read_u32(field=f"{parameter_prefix}类型")
            value_count = parameter_value_counts.get(parameter_type, 0)
            raw_values = cursor.read(
                value_count * 4,
                field=f"{parameter_prefix}值",
            )
            values = (
                struct.unpack(f"<{value_count}f", raw_values)
                if value_count
                else ()
            )
            shader_parameters.append(
                (parameter_name, parameter_type, tuple(values))
            )

        switch_count = cursor.read_u32(field=f"{prefix}开关数量")
        if switch_count > 100_000:
            raise ValueError(f"{prefix}开关数量异常：{switch_count}。")
        material_switches: list[tuple[str, int]] = []
        for switch_index in range(switch_count):
            switch_prefix = f"{prefix}开关 {switch_index + 1}"
            switch_name = cursor.read_text(field=f"{switch_prefix}名称")
            switch_value = struct.unpack(
                "<i",
                cursor.read(4, field=f"{switch_prefix}值"),
            )[0]
            material_switches.append((switch_name, switch_value))

        uv_count = cursor.read_u32(field=f"{prefix} UV 映射数量")
        uv_map_indices = tuple(
            cursor.read(uv_count, field=f"{prefix} UV 映射")
        )
        trailing_byte_count = cursor.read_u32(field=f"{prefix}附加字节数量")
        cursor.read(trailing_byte_count, field=f"{prefix}附加字节")
        trailing_properties = struct.unpack(
            "<3IfI",
            cursor.read(20, field=f"{prefix}尾部属性"),
        )
        materials.append(
            ModelMaterial(
                name=name,
                shader=shader,
                variant=variant,
                textures=tuple(textures),
                texture_slots=tuple(texture_slots),
                texture_wrap_s=tuple(texture_wrap_s),
                texture_wrap_t=tuple(texture_wrap_t),
                shader_parameters=tuple(shader_parameters),
                material_switches=tuple(material_switches),
                uv_map_indices=uv_map_indices,
                trailing_properties=trailing_properties,
            )
        )

    if cursor.remaining:
        raise ValueError(
            f"MDL 材质区段解析后剩余 {cursor.remaining} 字节，"
            "当前结构与文件不一致。"
        )
    return tuple(materials)
