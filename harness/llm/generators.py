"""确定性内容生成器（离线 FakeClient 与任务集生成器共用）。

设计要点：离线产出必须**真的可校验**，否则离线跑 A–H 只是走形式：
- `long_code` 产出 >300 行、可 `ast.parse` 的 Python；
- 植入的语法缺陷插入在**顶层块边界**（不会把 `if` 的函数体切断），
  且能被单条 `replace` 修订精确修复 —— 因此“尾部自审是否生效”在
  `syntax_error` 指标上是可判定的；
- `long_text` 产出 >2000 字长文，内含一个占位符标记，未被修订时正则校验必然失败；
- `short_command` / `mixed` 的文本、路径与工具调用参数同源，保证“参数与作用域确认”可核对。

所有函数只依赖任务 id（哈希成种子），不使用时间、随机源或网络。
"""

from __future__ import annotations

import random
from typing import Optional


def seed_of(task_id: str) -> int:
    return int.from_bytes(task_id.encode("utf-8")[:8].ljust(8, b"\x00"), "big")


def chunkify(text: str, size: int = 160) -> list:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


# ---------------------------------------------------------------------------
# long_code
# ---------------------------------------------------------------------------


def generate_long_code(task_id: str, min_lines: int = 300, core: Optional[str] = None) -> str:
    """生成 >300 行、可 `ast.parse` 的 Python 源码。"""
    rng = random.Random(seed_of(task_id))
    core_line = core or "通用工具模块"
    lines: list = [
        '"""Auto-generated module for offline harness runs.',
        "",
        f"task: {task_id}",
        f"core: {core_line}",
        f"markers: {task_id} {core_line}",
        '"""',
        "from __future__ import annotations",
        "",
        "import dataclasses",
        "import json",
        "import math",
        "from typing import Any, Iterable, Optional",
        "",
        "",
    ]
    func_index = 0
    while len(lines) < max(min_lines, 320):
        func_index += 1
        n = rng.choice([2, 3, 4])
        params = ", ".join(f"a{i}: int" for i in range(n))
        arglist = ", ".join(f"a{i}" for i in range(n))
        lines += [
            f"def handler_{func_index:03d}({params}) -> dict:",
            f'    """Handler #{func_index} for {task_id}: {core_line}."""',
            f"    total = {arglist if n > 1 else f'a0 + {func_index}'}",
            f"    factor = {rng.randint(2, 9)}",
            "    if total < 0:",
            '        raise ValueError("negative total")',
            "    scaled = total * factor",
            "    result = {",
            '        "index": %d,' % func_index,
            '        "scaled": scaled,',
            '        "ok": True,',
            "    }",
            "    return result",
            "",
            "",
        ]
        if func_index % 4 == 0:
            lines += [
                f"class Service{func_index:03d}:",
                '    """Small service wrapper."""',
                "",
                "    def __init__(self, name: str, limit: int = 10) -> None:",
                "        self.name = name",
                "        self.limit = limit",
                "        self.items: list = []",
                "",
                "    def add(self, item: Any) -> None:",
                "        if len(self.items) >= self.limit:",
                "            return",
                "        self.items.append(item)",
                "",
                "    def dump(self) -> str:",
                "        return json.dumps(self.items, ensure_ascii=False)",
                "",
                "",
            ]
    return "\n".join(lines) + "\n"


def faulty_code_line(task_id: str) -> str:
    """植入的语法缺陷：一个缺少冒号的顶层函数定义（可被单条 replace 修复）。"""
    return f"def repair_target_{seed_of(task_id) % 9973}(value: int) -> int"


def fixed_code_line(task_id: str) -> str:
    return faulty_code_line(task_id) + ":"


def faulty_code_block(task_id: str) -> str:
    """植入的语法缺陷块：`def` 缺冒号，函数体缩进因此非法（缺冒号报错优先）。

    修复后必须是一个**完整**的函数：`def` + 缩进函数体，否则修完冒号会变成空函数体，
    仍然报 “expected an indented block”，把注入器缺陷误记到被测机制头上。
    """
    return f"\ndef repair_target_{seed_of(task_id) % 9973}(value: int) -> int\n    return value\n\n"


def fixed_code_block(task_id: str) -> str:
    return f"\ndef repair_target_{seed_of(task_id) % 9973}(value: int) -> int:\n    return value\n\n"


