#!/usr/bin/env python3
"""
quick_validate.py — 轻量级 Skill 格式验证脚本

用法：
    python quick_validate.py <skill_path>
    python quick_validate.py skills/make-knowledge-cards

验证内容：
    1. SKILL.md 存在且 frontmatter 格式正确
    2. frontmatter 只包含允许的 key
    3. name 和 description 符合命名/长度规则
    4. agents/openai.yaml 存在且 interface 元数据齐全
"""

import os
import re
import sys
import json

MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MIN_SHORT_DESCRIPTION_LENGTH = 25
MAX_SHORT_DESCRIPTION_LENGTH = 64
ALLOWED_FRONTMATTER_KEYS = {"name", "description", "license", "allowed-tools", "metadata"}

FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
HYPHEN_CASE_PATTERN = re.compile(r"^[a-z0-9-]+$")


def _parse_simple_yaml(text: str) -> dict:
    """
    极简 YAML 解析器，支持：
      - 键值对（key: value 或 "value"）
      - 列表（key: 后跟以 "- " 开头的行）
      - 嵌套字典（key: 后跟缩进 key: value，递归）
      - 多行字面量（key: | 后跟缩进行，任意层级）
    不使用第三方依赖，避免 PyYAML 未安装的情况。
    如遇解析失败，抛出 ValueError。
    """
    lines = text.splitlines()

    def unquote(s: str) -> str:
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return s[1:-1]
        return s

    def parse_scalar(s: str):
        s = s.strip()
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        if s.lower() in ("null", "~", "none"):
            return None
        try:
            if re.fullmatch(r"-?\d+", s):
                return int(s)
        except Exception:
            pass
        try:
            if re.fullmatch(r"-?\d+\.\d+", s):
                return float(s)
        except Exception:
            pass
        return unquote(s)

    def skip_blanks_and_comments(idx, min_indent):
        """跳过空行/注释但不越过 min_indent 层级（None 表示不过滤），返回新 idx"""
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if min_indent is not None and indent < min_indent:
                break
            break
        return idx

    def parse_block_scalar(idx, base_indent):
        """解析 | 块字面量，返回 (值, 新 idx)"""
        idx += 1
        block_lines = []
        block_indent = None
        while idx < len(lines):
            line = lines[idx]
            if not line.strip():
                if block_indent is not None:
                    content = line[block_indent:] if len(line) >= block_indent else ""
                    block_lines.append(content)
                idx += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if block_indent is None:
                block_indent = indent
            if indent < block_indent or indent <= base_indent:
                break
            content = line[block_indent:] if len(line) >= block_indent else line.strip()
            block_lines.append(content)
            idx += 1
        return "\n".join(block_lines).rstrip() + "\n", idx

    def parse_collection(idx, base_indent):
        """
        从 idx 开始解析一个字典或列表（下一行缩进应 > base_indent）。
        返回 (值, 新 idx)。值可能是 dict 或 list 或 None。
        """
        items = []
        nested: dict = {}
        list_mode = None
        nested_indent = None
        while idx < len(lines):
            idx = skip_blanks_and_comments(idx, None)
            if idx >= len(lines):
                break
            line = lines[idx]
            indent = len(line) - len(line.lstrip(" "))
            if nested_indent is None:
                nested_indent = indent
            if indent < nested_indent or indent <= base_indent:
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                if list_mode is None:
                    list_mode = True
                if not list_mode:
                    raise ValueError(f"第 {idx + 1} 行：列表和字典不能混用")
                item_content = stripped[2:]
                # "- " 后也可能是嵌套结构（本项目里是字符串）
                if item_content == "|":
                    val, idx = parse_block_scalar(idx, indent)
                    items.append(val)
                else:
                    items.append(parse_scalar(item_content))
                    idx += 1
            elif ":" in stripped:
                if list_mode is None:
                    list_mode = False
                if list_mode:
                    raise ValueError(f"第 {idx + 1} 行：列表和字典不能混用")
                colon_idx = stripped.index(":")
                nk = stripped[:colon_idx].strip()
                nv = stripped[colon_idx + 1:].strip()
                if not nk:
                    raise ValueError(f"第 {idx + 1} 行缺少 key: {line}")
                if nv == "|":
                    val, idx = parse_block_scalar(idx, indent)
                    nested[nk] = val
                elif nv:
                    nested[nk] = parse_scalar(nv)
                    idx += 1
                else:
                    idx += 1
                    child_val, idx = parse_collection(idx, indent)
                    nested[nk] = child_val if child_val is not None else {}
            else:
                raise ValueError(f"第 {idx + 1} 行无法解析: {line}")

        if list_mode is True:
            return items, idx
        if list_mode is False:
            return nested, idx
        return None, idx

    # 顶层解析（等同于 parse_collection，起始缩进为 -1）
    result: dict = {}
    i = 0
    while i < len(lines):
        i = skip_blanks_and_comments(i, None)
        if i >= len(lines):
            break
        line = lines[i]
        stripped = line.strip()
        if ":" not in stripped:
            raise ValueError(f"第 {i + 1} 行格式错误（缺少冒号）: {line}")
        colon_idx = stripped.index(":")
        key = stripped[:colon_idx].strip()
        value_part = stripped[colon_idx + 1:].strip()
        if not key:
            raise ValueError(f"第 {i + 1} 行缺少 key: {line}")
        indent = len(line) - len(line.lstrip(" "))

        if value_part == "|":
            val, i = parse_block_scalar(i, indent)
            result[key] = val
        elif value_part:
            result[key] = parse_scalar(value_part)
            i += 1
        else:
            i += 1
            child_val, i = parse_collection(i, indent)
            result[key] = child_val if child_val is not None else {}
    return result


