"""Assemble a preview app and verified corresponding-source release assets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.request
import zipfile


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url: str, destination: Path, expected: str, algorithm: str = 'sha256') -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with destination.open('rb') as stream:
            if hashlib.file_digest(stream, algorithm).hexdigest() == expected:
                return
    error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                payload = response.read()
            if hashlib.new(algorithm, payload).hexdigest() != expected:
                raise ValueError(f'Checksum mismatch for {destination.name}')
            destination.write_bytes(payload)
            return
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'Failed to obtain {destination.name}') from error


def zip_tree(root: Path, destination: Path, *, stored: bool = False) -> None:
    mode = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(destination, 'w', mode) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(root.parent).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f'ZIP verification failed: {destination.name}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--contrib-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((root/'dist/TIS_Retext-build.json').read_text(encoding='utf-8-sig'))
    version = manifest['version']
    stage = root/'.runtime/release-staging/TIS_Retext'
    if stage.exists():
        raise RuntimeError('Use a fresh release staging directory')
    shutil.copytree(root/'dist/TIS_Retext', stage)
    for name in ['LICENSE', 'CREDITS.md', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md']:
        shutil.copy2(root/name, stage/name)
        shutil.copy2(root/name, stage/'_internal'/name)
    for name in ['docs', 'mappings', 'licenses']:
        shutil.copytree(root/name, stage/name)
    shutil.copy2(root/'dist/TIS_Retext-build.json', stage/'build-info.json')
    (stage/'使用说明.txt').write_text(
        f'TIS_Retext {version} — Windows x64 预览版\n\n'
        '自有代码采用 MIT；第三方组件保留原许可证。\n\n'
        '完整解压后运行 TIS_Retext.exe，无需安装 Python；请保留 _internal 文件夹。\n'
        '请解压到可写的非系统盘目录，缓存默认位于程序旁的 TIS_Retext_Data。\n'
        '操作指南：docs/EDITOR_GUIDE.md\n'
        '参考方案：mappings/常用文本替换_不含金.json（30 条规则）。\n'
        '在“批量”页导入映射表，添加处理范围，扫描并核对后再替换、导出 PAC。\n'
        '导入会覆盖当前映射表；不包含“金”的替换规则或章节图片。\n'
        '请备份游戏原文件，先导出到新文件并检查，再替换游戏资源。\n\n'
        '第三方对应源码：同一 Release 的 third-party-sources.zip，普通使用无需下载。\n'
        '源码、更新与反馈：https://github.com/WhalerO/sora_no_kiseki_pac_editor\n'
        '这是非官方工具，不附带游戏资源。\n', encoding='utf-8-sig')
    for path in stage.rglob('*'):
        if path.name in {'.runtime', 'TIS_Retext_Data', '__pycache__'} or path.suffix.lower() in {
            '.pac', '.mdl', '.dds', '.tbl', '.dat', '.pyc', '.wav', '.webm',
        }:
            raise RuntimeError(f'Forbidden runtime/game file: {path.name}')
    if sha256(stage/'TIS_Retext.exe') != manifest['executable_sha256'].lower():
        raise RuntimeError('Executable does not match build manifest')
    app_zip = output/f'TIS_Retext-{version}-win-x64-onedir.zip'
    zip_tree(stage, app_zip)

    sources = root/'.runtime/third-party-sources'
    sources.mkdir(parents=True, exist_ok=True)
    download('https://download.videolan.org/pub/videolan/vlc/3.0.23/vlc-3.0.23.tar.xz',
             sources/'vlc-3.0.23.tar.xz',
             'e891cae6aa3ccda69bf94173d5105cbc55c7a7d9b1d21b9b21666e69eff3e7e0')
    records = json.loads(args.contrib_manifest.read_text(encoding='utf-8'))
    if len(records) != 126:
        raise RuntimeError('Unexpected VLC contrib source inventory')

    def fetch_input(record: dict) -> None:
        name = record['name']
        if Path(name).name != name:
            raise ValueError('Invalid contrib input name')
        download(record['url'], sources/'contrib'/name, record['sha512'], 'sha512')

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch_input, records))
    shutil.copy2(args.contrib_manifest, sources/'contrib-manifest.json')
    with urllib.request.urlopen('https://pypi.org/pypi/python-vlc/3.0.21203/json', timeout=60) as response:
        binding = next(x for x in json.load(response)['urls'] if x['packagetype'] == 'sdist')
    download(binding['url'], sources/binding['filename'], binding['digests']['sha256'])
    (sources/'python-vlc-source.json').write_text(json.dumps({
        'url': binding['url'], 'name': binding['filename'], 'sha256': binding['digests']['sha256'],
    }, indent=2), encoding='utf-8')
    shutil.copytree(root/'pac_tools', sources/'pac_tools',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (sources/'README.txt').write_text(
        'Corresponding source for TIS_Retext '+version+'\n\n'
        'VLC 3.0.23 source contains build instructions, contrib recipes and patches.\n'
        'contrib/ includes all 126 inputs from the upstream SHA512SUMS lists.\n'
        'Place these inputs in VLC contrib/tarballs/ when rebuilding.\n'
        'python-vlc and pac_tools source retain their upstream licenses.\n'
        'Every component retains its own copyright and license; MIT does not relicense them.\n'
        'This archive is not needed to run the application.\n'
        f'Application source: https://github.com/WhalerO/sora_no_kiseki_pac_editor/tree/v{version}\n',
        encoding='utf-8')
    source_zip = output/f'TIS_Retext-{version}-third-party-sources.zip'
    zip_tree(sources, source_zip, stored=True)
    # GitHub normalizes non-ASCII asset names; keep the original inside the ZIP.
    mapping = output/'common-text-replacements-no-zin.json'
    shutil.copy2(root/'mappings/常用文本替换_不含金.json', mapping)
    assets = [app_zip, source_zip, mapping]
    manifest.update(public_source_commit=manifest['git_commit'], source_tag='v'+version,
                    original_code_license='MIT',
                    release_profile='preview; CPython 3.14.3; cloud-built from the release tag',
                    assets=[{'name':p.name, 'size':p.stat().st_size, 'sha256':sha256(p)} for p in assets])
    metadata = output/f'TIS_Retext-{version}-release.json'
    metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    assets.append(metadata)
    (output/'SHA256SUMS.txt').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in assets), encoding='utf-8')
    print(json.dumps(manifest['assets'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
