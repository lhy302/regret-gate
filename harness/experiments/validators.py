"""任务验证器（构建规范 §12.2 / §13.1）。

必须实现的验证器：
- `compile`：代码能否编译/解析（Python 用 `ast.parse`，TypeScript 走 `tsc --noEmit`，后者
  只在显式配置且可执行文件存在时启用，否则记为 `skipped` 而不是伪造通过）；
- `unit_test`：给定测试文件，跑测试（真实执行，不伪造结果）；
- `regex`：输出是否匹配给定正则；
- `llm_judge`：用独立模型判定（需要注入审核客户端；未注入时明确标记 `unavailable`）；
- `manual`：不做机器判定，按未通过记录。

返回结构统一为：
```
{
  "kind": str,
  "passed": bool,          # 是否通过（决定 task_success）
  "syntax_error": bool,    # 仅 compile / unit_test 有意义
  "logic_error": bool,
  "details": {...}
}
```
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

_TS_SUFFIXES = (".ts", ".tsx")


def validate_task_output(task, text: str, workdir: Optional[str] = None, judge_client=None) -> dict:
    spec = task.validation
    kind = spec.kind
    config = spec.config or {}
    if kind == "compile":
        return _validate_compile(task, text, config, workdir)
    if kind == "unit_test":
        return _validate_unit_test(task, text, config, workdir)
    if kind == "regex":
        return _validate_regex(task, text, config)
    if kind == "llm_judge":
        return _validate_llm_judge(task, text, config, judge_client)
    if kind == "manual":
        return {
            "kind": kind,
            "passed": False,
            "syntax_error": False,
            "logic_error": False,
            "details": {"reason": "manual validation is not machine-checkable in this experiment"},
        }
    return {
        "kind": kind,
        "passed": False,
        "syntax_error": False,
        "logic_error": False,
        "details": {"reason": f"unsupported validation kind: {kind!r}"},
    }


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def _extract_code(text: str) -> str:
    """优先取第一个 ```python / ``` 围栏块；没有围栏则整体当作代码。"""
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def _validate_compile(task, text: str, config: dict, workdir: Optional[str]) -> dict:
    language = config.get("language", "python")
    code = _extract_code(text)
    if language == "python":
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return {
                "kind": "compile",
                "passed": False,
                "syntax_error": True,
                "logic_error": False,
                "details": {"language": "python", "error": f"{exc.msg} (line {exc.lineno})"},
            }
        return {
            "kind": "compile",
            "passed": True,
            "syntax_error": False,
            "logic_error": False,
            "details": {"language": "python", "lines": code.count("\n") + 1},
        }
    if language in ("typescript", "ts"):
        tsc = shutil.which("tsc")
        if not tsc:
            return {
                "kind": "compile",
                "passed": False,
                "syntax_error": False,
                "logic_error": False,
                "details": {"language": "typescript", "status": "unavailable", "reason": "tsc not installed"},
            }
        directory = workdir or tempfile.mkdtemp(prefix="harness_ts_")
        path = os.path.join(directory, "artifact.ts")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(code)
        proc = subprocess.run(
            [tsc, "--noEmit", "--target", "es2019", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "kind": "compile",
            "passed": proc.returncode == 0,
            "syntax_error": proc.returncode != 0,
            "logic_error": False,
            "details": {"language": "typescript", "returncode": proc.returncode, "stderr": proc.stderr[:2000]},
        }
    return {
        "kind": "compile",
        "passed": False,
        "syntax_error": False,
        "logic_error": False,
        "details": {"language": language, "status": "unavailable"},
    }


# ---------------------------------------------------------------------------
# unit_test
# ---------------------------------------------------------------------------


def _validate_unit_test(task, text: str, config: dict, workdir: Optional[str]) -> dict:
    test_file = config.get("test_file")
    directory = workdir or tempfile.mkdtemp(prefix="harness_ut_")
    os.makedirs(directory, exist_ok=True)
    code = _extract_code(text)
    module_path = os.path.join(directory, "artifact_under_test.py")
    with open(module_path, "w", encoding="utf-8") as handle:
        handle.write(code)
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return {
            "kind": "unit_test",
            "passed": False,
            "syntax_error": True,
            "logic_error": False,
            "details": {"stage": "parse", "error": f"{exc.msg} (line {exc.lineno})"},
        }
    if not test_file or not os.path.isfile(test_file):
        return {
            "kind": "unit_test",
            "passed": False,
            "syntax_error": False,
            "logic_error": False,
            "details": {
                "stage": "test_file",
                "status": "missing",
                "test_file": test_file,
                "reason": "declared test file not found; run recorded as failed, never faked as passed",
            },
        }
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", os.path.basename(test_file), "-v"],
        cwd=os.path.dirname(os.path.abspath(test_file)) or ".",
        capture_output=True,
        text=True,
        timeout=float(config.get("timeout", 120)),
        env={**os.environ, "ARTIFACT_MODULE_PATH": module_path, "ARTIFACT_TEXT_PATH": _dump_text(directory, text)},
    )
    passed = proc.returncode == 0
    return {
        "kind": "unit_test",
        "passed": passed,
        "syntax_error": False,
        "logic_error": not passed,
        "details": {
            "test_file": test_file,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        },
    }


def _dump_text(directory: str, text: str) -> str:
    path = os.path.join(directory, "artifact.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ---------------------------------------------------------------------------
# regex
# ---------------------------------------------------------------------------


def _validate_regex(task, text: str, config: dict) -> dict:
    pattern = config.get("pattern")
    if not pattern:
        return {
            "kind": "regex",
            "passed": False,
            "syntax_error": False,
            "logic_error": True,
            "details": {"reason": "validation.config.pattern is required"},
        }
    flags = re.MULTILINE
    if config.get("ignore_case"):
        flags |= re.IGNORECASE
    if config.get("dotall"):
        flags |= re.DOTALL
    match = re.search(pattern, text, flags)
    mode = config.get("mode", "search")
    passed = bool(match)
    if config.get("must_not_match"):
        passed = not passed
    details = {
        "pattern": pattern,
        "mode": mode,
        "match": bool(match),
        "must_not_match": bool(config.get("must_not_match", False)),
        "text_len": len(text),
    }
    if match:
        details["matched"] = match.group(0)[:200]
    return {
        "kind": "regex",
        "passed": passed,
        "syntax_error": False,
        "logic_error": not passed,
        "details": details,
    }


# ---------------------------------------------------------------------------
# llm_judge
# ---------------------------------------------------------------------------


def _validate_llm_judge(task, text: str, config: dict, judge_client) -> dict:
    if judge_client is None:
        return {
            "kind": "llm_judge",
            "passed": False,
            "syntax_error": False,
            "logic_error": False,
            "details": {"status": "unavailable", "reason": "no judge client injected"},
        }
    from core.types import SamplingConfig

    prompt = (
        f"任务要求：\n{config.get('criteria', task.prompt)}\n\n"
        f"待判定输出：\n{text[:6000]}\n\n"
        '只输出 JSON：{"passed": true|false, "reason": "..."}'
    )
    sampling = SamplingConfig(
        model=config.get("model", "judge-model-v1"),
        temperature=float(config.get("temperature", 0.0)),
        max_tokens=int(config.get("max_tokens", 300)),
        seed=config.get("seed", 0),
    )
    try:
        raw, _usage = judge_client.complete(
            system="你是一个严格的评审，只输出 JSON。", prompt=prompt, sampling=sampling, timeout=30.0
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "kind": "llm_judge",
            "passed": False,
            "syntax_error": False,
            "logic_error": False,
            "details": {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
        }
    import json

    passed = False
    reason = raw[:400]
    try:
        payload = json.loads(raw.strip())
        passed = bool(payload.get("passed"))
        reason = str(payload.get("reason", ""))[:400]
    except (json.JSONDecodeError, TypeError, AttributeError):
        passed = False
    return {
        "kind": "llm_judge",
        "passed": passed,
        "syntax_error": False,
        "logic_error": not passed,
        "details": {"reason": reason, "raw": raw[:400]},
    }