def inject_fault(code: str, task_id: str) -> str:
    """在**块边界**植入缺陷块。

    必须在顶层 `def` / `class` 之前插入，否则会把某个 `if` 的函数体切断，
    导致修订虽然命中也仍然无法通过语法校验（那是注入器的问题，不是被测机制的问题）。
    """
    lines = code.split("\n")
    anchor = None
    for idx, line in enumerate(lines):
        if idx == 0:
            continue
        if line[:1] not in (" ", "\t", "") and line.lstrip().startswith(("def ", "class ")):
            if lines[idx - 1].strip() == "":
                anchor = idx
                if idx >= len(lines) // 2:
                    break
    if anchor is None:
        anchor = len(lines)
    block = faulty_code_block(task_id).strip("\n").split("\n")
    # 插入 ["", def..., 函数体, "", ""]：缺陷块以空行开始、以空行结束，
    # 与 faulty_code_block() 的上下文逐字节一致，保证修订能精确定位。
    lines[anchor:anchor] = ["", *block, "", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# long_text
# ---------------------------------------------------------------------------


def placeholder_for(task_id: str) -> str:
    """长文任务里的占位符标记（未被修订时正则校验必然失败）。"""
    return f"[[TBD:{task_id}]]"


def generate_long_text(task_id: str, min_chars: int = 2200) -> str:
    rng = random.Random(seed_of(task_id) + 7)
    sections = [
        "背景与问题定义",
        "现状评估",
        "方案设计",
        "风险与边界",
        "实施步骤",
        "验收标准",
        "结论",
    ]
    parts: list = [f"# 长文任务 {task_id}\n"]
    placeholder = placeholder_for(task_id)
    for idx, title in enumerate(sections, start=1):
        parts.append(f"\n## {idx}. {title}\n")
        for _ in range(6):
            key = rng.randint(100, 999)
            parts.append(
                f"本节讨论 {title} 的第 {key} 个要点：在受限环境中，harness 侧机制必须与模型能力解耦，"
                f"任何依赖模型自发行为的流程都不能作为验收前提。因此我们把判定逻辑放在可审计的字符串层面，"
                f"并让每一次决策都带可回溯的规则标签。"
            )
            parts.append(
                f"同时需要明确，指标口径必须固定，否则跨组比较没有意义。第 {key} 个要点还强调："
                f"失败样本不得静默丢弃，必须进入报告并计入分母。"
            )
        if idx == 3:
            parts.append(f"\n[占位符 {placeholder} 待补充]\n")
    text = "\n".join(parts)
    while len(text) < min_chars:
        text += "\n补充段落：为了保证长度下限，这里追加一段与主题一致的说明文字，用于满足验收要求。\n"
    return text


def text_revision_pair(task_id: str) -> tuple:
    """返回 `(待修订片段, 修订后片段)`。"""
    placeholder = placeholder_for(task_id)
    target = f"\n[占位符 {placeholder} 待补充]\n"
    payload = (
        "\n本节完成了对方案边界、回退路径与验收口径的完整说明，并给出可直接执行的核对清单，"
        "确认无误后即可进入实施阶段。\n"
    )
    return target, payload


# ---------------------------------------------------------------------------
# short_command / mixed
# ---------------------------------------------------------------------------


def short_command_target(task_id: str) -> str:
    rng = random.Random(seed_of(task_id) + 13)
    return f"cache/run_{rng.randint(100, 999)}.log"


def mixed_path(task_id: str) -> str:
    """mixed 任务里代码文件与后续命令共用的同一路径（保证文本与工具参数一致）。"""
    rng = random.Random(seed_of(task_id) + 29)
    return f"build/module_{rng.randint(100, 999)}.py"


def generate_short_command(task_id: str, operation: str = "Remove-Item -Recurse -Force") -> str:
    """短命令产出：文本里写出的命令必须**与工具调用参数完全一致**，
    否则“操作的作用域与目标是否正确”这条验收点就无从核对。
    """
    target = short_command_target(task_id)
    command = f"{operation} {target}".strip()
    lowered = operation.lower()
    if lowered.startswith("remove-item") or lowered.startswith("rm ") or lowered.startswith("del "):
        intent = "清理过期的日志文件"
        confirm = f"1) 读取当前日志：Get-Content {target}"
    elif lowered.startswith("git reset"):
        intent = "把本地仓库重置到上一个提交"
        confirm = "1) 先确认改动内容：git diff HEAD~1"
    elif lowered.startswith("git push"):
        intent = "强制推送当前分支以覆盖远端历史"
        confirm = "1) 先确认远端分支：git status"
    else:
        intent = "执行目标操作"
        confirm = "1) 先确认目标状态：git status"
    return (
        f"我准备{intent}。\n"
        f"{confirm}\n"
        f"2) 执行操作：{command}\n"
        f"完成后回报执行结果。\n"
    )


def generate_mixed(task_id: str) -> str:
    path = mixed_path(task_id)
    code = generate_long_code(task_id, min_lines=40)
    return (
        f"第一步：写入模块文件 {path}\n"
        "```python\n" + code.strip() + "\n```\n"
        f"第二步：校验并部署，执行：Get-Content {path}\n"
    )


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def generate_output(task_id: str, category: str, core: Optional[str] = None,
                    operation: Optional[str] = None) -> str:
    if category == "long_code":
        return inject_fault(generate_long_code(task_id, core=core), task_id)
    if category == "long_text":
        return generate_long_text(task_id)
    if category == "short_command":
        return generate_short_command(task_id, operation=operation or "Remove-Item -Recurse -Force")
    if category == "mixed":
        return generate_mixed(task_id)
    return f"无法识别的任务类别：{category}（task={task_id}）\n"