def load_frontmatter(skill_md_path: str) -> dict:
    with open(skill_md_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = FRONTMATTER_PATTERN.match(content)
    if not m:
        raise ValueError("SKILL.md frontmatter 格式错误：未找到 --- 包裹的 YAML 块")
    yaml_text = m.group(1)
    return _parse_simple_yaml(yaml_text)


def validate_skill_name(name: object) -> list:
    errors = []
    if not isinstance(name, str):
        errors.append("name 字段必须是字符串")
        return errors
    if len(name) == 0:
        errors.append("name 不能为空")
        return errors
    if len(name) > MAX_SKILL_NAME_LENGTH:
        errors.append(f"name 长度超过 {MAX_SKILL_NAME_LENGTH} 字符（当前 {len(name)}）")
    if not HYPHEN_CASE_PATTERN.match(name):
        errors.append("name 必须使用小写字母、数字和连字符（hyphen-case）")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name 不能以连字符开头或结尾")
    if "--" in name:
        errors.append("name 不能包含连续连字符")
    return errors


def validate_description(desc: object) -> list:
    errors = []
    if not isinstance(desc, str):
        errors.append("description 字段必须是字符串")
        return errors
    if len(desc) == 0:
        errors.append("description 不能为空")
        return errors
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"description 长度超过 {MAX_DESCRIPTION_LENGTH} 字符（当前 {len(desc)}）")
    if "<" in desc or ">" in desc:
        errors.append("description 不能包含 < 或 > 字符")
    return errors


def validate_frontmatter_keys(fm: dict) -> list:
    errors = []
    keys = set(fm.keys())
    unexpected = keys - ALLOWED_FRONTMATTER_KEYS
    if unexpected:
        errors.append(
            f"SKILL.md frontmatter 包含不允许的 key: {sorted(unexpected)}。"
            f"允许的 key: {sorted(ALLOWED_FRONTMATTER_KEYS)}"
        )
    for required in ("name", "description"):
        if required not in fm:
            errors.append(f"SKILL.md frontmatter 缺少必填字段: {required}")
    return errors


def validate_openai_yaml(yaml_path: str, skill_name: str) -> list:
    errors = []
    if not os.path.isfile(yaml_path):
        errors.append(f"缺少 agents/openai.yaml 文件: {yaml_path}")
        return errors
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = _parse_simple_yaml(f.read())
    except Exception as e:
        errors.append(f"agents/openai.yaml 解析失败: {e}")
        return errors

    interface = data.get("interface") if isinstance(data, dict) else None
    if not isinstance(interface, dict):
        errors.append("agents/openai.yaml 缺少 interface 字典")
        return errors

    for required in ("display_name", "short_description", "default_prompt"):
        if required not in interface:
            errors.append(f"agents/openai.yaml interface 缺少字段: {required}")

    sd = interface.get("short_description")
    if isinstance(sd, str):
        if len(sd) < MIN_SHORT_DESCRIPTION_LENGTH:
            errors.append(
                f"interface.short_description 太短（当前 {len(sd)}，最少 {MIN_SHORT_DESCRIPTION_LENGTH}）"
            )
        if len(sd) > MAX_SHORT_DESCRIPTION_LENGTH:
            errors.append(
                f"interface.short_description 太长（当前 {len(sd)}，最多 {MAX_SHORT_DESCRIPTION_LENGTH}）"
            )

    dp = interface.get("default_prompt")
    if isinstance(dp, str):
        token = f"${skill_name}"
        if token not in dp:
            errors.append(
                f"interface.default_prompt 应显式引用技能名标记 {token}"
            )
    return errors


def validate_skill(skill_path: str) -> tuple:
    """返回 (success: bool, message: str)"""
    if not os.path.isdir(skill_path):
        return False, f"路径不是目录: {skill_path}"

    skill_md = os.path.join(skill_path, "SKILL.md")
    if not os.path.isfile(skill_md):
        return False, "SKILL.md not found"

    try:
        fm = load_frontmatter(skill_md)
    except Exception as e:
        return False, str(e)

    errors = []
    errors.extend(validate_frontmatter_keys(fm))
    if "name" in fm:
        errors.extend(validate_skill_name(fm["name"]))
    if "description" in fm:
        errors.extend(validate_description(fm["description"]))

    skill_name = fm.get("name", "") if isinstance(fm.get("name"), str) else ""
    openai_yaml = os.path.join(skill_path, "agents", "openai.yaml")
    errors.extend(validate_openai_yaml(openai_yaml, skill_name))

    if errors:
        return False, "\n".join("- " + e for e in errors)
    return True, "Skill is valid!"


def main(argv: list) -> int:
    if len(argv) < 2:
        print("用法: python quick_validate.py <skill_path>", file=sys.stderr)
        return 2
    skill_path = argv[1]
    ok, msg = validate_skill(skill_path)
    if ok:
        print(f"✅ Skill validated successfully — {msg}")
        return 0
    print(f"❌ Validation failed for '{skill_path}':\n{msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
