"""打包与许可证不变量。

这两组断言不是形式检查，各自锁住一次真实事故：

- **版本号单一来源**：版本曾在 4 个文件里各写一份，漂移已经真实发生
  （README 已 v0.6.0，pyproject 还停在 0.5.0）。
- **许可可分发**：`LICENSE` 曾经只躺在仓库根目录，构建产物里没有它 ——
  而 MIT 恰恰要求「随副本附带版权与许可声明」，这正是最容易被踩空的一条。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LICENSE_FILE = ROOT / "LICENSE"
README = ROOT / "README.md"

#: 版本号字面量唯一允许出现的地方
VERSION_SOURCE = "a2a_hub/__init__.py"

#: 扫版本硬编码时跳过的目录（构建产物 / 本机状态 / 临时目录）
_SKIP_DIRS = {
    ".git", ".workbuddy", ".pytest_cache", "node_modules",
    "build", "dist",  # setuptools 构建残留：里面的副本不是源码
}


def _project_table() -> dict:
    """读 pyproject 的 ``[project]`` 段。

    优先用标准库 ``tomllib``（3.11+）；3.10 没有它，退回只认这几个标量键的
    窄解析 —— 不为此引入 `tomli` 依赖，也不让断言在 3.10 上被静默跳过
    （静默跳过的测试等于没有测试）。
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        table: dict[str, object] = {}
        for key in ("name", "version", "license"):
            m = re.search(rf'^{key}\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                table[key] = m.group(1)
        m = re.search(r"^license-files\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
        if m:
            table["license-files"] = re.findall(r'"([^"]+)"', m.group(1))
        return table
    return tomllib.loads(text)["project"]


def _license_text_normalized() -> str:
    """LICENSE 正文压平空白，便于对跨行句子做断言。"""
    return re.sub(r"\s+", " ", LICENSE_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 许可证
# --------------------------------------------------------------------------- #


def test_license_expression_is_spdx():
    """pyproject 用 SPDX 表达式声明许可证（PEP 639），而不是旧式 ``{ text = ... }``。"""
    assert _project_table()["license"] == "MIT"


def test_license_file_is_verbatim_mit():
    """LICENSE 正文必须是**逐字**的 MIT。

    改动许可正文的措辞会让它不再成其为 MIT —— 授权条款与免责声明都是法律
    文本，改一个词就是换了一份协议。所以这里对两个关键段落做原文断言
    （压平空白后比对，避免因重新折行而误报）。
    """
    text = _license_text_normalized()
    assert text.startswith("MIT License"), "LICENSE 首行应为 'MIT License'"
    # 授权段落
    assert "Permission is hereby granted, free of charge, to any person obtaining a copy" in text
    assert "without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell" in text
    # 保留声明义务（分发时最难执行的一条）
    assert "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software." in text
    # 免责声明
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED' in text
    assert "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE" in text
    # 版权行必须有人认领，不能是空占位
    m = re.search(r"Copyright \(c\) (\d{4}) (.+)", text)
    assert m, "LICENSE 缺版权行"
    assert m.group(2).strip(), "版权归属为空"


def test_license_ships_with_distribution():
    """LICENSE 必须随构建产物分发。

    这条是 MIT 第 1 段义务的工程化落实：只在仓库根目录放一个 LICENSE，
    别人 `pip install` 之后是拿不到的。
    """
    assert "LICENSE" in _project_table().get("license-files", [])


def test_license_file_exists_and_is_not_empty():
    assert LICENSE_FILE.is_file()
    assert LICENSE_FILE.stat().st_size > 500, "LICENSE 体积异常，疑似被截断"


def test_readme_points_at_license():
    """README 要能让人找到协议，而不是只写一个 'MIT' 就算交代。"""
    text = README.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "](LICENSE)" in text, "README 应链接到 LICENSE 文件"


def test_no_license_classifier_alongside_expression():
    """PEP 639：有 license 表达式时不该再写 License:: 分类器。

    两者并存会被 setuptools 判定为元数据冲突（构建期告警甚至失败），
    且 GitHub / PyPI 的许可证识别会以表达式为准 —— 分类器纯属噪音。
    """
    classifiers = _project_table().get("classifiers", [])
    license_classifiers = [c for c in classifiers if c.startswith("License ::")]
    assert not license_classifiers, (
        f"license 表达式与分类器并存：{license_classifiers}"
    )


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #


def test_pyproject_version_matches_package():
    """pyproject 的 version 必须等于 ``a2a_hub.__version__``。"""
    from a2a_hub import __version__

    assert _project_table()["version"] == __version__


def test_project_version_literal_appears_exactly_once():
    """项目版本号只能有一个字面量出处。

    允许出现的只有 ``__init__.py`` 那一处；其余文件（HTTP 服务、CLI、MCP
    握手）都应从它派生。这条断言是「README 已 0.6.0 / pyproject 还 0.5.0」
    那次漂移的直接防线 —— 只比对 pyproject 是不够的，手写点可能在任何文件。
    """
    from a2a_hub import __version__

    literal = f'"{__version__}"'
    offenders: list[str] = []

    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if any(part in _SKIP_DIRS or part.startswith("_") for part in parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == VERSION_SOURCE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if literal in text:
            offenders.append(rel)

    assert not offenders, (
        f"版本号 {__version__} 被手写在 {offenders}；"
        f"应从 {VERSION_SOURCE} 的 __version__ 派生"
    )
